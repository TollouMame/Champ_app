"""Tests de non-régression pour les bugs identifiés lors de l'audit :

1. zone_id() produisait des identifiants non alphabétiques au-delà de 26 lignes.
2. n_rows/n_cols/n_zones/resolution n'avaient pas de borne haute -> risque de DoS.
3. Le mode kmeans plantait (exception non gérée) sur un raster entièrement masqué.
4. Le mode grid renvoyait un JSON invalide (NaN littéral) dans le même cas.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src import api
from src.main import run_pipeline
from src.zoning import zone_id


# ---------- 1. zone_id au-delà de 26 lignes ----------

def test_zone_id_stays_alphabetic_beyond_26_rows():
    assert zone_id(0, 0) == "A1"
    assert zone_id(25, 0) == "Z1"
    assert zone_id(26, 0) == "AA1"
    assert zone_id(27, 0) == "AB1"
    assert zone_id(51, 0) == "AZ1"
    assert zone_id(52, 0) == "BA1"


def test_zone_id_never_contains_non_alphanumeric_characters():
    for row in range(0, 60):
        generated = zone_id(row, 3)
        letters, digits = generated[:-1], generated[-1]
        assert letters.isalpha() and letters.isupper()
        assert digits.isdigit()


# ---------- 2. bornes hautes sur les paramètres de zonage ----------

def _register_and_get_headers(client, email):
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "motdepasse123"},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_field(client, headers):
    response = client.post(
        "/fields",
        json={"name": "Parcelle", "crop": "Blé tendre", "bbox": [1.500, 48.300, 1.505, 48.304]},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_analyses_endpoint_rejects_oversized_grid(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        headers = _register_and_get_headers(client, "grid.user@example.fr")
        field_id = _create_field(client, headers)

        response = client.post(
            f"/fields/{field_id}/analyses",
            json={"n_rows": 100000, "n_cols": 8},
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_analyses_endpoint_rejects_oversized_n_zones(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        headers = _register_and_get_headers(client, "nzones.user@example.fr")
        field_id = _create_field(client, headers)

        response = client.post(
            f"/fields/{field_id}/analyses",
            json={"n_zones": 500},
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_zones_endpoint_rejects_oversized_grid(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    with TestClient(api.app) as client:
        headers = _register_and_get_headers(client, "legacy.user@example.fr")
        field_id = _create_field(client, headers)

        response = client.get(
            f"/fields/{field_id}/zones?n_rows=1000&n_cols=1000",
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


# ---------- 3 & 4. raster entièrement masqué (ex : couverture nuageuse totale) ----------

def _fully_masked_bands():
    bands = np.zeros((20, 20, 4), dtype=np.float32)
    bands[:, :, 3] = 0  # dataMask = 0 partout -> aucun pixel valide
    return bands


def test_kmeans_pipeline_raises_clear_error_on_fully_masked_raster():
    with pytest.raises(ValueError, match="Aucun pixel valide"):
        run_pipeline(_fully_masked_bands(), method="kmeans", n_zones=6)


def test_grid_pipeline_returns_none_instead_of_nan_on_fully_masked_raster():
    result, _ = run_pipeline(_fully_masked_bands(), method="grid", n_rows=4, n_cols=4)
    assert result["field_mean_ndvi"] is None


def test_analyses_endpoint_returns_clean_422_on_unusable_image(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(api, "generate_synthetic_bands", lambda seed: _fully_masked_bands())

    with TestClient(api.app) as client:
        headers = _register_and_get_headers(client, "masked.user@example.fr")
        field_id = _create_field(client, headers)

        response = client.post(
            f"/fields/{field_id}/analyses",
            json={"method": "kmeans", "n_zones": 6},
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "analysis_error"


# ---------- 5. rate limiting sur les routes d'authentification ----------

def test_login_is_rate_limited_after_too_many_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-pour-la-suite-de-tests-0123456789")
    monkeypatch.setenv("AUTH_RATE_LIMIT", "3/minute")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")

    api.limiter.enabled = True
    api.limiter.reset()
    try:
        with TestClient(api.app) as client:
            client.post(
                "/auth/register",
                json={"email": "ratelimit.user@example.fr", "password": "motdepasse123"},
            )

            statuses = [
                client.post(
                    "/auth/login",
                    json={"email": "ratelimit.user@example.fr", "password": "wrong-password"},
                ).status_code
                for _ in range(5)
            ]
    finally:
        api.limiter.enabled = False
        api.limiter.reset()

    # les premières tentatives échouent normalement (401), au-delà de la limite -> 429
    assert 429 in statuses
    assert statuses.count(429) >= 1