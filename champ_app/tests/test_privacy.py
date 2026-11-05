import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src import api, privacy

PASSWORD = "motdepasse123"
SECRET_EMAIL = "marie.confidentielle@example.fr"
SECRET_FIELD = "Parcelle-Confidentielle-XYZ"
SECRET_NOTE = "note-traitement-confidentielle-XYZ"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as c:
        yield c


def _register(client, email):
    body = client.post("/auth/register", json={"email": email, "password": PASSWORD}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user_id"]


def _populate(client, headers, name=SECRET_FIELD, polygon=False):
    geometry = {"polygon": [[1.5, 48.3], [1.506, 48.3], [1.506, 48.305], [1.5, 48.305]]} if polygon else {"bbox": [1.5, 48.3, 1.505, 48.304]}
    field_id = client.post("/fields", headers=headers, json={"name": name, "crop": "Orge", "sowing_date": "2026-03-01", **geometry}).json()["id"]
    analysis = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "kmeans"}).json()
    zone_id = analysis["result"]["zones"][0]["id"]
    client.post(f"/fields/{field_id}/treatments", headers=headers, json={"analysis_id": analysis["id"], "zone_id": zone_id, "note": SECRET_NOTE})
    return field_id, analysis["id"]


def _row_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("users", "fields", "analyses", "zone_treatments")}
    finally:
        conn.close()


# ------------------------------------------------------------------ export

def test_export_contains_all_the_users_data_and_nothing_else(client):
    headers, user_id = _register(client, SECRET_EMAIL)
    other_headers, _ = _register(client, "autre@example.fr")
    _populate(client, headers, polygon=True)
    _populate(client, headers, name="Deuxième champ")
    _populate(client, other_headers, name="Champ-Appartenant-A-Un-Autre")

    response = client.get("/account/export", headers=headers)
    assert response.status_code == 200
    assert 'attachment; filename="champ_app_mes_donnees.json"' == response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    data = response.json()
    assert data["account"]["email"] == SECRET_EMAIL and data["account"]["id"] == user_id
    assert [f["name"] for f in data["fields"]] == [SECRET_FIELD, "Deuxième champ"]
    first = data["fields"][0]
    assert first["polygon"] is not None and len(first["bbox"]) == 4
    assert len(first["analyses"]) == 1 and first["analyses"][0]["result"]["zones"]
    assert first["treatments"][0]["note"] == SECRET_NOTE

    dump = response.text
    assert "Champ-Appartenant-A-Un-Autre" not in dump and "autre@example.fr" not in dump
    for forbidden in ("password", "salt", "pbkdf2"):
        assert forbidden not in dump.lower(), forbidden


def test_export_requires_authentication(client):
    assert client.get("/account/export").status_code == 401


def test_export_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv("EXPORT_RATE_LIMIT", "2/minute")
    headers, _ = _register(client, SECRET_EMAIL)
    api.limiter.enabled = True
    api.limiter.reset()
    try:
        assert client.get("/account/export", headers=headers).status_code == 200
        assert client.get("/account/export", headers=headers).status_code == 200
        assert client.get("/account/export", headers=headers).status_code == 429
    finally:
        api.limiter.enabled = False
        api.limiter.reset()


# ------------------------------------------------------------------ suppression du compte

def test_account_deletion_requires_the_correct_password(client, tmp_path):
    headers, _ = _register(client, SECRET_EMAIL)
    _populate(client, headers)
    before = _row_counts(tmp_path / "app.db")

    wrong = client.post("/account/delete", headers=headers, json={"password": "mauvais-mot-de-passe"})
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["error"] == "invalid_password"
    assert _row_counts(tmp_path / "app.db") == before                      # rien n'a été supprimé
    assert client.get("/fields", headers=headers).status_code == 200       # et la session fonctionne toujours

    assert client.post("/account/delete", headers={}, json={"password": PASSWORD}).status_code == 401


def test_account_deletion_removes_everything_and_only_that_account(client, tmp_path):
    headers, _ = _register(client, SECRET_EMAIL)
    other_headers, _ = _register(client, "autre@example.fr")
    _populate(client, headers, polygon=True)
    _populate(client, headers, name="Deuxième champ")
    other_field_id, _ = _populate(client, other_headers, name="Champ de l'autre")

    response = client.post("/account/delete", headers=headers, json={"password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "fields": 2, "analyses": 2, "treatments": 2}

    assert _row_counts(tmp_path / "app.db") == {"users": 1, "fields": 1, "analyses": 1, "zone_treatments": 1}
    assert client.get(f"/fields/{other_field_id}", headers=other_headers).status_code == 200   # l'autre compte est intact


def test_deleted_account_can_no_longer_log_in_or_use_its_old_token(client):
    headers, _ = _register(client, SECRET_EMAIL)
    client.post("/account/delete", headers=headers, json={"password": PASSWORD})

    assert client.post("/auth/login", json={"email": SECRET_EMAIL, "password": PASSWORD}).status_code == 401
    # le token est encore valide cryptographiquement (7 jours) mais le compte n'existe plus
    for method, path, kwargs in (("GET", "/fields", {}), ("GET", "/account/export", {}),
                                 ("POST", "/fields", {"json": {"name": "x", "crop": "Orge", "bbox": [1.5, 48.3, 1.505, 48.304]}})):
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == 401, (method, path)
        assert response.json()["detail"]["error"] == "authentication_error"


def test_email_can_be_reused_after_deletion(client):
    headers, _ = _register(client, SECRET_EMAIL)
    client.post("/account/delete", headers=headers, json={"password": PASSWORD})
    assert client.post("/auth/register", json={"email": SECRET_EMAIL, "password": PASSWORD}).status_code == 200


def test_account_deletion_is_rate_limited_like_login(client, monkeypatch):
    """Sinon cette route servirait à tester des mots de passe sans limite."""
    monkeypatch.setenv("AUTH_RATE_LIMIT", "3/minute")
    headers, _ = _register(client, SECRET_EMAIL)
    api.limiter.enabled = True
    api.limiter.reset()
    try:
        codes = [client.post("/account/delete", headers=headers, json={"password": f"essai-numero-{i}"}).status_code for i in range(5)]
        assert codes[:3] == [403, 403, 403] and codes[3:] == [429, 429]
    finally:
        api.limiter.enabled = False
        api.limiter.reset()


# ------------------------------------------------------------------ effacement réel dans le fichier

def _raw(db_path) -> bytes:
    return db_path.read_bytes()


def test_deleted_data_is_really_gone_from_the_database_file_even_without_secure_delete_default(client, tmp_path, monkeypatch):
    """Selon la façon dont SQLite est compilé, un DELETE peut laisser le contenu lisible dans le fichier.
    On simule une machine où secure_delete est désactivé par défaut : le code doit l'activer lui-même."""
    real_get_connection = privacy.get_connection

    def connection_with_secure_delete_off(path):
        conn = real_get_connection(path)
        conn.execute("PRAGMA secure_delete = OFF")
        return conn

    monkeypatch.setattr(privacy, "get_connection", connection_with_secure_delete_off)

    headers, _ = _register(client, SECRET_EMAIL)
    _populate(client, headers)
    db = tmp_path / "app.db"
    assert SECRET_EMAIL.encode() in _raw(db) and SECRET_FIELD.encode() in _raw(db)   # présent avant (le test est significatif)

    assert client.post("/account/delete", headers=headers, json={"password": PASSWORD}).status_code == 200
    raw = _raw(db)
    for secret in (SECRET_EMAIL, SECRET_FIELD, SECRET_NOTE):
        assert secret.encode() not in raw, f"{secret!r} est encore lisible dans le fichier de base de données"


def test_field_deletion_removes_field_analyses_and_treatments(client, tmp_path):
    headers, _ = _register(client, SECRET_EMAIL)
    other_headers, _ = _register(client, "autre@example.fr")
    field_id, _ = _populate(client, headers)
    other_field_id, _ = _populate(client, other_headers, name="Champ de l'autre")

    assert client.delete(f"/fields/{field_id}", headers=other_headers).status_code == 404      # pas le sien
    assert _row_counts(tmp_path / "app.db")["fields"] == 2

    response = client.delete(f"/fields/{field_id}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "field_id": field_id, "analyses": 1, "treatments": 1}
    assert _row_counts(tmp_path / "app.db") == {"users": 2, "fields": 1, "analyses": 1, "zone_treatments": 1}
    assert SECRET_FIELD.encode() not in _raw(tmp_path / "app.db")
    assert client.get(f"/fields/{field_id}", headers=headers).status_code == 404
    assert client.delete(f"/fields/{field_id}", headers=headers).status_code == 404            # déjà supprimé
    assert client.get(f"/fields/{other_field_id}", headers=other_headers).status_code == 200


def test_field_deletion_requires_authentication(client):
    assert client.delete("/fields/1").status_code == 401


# ------------------------------------------------------------------ logs sans email en clair

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_failed_login_and_signup_logs_do_not_contain_the_plain_email(client):
    capture = _Capture()
    api.logger.addHandler(capture)
    try:
        client.post("/auth/login", json={"email": "Victime@Example.fr", "password": "mauvais-mdp-123"})
        _register(client, "dejapris@example.fr")
        client.post("/auth/register", json={"email": "dejapris@example.fr", "password": PASSWORD})   # doublon -> échec journalisé
    finally:
        api.logger.removeHandler(capture)

    failures = [r for r in capture.records if r.getMessage() in ("échec de connexion", "échec d'inscription")]
    assert len(failures) == 2
    for record in failures:
        assert not hasattr(record, "email")
        assert len(record.email_hash) == 16
        assert "example.fr" not in json.dumps(record.__dict__, default=str).lower()


def test_pseudonym_is_stable_case_insensitive_and_not_a_plain_hash(monkeypatch):
    import hashlib

    a = privacy.pseudonymize_email("Marie@Example.fr")
    assert a == privacy.pseudonymize_email("  marie@example.fr ")
    assert a != privacy.pseudonymize_email("pierre@example.fr")
    assert a != hashlib.sha256(b"marie@example.fr").hexdigest()[:16]     # sinon retrouvable par dictionnaire
    monkeypatch.setenv("JWT_SECRET_KEY", "z" * 48)
    assert privacy.pseudonymize_email("marie@example.fr") != a            # dépend du secret serveur
