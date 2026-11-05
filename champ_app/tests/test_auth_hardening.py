import hashlib
import sqlite3

import pytest

from src import auth
from src.auth import AuthError, authenticate_user, register_user
from src.db import init_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "app.db"
    init_db(path)
    return path


def _stored(db, email):
    row = sqlite3.connect(db).execute("SELECT password_hash, salt FROM users WHERE email = ?", (email,)).fetchone()
    return row


def test_new_accounts_use_current_cost_and_self_describing_format(db):
    register_user(db, "neuf@example.fr", "motdepasse123")
    stored, _ = _stored(db, "neuf@example.fr")
    assert stored.startswith(f"pbkdf2_sha256${auth.PBKDF2_ITERATIONS}$")
    assert authenticate_user(db, "neuf@example.fr", "motdepasse123") > 0
    with pytest.raises(AuthError):
        authenticate_user(db, "neuf@example.fr", "mauvais-mot-de-passe")


def _insert_legacy_account(db, email, password):
    """Compte créé avant la migration : hash hexadécimal brut, 260 000 itérations."""
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000).hex()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users (email, password_hash, salt) VALUES (?, ?, ?)", (email, digest, salt.hex()))
    conn.commit()
    conn.close()


def test_legacy_account_still_logs_in_and_is_upgraded_transparently(db):
    _insert_legacy_account(db, "ancien@example.fr", "motdepasse123")
    old_hash, old_salt = _stored(db, "ancien@example.fr")
    assert "$" not in old_hash

    user_id = authenticate_user(db, "ancien@example.fr", "motdepasse123")
    new_hash, new_salt = _stored(db, "ancien@example.fr")
    assert new_hash.startswith(f"pbkdf2_sha256${auth.PBKDF2_ITERATIONS}$")
    assert new_salt != old_salt                                    # nouveau sel aussi

    assert authenticate_user(db, "ancien@example.fr", "motdepasse123") == user_id   # et ça marche après migration
    with pytest.raises(AuthError):
        authenticate_user(db, "ancien@example.fr", "mauvais-mot-de-passe")


def test_wrong_password_on_legacy_account_does_not_upgrade_or_log_in(db):
    _insert_legacy_account(db, "ancien@example.fr", "motdepasse123")
    before = _stored(db, "ancien@example.fr")
    with pytest.raises(AuthError):
        authenticate_user(db, "ancien@example.fr", "pas-le-bon-mdp")
    assert _stored(db, "ancien@example.fr") == before


def test_unknown_email_costs_the_same_hashing_work_as_a_real_account(db, monkeypatch):
    """Régression : un email inconnu répondait en ~2 ms contre ~70 ms, ce qui révélait les comptes existants."""
    register_user(db, "existe@example.fr", "motdepasse123")
    calls = []
    real = hashlib.pbkdf2_hmac
    monkeypatch.setattr(auth.hashlib, "pbkdf2_hmac", lambda *a, **k: calls.append(a[3]) or real(*a, **k))

    with pytest.raises(AuthError):
        authenticate_user(db, "inconnu@example.fr", "motdepasse123")
    unknown = list(calls)
    calls.clear()
    with pytest.raises(AuthError):
        authenticate_user(db, "existe@example.fr", "mauvais-mot-de-passe")

    assert unknown == [auth.PBKDF2_ITERATIONS]       # un calcul complet pour l'inconnu
    assert calls == [auth.PBKDF2_ITERATIONS]         # même coût pour un compte existant


def test_error_message_is_identical_for_unknown_email_and_wrong_password(db):
    register_user(db, "existe@example.fr", "motdepasse123")
    with pytest.raises(AuthError) as unknown:
        authenticate_user(db, "inconnu@example.fr", "motdepasse123")
    with pytest.raises(AuthError) as wrong:
        authenticate_user(db, "existe@example.fr", "mauvais-mot-de-passe")
    assert str(unknown.value) == str(wrong.value)
