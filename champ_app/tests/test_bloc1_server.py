import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src import api

PASSWORD = "motdepasse123"
POLYGON = [[1.500, 48.300], [1.508, 48.300], [1.508, 48.304], [1.504, 48.306], [1.500, 48.304]]   # pentagone


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as c:
        yield c


def _user(client, email="bloc1@example.fr"):
    token = client.post("/auth/register", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _field(client, headers, **geometry):
    geometry = geometry or {"bbox": [1.5, 48.3, 1.505, 48.304]}
    return client.post("/fields", headers=headers, json={"name": "P", "crop": "Orge", **geometry}).json()["id"]


# ----------------------------------------------------------------- « Actualiser » ne duplique plus

def test_repeated_refresh_on_the_same_day_reuses_the_analysis(client):
    headers = _user(client)
    field_id = _field(client, headers)
    url = f"/fields/{field_id}/analyses"

    first = client.post(url, headers=headers, json={"method": "kmeans"}).json()
    second = client.post(url, headers=headers, json={"method": "kmeans"}).json()
    assert first["reused"] is False and second["reused"] is True
    assert second["id"] == first["id"]
    assert len(client.get(url, headers=headers).json()) == 1               # plus de doublons


def test_demo_data_changes_from_one_day_to_the_next_like_real_imagery(client, monkeypatch):
    headers = _user(client)
    field_id = _field(client, headers)
    url = f"/fields/{field_id}/analyses"

    monkeypatch.setattr(api, "_today_utc", lambda: dt.date(2026, 11, 5))
    today = client.post(url, headers=headers, json={"method": "kmeans"}).json()
    monkeypatch.setattr(api, "_today_utc", lambda: dt.date(2026, 11, 6))
    tomorrow = client.post(url, headers=headers, json={"method": "kmeans"}).json()

    assert tomorrow["reused"] is False and tomorrow["id"] != today["id"]
    assert today["parameters"]["acquisition_date"] == "2026-11-05" and tomorrow["parameters"]["acquisition_date"] == "2026-11-06"
    assert today["result"]["zones"] != tomorrow["result"]["zones"]          # les valeurs de NDVI diffèrent


def test_different_parameters_create_a_new_analysis(client):
    headers = _user(client)
    field_id = _field(client, headers)
    url = f"/fields/{field_id}/analyses"
    ids = {client.post(url, headers=headers, json=body).json()["id"] for body in
           ({"method": "kmeans"}, {"method": "grid"}, {"method": "kmeans", "n_zones": 4}, {"method": "kmeans", "critical_threshold": 0.3})}
    assert len(ids) == 4


def test_reused_analysis_does_not_consume_the_quota(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_ANALYSES_PER_FIELD", 2)
    headers = _user(client)
    field_id = _field(client, headers)
    url = f"/fields/{field_id}/analyses"
    client.post(url, headers=headers, json={"acquisition_date": "2026-03-01"})
    client.post(url, headers=headers, json={"acquisition_date": "2026-03-02"})
    # quota atteint, mais redemander une analyse existante reste possible
    assert client.post(url, headers=headers, json={"acquisition_date": "2026-03-02"}).json()["reused"] is True
    assert client.post(url, headers=headers, json={"acquisition_date": "2026-03-03"}).status_code == 429


def test_quota_is_no_longer_a_dead_end_analyses_can_be_deleted(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_ANALYSES_PER_FIELD", 1)
    headers = _user(client)
    field_id = _field(client, headers)
    url = f"/fields/{field_id}/analyses"
    first = client.post(url, headers=headers, json={"acquisition_date": "2026-03-01"}).json()
    blocked = client.post(url, headers=headers, json={"acquisition_date": "2026-03-02"})
    assert blocked.status_code == 429 and "DELETE" in blocked.json()["detail"]["message"]

    assert client.delete(f"{url}/{first['id']}", headers=headers).json() == {"deleted": True, "analysis_id": first["id"], "treatments": 0}
    assert client.post(url, headers=headers, json={"acquisition_date": "2026-03-02"}).status_code == 200


def test_deleting_an_analysis_removes_its_treatments_and_respects_ownership(client, tmp_path):
    headers, other = _user(client), _user(client, "autre@example.fr")
    field_id = _field(client, headers)
    analysis = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "kmeans"}).json()
    zone_id = analysis["result"]["zones"][0]["id"]
    client.post(f"/fields/{field_id}/treatments", headers=headers, json={"analysis_id": analysis["id"], "zone_id": zone_id})

    url = f"/fields/{field_id}/analyses/{analysis['id']}"
    assert client.delete(url, headers=other).status_code == 404            # pas à lui
    assert client.delete(url).status_code == 401
    assert client.delete(f"/fields/{field_id}/analyses/9999", headers=headers).status_code == 404

    assert client.delete(url, headers=headers).json()["treatments"] == 1
    conn = sqlite3.connect(tmp_path / "app.db")
    assert conn.execute("SELECT COUNT(*) FROM zone_treatments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0
    conn.close()


# ----------------------------------------------------------------- traitements

def _grid_analysis_with_polygon(client, headers):
    field_id = _field(client, headers, polygon=POLYGON)
    analysis = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "grid"}).json()
    return field_id, analysis


def test_marking_the_same_zone_twice_creates_no_duplicate(client):
    headers = _user(client)
    field_id = _field(client, headers)
    analysis = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "kmeans"}).json()
    body = {"analysis_id": analysis["id"], "zone_id": analysis["result"]["zones"][0]["id"]}

    first = client.post(f"/fields/{field_id}/treatments", headers=headers, json=body).json()
    second = client.post(f"/fields/{field_id}/treatments", headers=headers, json=body).json()
    assert first["already_recorded"] is False and second["already_recorded"] is True
    assert second["id"] == first["id"]
    assert len(client.get(f"/fields/{field_id}/treatments", headers=headers).json()) == 1

    other_zone = {"analysis_id": analysis["id"], "zone_id": analysis["result"]["zones"][1]["id"]}
    assert client.post(f"/fields/{field_id}/treatments", headers=headers, json=other_zone).json()["already_recorded"] is False
    assert len(client.get(f"/fields/{field_id}/treatments", headers=headers).json()) == 2


def test_a_zone_outside_the_parcel_cannot_be_marked_as_treated(client):
    headers = _user(client)
    field_id, analysis = _grid_analysis_with_polygon(client, headers)
    no_data = [z["id"] for z in analysis["result"]["zones"] if z["status"] == "no_data"]
    usable = [z["id"] for z in analysis["result"]["zones"] if z["status"] != "no_data"]
    assert no_data and usable                                              # le pentagone laisse des cases hors parcelle

    refused = client.post(f"/fields/{field_id}/treatments", headers=headers, json={"analysis_id": analysis["id"], "zone_id": no_data[0]})
    assert refused.status_code == 400 and "hors de la parcelle" in refused.json()["detail"]["message"]
    assert client.post(f"/fields/{field_id}/treatments", headers=headers, json={"analysis_id": analysis["id"], "zone_id": usable[0]}).status_code == 200


# ----------------------------------------------------------------- racine, schéma Bearer

def test_root_redirects_to_the_app_instead_of_a_json_404(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/onboarding_mobile.html"
    assert client.get("/", follow_redirects=True).status_code == 200


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_authorization_scheme_is_case_insensitive(client, scheme):
    token = client.post("/auth/register", json={"email": f"{scheme}@example.fr", "password": PASSWORD}).json()["access_token"]
    assert client.get("/fields", headers={"Authorization": f"{scheme} {token}"}).status_code == 200
    assert client.get("/fields", headers={"Authorization": f"Token {token}"}).status_code == 401


# ----------------------------------------------------------------- limite de débit par utilisateur

def test_analysis_rate_limit_is_per_user_across_all_their_fields(client, monkeypatch):
    """Tous les clients de ce test viennent de la même adresse IP (comme derrière un reverse proxy).

    Deux défauts à détecter : une limite « par IP » (Bob serait bloqué par Alice) et un compteur
    « par URL » de slowapi, donc par champ (Alice contournerait la limite en changeant de champ).
    """
    monkeypatch.setenv("ANALYSIS_RATE_LIMIT", "2/minute")
    alice, bob = _user(client, "alice@example.fr"), _user(client, "bob@example.fr")
    a1, a2, b1 = _field(client, alice), _field(client, alice), _field(client, bob)
    api.limiter.enabled = True
    api.limiter.reset()
    try:
        def post(headers, field, day):
            return client.post(f"/fields/{field}/analyses", headers=headers, json={"acquisition_date": f"2026-03-0{day}"}).status_code
        codes_alice = [post(alice, a1, 1), post(alice, a2, 1), post(alice, a1, 2), post(alice, a2, 2)]   # 2 champs, 1 seul compteur
        codes_bob = [post(bob, b1, 1), post(bob, b1, 2), post(bob, b1, 3)]
    finally:
        api.limiter.enabled = False
        api.limiter.reset()
    assert codes_alice == [200, 200, 429, 429]     # changer de champ ne contourne pas la limite
    assert codes_bob == [200, 200, 429]            # et Bob a sa propre limite : Alice ne l'a pas consommée


def test_rate_limit_key_falls_back_to_the_ip_without_a_valid_token():
    from starlette.requests import Request

    def request_with(headers):
        scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": ("203.0.113.9", 1234)}
        return Request(scope)

    assert api.user_or_ip_key(request_with({})) == "203.0.113.9"
    assert api.user_or_ip_key(request_with({"Authorization": "Bearer pas-un-token"})) == "203.0.113.9"
