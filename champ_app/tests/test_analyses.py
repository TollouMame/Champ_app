from datetime import date

from fastapi.testclient import TestClient

from src import api
from src.demo_data import generate_synthetic_bands


def test_analysis_history_is_persisted_and_limited_to_field_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")

    with TestClient(api.app) as client:
        registration = client.post(
            "/auth/register",
            json={"email": "analyses@example.fr", "password": "motdepasse123"},
        )
        headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
        field = client.post(
            "/fields",
            json={"name": "Parcelle", "crop": "Blé tendre", "bbox": [1.5, 48.3, 1.505, 48.304]},
            headers=headers,
        ).json()

        created = client.post(
            f"/fields/{field['id']}/analyses",
            json={"method": "grid", "n_rows": 4, "n_cols": 6},
            headers=headers,
        )
        assert created.status_code == 200
        assert created.json()["source"] == "synthetic"
        assert created.json()["result"]["grid"] == {"method": "grid", "rows": 4, "cols": 6}

        history = client.get(f"/fields/{field['id']}/analyses", headers=headers)
        assert history.status_code == 200
        assert len(history.json()) == 1
        assert history.json()[0]["id"] == created.json()["id"]


def test_sentinel_analysis_uses_field_bbox_and_persists_source(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    captured = {}

    def fake_fetch_field_bands(bbox, date=None, resolution=10):
        captured.update({"bbox": bbox, "date": date, "resolution": resolution})
        return generate_synthetic_bands(seed=12)

    monkeypatch.setattr(api, "fetch_field_bands", fake_fetch_field_bands)

    with TestClient(api.app) as client:
        registration = client.post(
            "/auth/register",
            json={"email": "sentinel@example.fr", "password": "motdepasse123"},
        )
        headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
        field = client.post(
            "/fields",
            json={"name": "Parcelle Sud", "crop": "Blé tendre", "bbox": [1.5, 48.3, 1.505, 48.304]},
            headers=headers,
        ).json()

        response = client.post(
            f"/fields/{field['id']}/analyses",
            json={"source": "sentinel", "acquisition_date": "2026-07-01", "resolution": 20},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["source"] == "sentinel"
    assert captured == {"bbox": (1.5, 48.3, 1.505, 48.304), "date": date(2026, 7, 1), "resolution": 20}


def test_sentinel_configuration_error_is_not_replaced_with_demo_data(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")

    def fail_fetch(*args, **kwargs):
        raise api.SentinelHubConfigError("identifiants absents")

    monkeypatch.setattr(api, "fetch_field_bands", fail_fetch)

    with TestClient(api.app) as client:
        registration = client.post(
            "/auth/register",
            json={"email": "missing.credentials@example.fr", "password": "motdepasse123"},
        )
        headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
        field = client.post(
            "/fields",
            json={"name": "Parcelle Est", "crop": "Blé tendre", "bbox": [1.5, 48.3, 1.505, 48.304]},
            headers=headers,
        ).json()
        response = client.post(f"/fields/{field['id']}/analyses", json={"source": "sentinel"}, headers=headers)

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "sentinel_configuration_error"
