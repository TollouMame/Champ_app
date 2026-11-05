from fastapi.testclient import TestClient

from src import api


def test_treatment_is_linked_to_its_field_analysis_and_hidden_from_others(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")

    with TestClient(api.app) as client:
        owner = client.post(
            "/auth/register", json={"email": "owner@example.fr", "password": "motdepasse123"},
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        field = client.post(
            "/fields",
            json={"name": "Parcelle", "crop": "Blé tendre", "bbox": [1.5, 48.3, 1.505, 48.304]},
            headers=owner_headers,
        ).json()
        analysis = client.post(
            f"/fields/{field['id']}/analyses",
            json={"method": "kmeans"},
            headers=owner_headers,
        ).json()

        created = client.post(
            f"/fields/{field['id']}/treatments",
            json={"analysis_id": analysis["id"], "zone_id": analysis["result"]["zones"][0]["id"], "note": "Irrigation vérifiée"},
            headers=owner_headers,
        )
        assert created.status_code == 200
        assert created.json()["zone_id"] == analysis["result"]["zones"][0]["id"]
        assert created.json()["analysis_id"] == analysis["id"]

        history = client.get(f"/fields/{field['id']}/treatments", headers=owner_headers)
        assert len(history.json()) == 1

        other = client.post(
            "/auth/register", json={"email": "other@example.fr", "password": "motdepasse456"},
        ).json()
        response = client.get(
            f"/fields/{field['id']}/treatments",
            headers={"Authorization": f"Bearer {other['access_token']}"},
        )
        assert response.status_code == 404