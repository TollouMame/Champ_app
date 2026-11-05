import importlib

import pytest
from fastapi.testclient import TestClient

from src import api
from src.tokens import TokenError, create_access_token, decode_access_token


def test_create_and_decode_access_token(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    import src.tokens as tokens

    importlib.reload(tokens)

    token = tokens.create_access_token(42)
    assert tokens.decode_access_token(token) == 42


def test_invalid_token_is_rejected_by_api(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    import src.tokens as tokens

    importlib.reload(tokens)

    with TestClient(api.app) as client:
        response = client.get("/fields", headers={"Authorization": "Bearer not-a-valid-token"})

    assert response.status_code == 401
    payload = response.json()["detail"]
    assert payload["error"] == "authentication_error"
    assert payload["detail"] == "Token invalide ou expiré."
    assert payload["message"] == "Token invalide ou expiré."


# ---------- durcissement (migration python-jose -> PyJWT) ----------

import base64
import datetime as dt
import json

import jwt as pyjwt

from src import tokens as tokens_module


def _forge(payload: dict, key: str = "test-secret-pour-la-suite-de-tests-0123456789", algorithm: str = "HS256") -> str:
    return pyjwt.encode(payload, key, algorithm=algorithm)


def _in(hours: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    expired = _forge({"sub": "1", "exp": _in(-1)}, key="x" * 40)
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(expired)


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(_forge({"sub": "1", "exp": _in(1)}, key="y" * 40))


def test_token_without_expiry_is_rejected(monkeypatch):
    """Un token sans 'exp' serait valable indéfiniment : on l'exige."""
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(_forge({"sub": "1"}, key="x" * 40))


def test_unsigned_alg_none_token_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    unsigned = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'exp': int(_in(1).timestamp())})}."
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(unsigned)


def test_token_with_another_hmac_algorithm_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(_forge({"sub": "1", "exp": _in(1)}, key="x" * 40, algorithm="HS512"))


@pytest.mark.parametrize("sub", ["abc", "", None, "1.5"])
def test_token_with_non_integer_subject_is_rejected_cleanly(monkeypatch, sub):
    """Avant : int(sub) levait ValueError -> erreur 500 au lieu de 401."""
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    payload = {"exp": _in(1)}
    if sub is not None:
        payload["sub"] = sub
    with pytest.raises(tokens_module.TokenError):
        decode_access_token(_forge(payload, key="x" * 40))


def test_issued_token_carries_issue_and_expiry_claims(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    claims = pyjwt.decode(create_access_token(7), "x" * 40, algorithms=["HS256"])
    assert claims["sub"] == "7"
    assert claims["exp"] - claims["iat"] == tokens_module.TOKEN_EXPIRY_HOURS * 3600


def test_python_jose_is_no_longer_a_dependency():
    from pathlib import Path

    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text().lower()
    assert "python-jose" not in requirements and "pyjwt" in requirements


def test_short_secret_is_refused_outside_development(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "trop-court")
    with pytest.raises(RuntimeError, match="trop court"):
        tokens_module.get_secret_key()

    monkeypatch.setenv("JWT_SECRET_KEY", "x" * tokens_module.MIN_SECRET_LENGTH)
    assert tokens_module.get_secret_key() == "x" * tokens_module.MIN_SECRET_LENGTH

    monkeypatch.setenv("ENV", "development")             # en développement local, un secret court reste toléré
    monkeypatch.setenv("JWT_SECRET_KEY", "court")
    assert tokens_module.get_secret_key() == "court"
