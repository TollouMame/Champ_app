import math
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src import api
from src.analyses import list_analyses, save_analysis
from src.db import init_db
from src.main import run_pipeline
from src.demo_data import generate_synthetic_bands
from src.zoning import get_thresholds


@pytest.mark.parametrize("overrides", [
    {"critical": float("nan")},
    {"warning": float("inf")},
    {"critical": -5, "warning": -4},          # hors plage NDVI
    {"critical": 0.8, "warning": 0.2},        # incohérent
    {"critical": 0.55},                       # égal au seuil warning par défaut
])
def test_get_thresholds_rejects_unusable_values(overrides):
    with pytest.raises(ValueError):
        get_thresholds(overrides)


def test_get_thresholds_keeps_valid_overrides():
    assert get_thresholds({"critical": 0.2, "warning": 0.4}) == {"critical": 0.2, "warning": 0.4}
    assert get_thresholds(None) == {"critical": 0.35, "warning": 0.55}


def _statuses(result):
    return {z["status"] for z in result["zones"] if z["status"] != "no_data"}


def test_kmeans_applies_custom_thresholds_like_grid():
    """Régression : k-means ignorait les seuils tout en les annonçant dans thresholds_used."""
    bands = generate_synthetic_bands()
    strict = {"critical": 0.9, "warning": 0.95}  # tout le champ est sous 0,9
    for method in ("grid", "kmeans"):
        result, _ = run_pipeline(bands, method=method, thresholds=strict)
        assert result["thresholds_used"] == strict
        assert _statuses(result) == {"critical"}, method

    lenient = {"critical": -0.9, "warning": -0.8}  # plus rien n'est critique
    for method in ("grid", "kmeans"):
        result, _ = run_pipeline(bands, method=method, thresholds=lenient)
        assert _statuses(result) == {"ok"}, method
        assert result["alert_count"] == 0


def test_api_rejects_invalid_thresholds_and_keeps_listing_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as client:
        token = client.post("/auth/register", json={"email": "seuils@example.fr", "password": "motdepasse123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        field_id = client.post("/fields", headers=headers, json={"name": "P", "crop": "Orge", "bbox": [1.5, 48.3, 1.505, 48.304]}).json()["id"]
        url = f"/fields/{field_id}/analyses"

        for body in ('{"critical_threshold": NaN}', '{"critical_threshold": 0.8, "warning_threshold": 0.2}',
                     '{"critical_threshold": -5, "warning_threshold": -4}'):
            response = client.post(url, headers=headers, content=body)
            assert response.status_code == 422, body
            assert response.json()["detail"]["error"] == "validation_error"

        # rien n'a été enregistré, la liste reste lisible
        listing = client.get(url, headers=headers)
        assert listing.status_code == 200
        assert listing.json() == []


def test_save_analysis_never_persists_nan(tmp_path):
    db = tmp_path / "app.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users (email, password_hash, salt) VALUES ('a@example.fr', 'x', 'y')")
    conn.execute("INSERT INTO fields (user_id, name, crop, bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, area_ha) VALUES (1, 'P', 'Orge', 1, 1, 2, 2, 5)")
    conn.commit(); conn.close()

    with pytest.raises(ValueError):
        save_analysis(db, 1, "synthetic", "grid", {}, {"zones": [], "x": math.nan})
    assert list_analyses(db, 1) == []


def test_previously_stored_nan_is_read_back_as_null(tmp_path):
    """Les analyses enregistrées avant le correctif (avec un NaN littéral) ne doivent plus casser la liste."""
    db = tmp_path / "app.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users (email, password_hash, salt) VALUES ('a@example.fr', 'x', 'y')")
    conn.execute("INSERT INTO fields (user_id, name, crop, bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, area_ha) VALUES (1, 'P', 'Orge', 1, 1, 2, 2, 5)")
    conn.execute("INSERT INTO analyses (field_id, source, method, parameters_json, result_json) VALUES (1, 'synthetic', 'grid', '{\"critical_threshold\": NaN}', '{\"thresholds_used\": {\"critical\": NaN}}')")
    conn.commit(); conn.close()

    [analysis] = list_analyses(db, 1)
    assert analysis["parameters"]["critical_threshold"] is None
    assert analysis["result"]["thresholds_used"]["critical"] is None
