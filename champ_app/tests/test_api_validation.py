from fastapi.testclient import TestClient

from src import api


def test_invalid_email_returns_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        response = client.post(
            "/auth/register",
            json={"email": "not-an-email", "password": "motdepasse123"},
        )

    assert response.status_code == 422
    payload = response.json()["detail"]
    assert payload["error"] == "validation_error"
    assert payload["detail"] == "Adresse email invalide."
    assert payload["message"] == "Adresse email invalide."


def test_invalid_sowing_date_returns_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        register_response = client.post(
            "/auth/register",
            json={"email": "field.user@example.fr", "password": "motdepasse123"},
        )
        assert register_response.status_code == 200
        token = register_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post(
            "/fields",
            json={
                "name": "Parcelle",
                "crop": "Blé tendre",
                "bbox": [1.500, 48.300, 1.505, 48.304],
                "sowing_date": "2026/07/11",
            },
            headers=headers,
        )

    assert response.status_code == 400
    payload = response.json()["detail"]
    assert payload["error"] == "validation_error"
    assert payload["detail"] == "Format de date de semis invalide."
    assert payload["message"] == "Format de date de semis invalide."


def test_auth_response_exposes_token_lifetime(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        response = client.post(
            "/auth/register",
            json={"email": "auth.contract@example.fr", "password": "motdepasse123"},
        )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["expires_in_seconds"] == 7 * 24 * 60 * 60


def test_zones_endpoint_accepts_and_forwards_query_parameters(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    captured = {}

    def fake_run_pipeline(bands, n_rows=8, n_cols=12, method="grid", n_zones=6, thresholds=None):
        captured.update({"n_rows": n_rows, "n_cols": n_cols, "method": method, "n_zones": n_zones, "thresholds": thresholds})
        return {
            "thresholds_used": {},
            "grid": {"method": method, "rows": n_rows, "cols": n_cols},
            "zones": [],
            "alert_count": 0,
            "alert_zone_ids": [],
            "field_mean_ndvi": 0.0,
            "label_grid": None,
        }, {}

    monkeypatch.setattr(api, "run_pipeline", fake_run_pipeline)

    with TestClient(api.app) as client:
        register_response = client.post(
            "/auth/register",
            json={"email": "zones.user@example.fr", "password": "motdepasse123"},
        )
        assert register_response.status_code == 200
        token = register_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        field_response = client.post(
            "/fields",
            json={"name": "Parcelle", "crop": "Blé tendre", "bbox": [1.500, 48.300, 1.505, 48.304]},
            headers=headers,
        )
        assert field_response.status_code == 200
        field_id = field_response.json()["id"]

        response = client.get(
            f"/fields/{field_id}/zones?method=grid&n_rows=4&n_cols=6&n_zones=3&critical_threshold=0.2&warning_threshold=0.4",
            headers=headers,
        )

    assert response.status_code == 200
    assert captured == {"n_rows": 4, "n_cols": 6, "method": "grid", "n_zones": 3, "thresholds": {"critical": 0.2, "warning": 0.4}}
