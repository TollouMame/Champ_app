import pytest
from fastapi.testclient import TestClient

from src import api


def test_health_endpoint_reports_service_status(monkeypatch, tmp_path):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(api, "DB_PATH", db_path)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with TestClient(api.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload == {"status": "ok", "database": "ready"}
        assert str(db_path) not in response.text  # plus de chemin de fichier ni d'environnement


def test_startup_fails_when_production_secret_is_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(api, "DB_PATH", db_path)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        with TestClient(api.app):
            pass


def test_startup_fails_when_env_is_unset_and_secret_is_missing(monkeypatch, tmp_path):
    """Régression : oublier ENV ne doit plus activer le secret de développement."""
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(api, "DB_PATH", db_path)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        with TestClient(api.app):
            pass


def test_get_allowed_origins_parses_environment(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com, https://admin.example.com")
    assert api.get_allowed_origins() == ["https://app.example.com", "https://admin.example.com"]


def test_get_allowed_origins_defaults_to_safe_local_origins(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    assert api.get_allowed_origins() == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]


def test_get_db_path_uses_environment_override(monkeypatch, tmp_path):
    db_path = tmp_path / "custom.db"
    monkeypatch.setenv("CHAMP_DB_PATH", str(db_path))
    assert api.get_db_path() == db_path


def test_init_db_is_idempotent(tmp_path):
    from src.db import init_db

    db_path = tmp_path / "app.db"
    init_db(db_path)
    init_db(db_path)

    assert db_path.exists()
