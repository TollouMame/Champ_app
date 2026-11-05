"""
Émission et vérification de tokens JWT pour les sessions API.

Le secret de signature est lu dans la variable d'environnement JWT_SECRET_KEY.
Si elle est absente, le code échoue (RuntimeError), SAUF si ENV est
explicitement défini à "development" (ou dev/local/test) : un secret de
développement public est alors utilisé. Oublier de définir ENV en production
ne peut donc plus conduire à signer des tokens avec un secret connu de tous.
"""

import os
import datetime as dt

import jwt

from src.config import load_environment

load_environment()

DEV_ENVIRONMENTS = {"dev", "development", "local", "test"}
DEV_ONLY_SECRET = "dev-only-secret-do-not-use-in-production"
MIN_SECRET_LENGTH = 32  # taille de la sortie de SHA-256 (RFC 7518 §3.2) ; en dessous, la clé se devine plus facilement
ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24 * 7  # 7 jours


def is_production_environment() -> bool:
    return os.environ.get("ENV", "development").strip().lower() in {"prod", "production", "true"}


def is_development_environment() -> bool:
    return os.environ.get("ENV", "").strip().lower() in DEV_ENVIRONMENTS


def get_secret_key() -> str:
    secret_key = os.environ.get("JWT_SECRET_KEY")
    if secret_key:
        if len(secret_key) < MIN_SECRET_LENGTH and not is_development_environment():
            raise RuntimeError(
                f"JWT_SECRET_KEY est trop court ({len(secret_key)} caractères, minimum {MIN_SECRET_LENGTH}). "
                'Générez-en un : python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return secret_key
    if is_development_environment():
        return DEV_ONLY_SECRET
    raise RuntimeError(
        "JWT_SECRET_KEY doit être défini. Pour le développement local uniquement, "
        "définissez ENV=development (voir .env.example)."
    )


class TokenError(Exception):
    """Token manquant, invalide, ou expiré."""


def create_access_token(user_id: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + dt.timedelta(hours=TOKEN_EXPIRY_HOURS),
    }
    return jwt.encode(payload, get_secret_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> int:
    """Retourne le user_id encodé dans le token. Lève TokenError si invalide/expiré."""
    try:
        # algorithms=[...] est obligatoire : il empêche qu'un token choisisse lui-même
        # son algorithme (ex. "none"). exp et sub sont exigés : sans "exp", un token
        # volé serait valable indéfiniment.
        payload = jwt.decode(
            token,
            get_secret_key(),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:  # signature, expiration, format, claim manquant...
        raise TokenError("Token invalide ou expiré.") from exc

    try:
        return int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise TokenError("Token mal formé.") from exc
