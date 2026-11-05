import importlib

import pytest


def test_build_config_reads_environment_credentials(monkeypatch):
    monkeypatch.setenv("SH_CLIENT_ID", "demo-client")
    monkeypatch.setenv("SH_CLIENT_SECRET", "demo-secret")
    import src.fetch_sentinel as fetch_sentinel

    importlib.reload(fetch_sentinel)

    config = fetch_sentinel.build_config()
    assert config.sh_client_id == "demo-client"
    assert config.sh_client_secret == "demo-secret"


def test_fetch_field_bands_rejects_invalid_bbox_and_resolution(monkeypatch):
    monkeypatch.setenv("SH_CLIENT_ID", "demo-client")
    monkeypatch.setenv("SH_CLIENT_SECRET", "demo-secret")
    import src.fetch_sentinel as fetch_sentinel

    importlib.reload(fetch_sentinel)

    with pytest.raises(ValueError, match="bbox"):
        fetch_sentinel.fetch_field_bands((0.0, 0.0, 0.0, 1.0))

    with pytest.raises(ValueError, match="resolution"):
        fetch_sentinel.fetch_field_bands((0.0, 0.0, 1.0, 1.0), resolution=0)


def test_fetch_field_bands_refuses_area_too_large_for_one_request(monkeypatch):
    monkeypatch.setenv("SH_CLIENT_ID", "demo-client")
    monkeypatch.setenv("SH_CLIENT_SECRET", "demo-secret")
    import src.fetch_sentinel as fetch_sentinel

    importlib.reload(fetch_sentinel)
    monkeypatch.setattr(
        fetch_sentinel, "SentinelHubRequest", lambda *a, **k: pytest.fail("aucune requête ne doit partir")
    )

    with pytest.raises(ValueError, match="trop étendue"):
        fetch_sentinel.fetch_field_bands((0.0, 40.0, 1.0, 41.0), resolution=10)  # ~ 8500 x 11000 px
