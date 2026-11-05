"""
Création de compte et authentification.

Hachage de mot de passe en PBKDF2-HMAC-SHA256 (bibliothèque standard Python,
pas de dépendance externe). Pour une vraie mise en production, bcrypt ou
argon2 restent préférables, mais PBKDF2 via hashlib est un choix raisonnable
et suffisamment robuste pour un MVP.
"""

import hashlib
import hmac
import os
import re
import sqlite3
from pathlib import Path

from src.db import get_connection

PBKDF2_ITERATIONS = 600_000          # recommandation OWASP 2023 pour PBKDF2-HMAC-SHA256
LEGACY_PBKDF2_ITERATIONS = 260_000   # comptes créés avant l'augmentation (hash sans préfixe)
HASH_PREFIX = "pbkdf2_sha256"
# Sel fixe servant uniquement à dépenser le même temps de calcul pour un email inconnu
_DUMMY_SALT = bytes(16)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254  # limite standard RFC 5321
MAX_PASSWORD_LENGTH = 128  # évite un coût PBKDF2 excessif sur une entrée très longue


class AuthError(Exception):
    """Erreur de validation ou d'authentification (email invalide, déjà pris,
    mot de passe trop court, identifiants incorrects...)."""


def _hash_password(
    password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS
) -> tuple[str, str]:
    """Retourne (hash stocké, sel en hexadécimal).

    Le hash est stocké sous la forme 'pbkdf2_sha256$<itérations>$<hex>' : le nombre
    d'itérations voyage avec le hash, ce qui permet de l'augmenter plus tard sans
    casser les comptes existants.
    """
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{HASH_PREFIX}${iterations}${digest.hex()}", salt.hex()


def _parse_stored_hash(stored: str) -> tuple[int, str]:
    """(itérations, hash hexadécimal). Sans préfixe = ancien format (260 000 itérations)."""
    if stored.startswith(HASH_PREFIX + "$"):
        _, iterations, digest = stored.split("$", 2)
        return int(iterations), digest
    return LEGACY_PBKDF2_ITERATIONS, stored


def register_user(db_path: str | Path, email: str, password: str) -> int:
    """Crée un compte. Retourne l'id utilisateur. Lève AuthError si invalide."""
    email = email.strip().lower()

    if not EMAIL_RE.match(email):
        raise AuthError("Adresse email invalide.")
    if len(email) > MAX_EMAIL_LENGTH:
        raise AuthError("Adresse email trop longue.")
    if len(password) < 8:
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"Le mot de passe ne peut pas dépasser {MAX_PASSWORD_LENGTH} caractères.")

    password_hash, salt = _hash_password(password)

    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, salt) VALUES (?, ?, ?)",
            (email, password_hash, salt),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        raise AuthError("Un compte existe déjà avec cet email.") from None
    finally:
        conn.close()


def get_user(db_path: str | Path, user_id: int) -> dict | None:
    """Infos publiques d'un compte (id, email, date de création) ; jamais le hash ni le sel."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def authenticate_user(db_path: str | Path, email: str, password: str) -> int:
    """Vérifie les identifiants. Retourne l'id utilisateur si valide, lève AuthError sinon."""
    email = email.strip().lower()
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            # Même coût de calcul que pour un vrai compte : sans cela, une réponse
            # 30 fois plus rapide révèle qu'aucun compte n'existe avec cet email,
            # malgré le message d'erreur identique.
            hashlib.pbkdf2_hmac("sha256", password.encode(), _DUMMY_SALT, PBKDF2_ITERATIONS)
            raise AuthError("Email ou mot de passe incorrect.")

        iterations, expected_digest = _parse_stored_hash(row["password_hash"])
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(row["salt"]), iterations).hex()
        # comparaison en temps constant pour éviter une attaque par mesure de timing
        if not hmac.compare_digest(computed, expected_digest):
            raise AuthError("Email ou mot de passe incorrect.")

        if iterations < PBKDF2_ITERATIONS:
            # mot de passe vérifié : on profite de la connexion pour migrer vers le coût actuel
            new_hash, new_salt = _hash_password(password)
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (new_hash, new_salt, row["id"]))
            conn.commit()

        return row["id"]
    finally:
        conn.close()