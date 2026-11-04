import numpy as np
import pytest
from fastapi.testclient import TestClient
from matplotlib.path import Path as MplPath

from src import api
from src.demo_data import generate_synthetic_bands
from src.geometry import polygon_mask, prepare_bands

BBOX = (1.50, 48.30, 1.51, 48.31)


def _centers(bbox, shape):
    h, w = shape
    lons = bbox[0] + (np.arange(w) + 0.5) / w * (bbox[2] - bbox[0])
    lats = bbox[3] - (np.arange(h) + 0.5) / h * (bbox[3] - bbox[1])
    xx, yy = np.meshgrid(lons, lats)
    return np.column_stack([xx.ravel(), yy.ravel()])


@pytest.mark.parametrize("seed", range(6))
def test_polygon_mask_matches_independent_implementation(seed):
    """Comparaison pixel à pixel avec matplotlib.path.Path (implémentation indépendante)."""
    rng = np.random.default_rng(seed)
    n = int(rng.integers(5, 14))
    angles = np.sort(rng.uniform(0, 2 * np.pi, n))
    radius = rng.uniform(0.25, 0.48, n)
    polygon = [(1.505 + r * 0.01 * np.cos(a), 48.305 + r * 0.01 * np.sin(a)) for a, r in zip(angles, radius)]  # étoilé => simple

    shape = (53, 71)
    ours = polygon_mask(polygon, BBOX, shape)
    reference = MplPath(polygon).contains_points(_centers(BBOX, shape)).reshape(shape)
    assert (ours != reference).sum() == 0
    assert 0 < ours.sum() < ours.size


def test_polygon_mask_is_north_up():
    north_half = [(1.50, 48.305), (1.51, 48.305), (1.51, 48.31), (1.50, 48.31)]
    mask = polygon_mask(north_half, BBOX, (40, 40))
    assert mask[:20].all() and not mask[20:].any()  # ligne 0 = nord


def test_polygon_mask_handles_concave_l_shape():
    l_shape = [(1.50, 48.30), (1.51, 48.30), (1.51, 48.303), (1.503, 48.303), (1.503, 48.31), (1.50, 48.31)]
    mask = polygon_mask(l_shape, BBOX, (100, 100))
    h = mask.shape[0]
    assert mask[:, :25].all()                       # la barre verticale de gauche est dedans
    assert not mask[:h // 2, 60:].any()             # le « creux » en haut à droite est dehors
    assert mask[-20:, 60:].all()                    # la barre horizontale du bas est dedans


def test_prepare_bands_invalidates_outside_and_keeps_input_untouched():
    bands = generate_synthetic_bands(height=60, width=80)
    original = bands.copy()
    triangle = [(1.50, 48.30), (1.51, 48.30), (1.505, 48.31)]
    masked, ratio = prepare_bands(bands, BBOX, triangle)

    assert np.array_equal(bands, original)
    assert masked[:, :, 3].sum() < bands[:, :, 3].sum()
    assert masked[0, 0, 3] == 0                      # coin nord-ouest hors triangle
    assert 0.9 < ratio <= 1.0                        # dans le triangle, presque tout est valide


def test_prepare_bands_without_polygon_uses_whole_bbox_and_reports_cloud_cover():
    bands = generate_synthetic_bands(height=50, width=50)
    bands[:, :, 3] = 1
    bands[:25, :, 3] = 0                             # moitié nord masquée (nuages)
    masked, ratio = prepare_bands(bands, BBOX, None)
    assert ratio == pytest.approx(0.5)
    assert np.array_equal(masked, bands)


def test_prepare_bands_rejects_polygon_smaller_than_a_pixel():
    bands = generate_synthetic_bands(height=10, width=10)
    tiny = [(1.5051, 48.3051), (1.5052, 48.3051), (1.5052, 48.3052)]
    with pytest.raises(ValueError, match="trop petite"):
        prepare_bands(bands, BBOX, tiny)


# ---- bout en bout via l'API ----

def _client_with_field(monkeypatch, tmp_path, **field_geometry):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    client = TestClient(api.app)
    client.__enter__()
    token = client.post("/auth/register", json={"email": "geo@example.fr", "password": "motdepasse123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    field = client.post("/fields", headers=headers, json={"name": "P", "crop": "Orge", **field_geometry})
    assert field.status_code == 200, field.text
    return client, headers, field.json()["id"]


def test_api_analysis_ignores_pixels_outside_the_polygon(monkeypatch, tmp_path):
    triangle = [[1.500, 48.300], [1.506, 48.300], [1.500, 48.306]]   # moitié sud-ouest de la bbox
    client, headers, field_id = _client_with_field(monkeypatch, tmp_path, polygon=triangle)
    try:
        body = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "kmeans"}).json()
        quality = body["result"]["data_quality"]
        assert quality["polygon_applied"] is True
        grid = np.array(body["result"]["label_grid"]["data"])
        # le coin nord-est (hors triangle) n'a pas de zone ; le coin sud-ouest en a une
        assert grid[0, -1] == -1
        assert grid[-1, 0] >= 0
        assert (grid == -1).mean() > 0.35
    finally:
        client.__exit__(None, None, None)


def test_api_refuses_image_that_is_mostly_invalid(monkeypatch, tmp_path):
    client, headers, field_id = _client_with_field(monkeypatch, tmp_path, bbox=[1.5, 48.3, 1.505, 48.304])

    def cloudy(seed=0, **_):
        bands = generate_synthetic_bands(seed=seed)
        bands[:, :, 3] = 0
        bands[:10, :10, 3] = 1                         # < 1 % de pixels exploitables
        return bands

    monkeypatch.setattr(api, "generate_synthetic_bands", cloudy)
    try:
        response = client.post(f"/fields/{field_id}/analyses", headers=headers, json={})
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "analysis_error"
        assert "visible" in response.json()["detail"]["detail"]
        assert client.get(f"/fields/{field_id}/analyses", headers=headers).json() == []
    finally:
        client.__exit__(None, None, None)


def test_api_flags_low_quality_but_still_returns_analysis(monkeypatch, tmp_path):
    client, headers, field_id = _client_with_field(monkeypatch, tmp_path, bbox=[1.5, 48.3, 1.505, 48.304])

    def half_cloudy(seed=0, **_):
        bands = generate_synthetic_bands(seed=seed)
        bands[:, :, 3] = 1
        bands[:60, :, 3] = 0                           # ~50 % masqué
        return bands

    monkeypatch.setattr(api, "generate_synthetic_bands", half_cloudy)
    try:
        response = client.post(f"/fields/{field_id}/analyses", headers=headers, json={})
        assert response.status_code == 200
        quality = response.json()["result"]["data_quality"]
        assert quality["low_quality"] is True and 0.4 < quality["valid_pixel_ratio"] < 0.6
    finally:
        client.__exit__(None, None, None)
