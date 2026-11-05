import math
import time

import pytest
from fastapi.testclient import TestClient

from src import api
from src.fields import MAX_POLYGON_VERTICES


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as c:
        token = c.post("/auth/register", json={"email": "limites@example.fr", "password": "motdepasse123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _circle(n, radius=0.002):
    return [[1.5 + radius * math.cos(2 * math.pi * i / n), 48.3 + radius * math.sin(2 * math.pi * i / n)] for i in range(n)]


def _field(client, **geometry):
    return client.post("/fields", json={"name": "P", "crop": "Orge", **geometry})


def _small_field_id(client):
    return _field(client, bbox=[1.5, 48.3, 1.505, 48.304]).json()["id"]


# ---------- parcelles ----------

def test_polygon_with_too_many_vertices_is_rejected_quickly(client):
    t = time.time()
    response = _field(client, polygon=_circle(MAX_POLYGON_VERTICES + 1))
    assert time.time() - t < 1.0                      # refusé avant la validation en O(n²)
    assert response.status_code in (400, 422)
    assert "sommets" in response.text


def test_polygon_at_the_vertex_limit_is_accepted(client):
    t = time.time()
    assert _field(client, polygon=_circle(MAX_POLYGON_VERTICES)).status_code == 200
    assert time.time() - t < 1.5


def test_huge_bbox_is_rejected(client):
    response = _field(client, bbox=[-5, 42, 5, 50])   # ~ 69 millions d'hectares avant correctif
    assert response.status_code in (400, 422)
    assert "trop grand" in response.text


def test_sliver_polygon_with_huge_bounding_box_is_rejected(client):
    # surface minuscule mais rectangle englobant d'environ 8 000 ha : c'est lui qu'on enverrait au satellite
    sliver = [[1.50, 48.30], [1.60, 48.40], [1.5005, 48.30]]
    response = _field(client, polygon=sliver)
    assert response.status_code in (400, 422)
    assert "englobant" in response.text


# ---------- analyses ----------

def test_resolution_below_native_sentinel_resolution_is_rejected(client):
    field_id = _small_field_id(client)
    low = client.post(f"/fields/{field_id}/analyses", json={"resolution": 1})
    assert low.status_code == 422 and "inférieure" in low.text
    assert client.post(f"/fields/{field_id}/analyses", json={"resolution": 10}).status_code == 200


def test_analysis_quota_per_field(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_ANALYSES_PER_FIELD", 2)
    field_id = _small_field_id(client)
    url = f"/fields/{field_id}/analyses"
    assert client.post(url, json={}).status_code == 200
    assert client.post(url, json={}).status_code == 200
    blocked = client.post(url, json={})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["error"] == "analysis_quota_exceeded"
    assert len(client.get(url).json()) == 2           # l'historique existant reste consultable


def test_analysis_rate_limit(client, monkeypatch):
    monkeypatch.setenv("ANALYSIS_RATE_LIMIT", "2/minute")
    field_id = _small_field_id(client)
    url = f"/fields/{field_id}/analyses"
    api.limiter.enabled = True
    api.limiter.reset()
    try:
        assert client.post(url, json={}).status_code == 200
        assert client.post(url, json={}).status_code == 200
        assert client.post(url, json={}).status_code == 429
    finally:
        api.limiter.enabled = False
        api.limiter.reset()


def test_analyses_list_is_paginated_newest_first(client):
    field_id = _small_field_id(client)
    url = f"/fields/{field_id}/analyses"
    ids = [client.post(url, json={}).json()["id"] for _ in range(5)]

    assert [a["id"] for a in client.get(url, params={"limit": 2}).json()] == [ids[4], ids[3]]
    assert [a["id"] for a in client.get(url, params={"limit": 2, "offset": 2}).json()] == [ids[2], ids[1]]
    assert len(client.get(url).json()) == 5           # limite par défaut (20) > 5
    assert client.get(url, params={"limit": 0}).status_code == 422
    assert client.get(url, params={"limit": 101}).status_code == 422


def test_sentinel_failure_does_not_leak_exception_text(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret-interne-https://services.example/token=abc123")

    monkeypatch.setattr(api, "fetch_field_bands", boom)
    field_id = _small_field_id(client)
    response = client.post(f"/fields/{field_id}/analyses", json={"source": "sentinel"})
    assert response.status_code == 502
    assert "abc123" not in response.text and "secret-interne" not in response.text


# ---------- corps de requête ----------

def test_oversized_request_body_is_rejected_before_parsing(client, monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "2000")
    t = time.time()
    response = client.post("/fields", content=b'{"name":"' + b"x" * 5000 + b'"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "payload_too_large"
    assert time.time() - t < 0.5

    assert _field(client, bbox=[1.5, 48.3, 1.505, 48.304]).status_code == 200   # une requête normale passe
