"""Les bibliothèques tierces sont hébergées localement : tout ce que les pages et le
service worker référencent doit réellement exister (un fichier manquant casserait
la carte, ou l'installation entière du service worker via cache.addAll)."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def test_pages_do_not_load_code_from_a_cdn():
    for page in STATIC.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "cdnjs.cloudflare.com" not in html, page.name
        for src in re.findall(r"<script[^>]+src=\"([^\"]+)\"", html):
            assert src.startswith("/"), f"{page.name} charge un script externe : {src}"


def test_every_local_resource_referenced_by_pages_exists():
    for page in STATIC.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for ref in re.findall(r"(?:href|src)=\"(/[^\"#?]+)\"", html):
            assert (STATIC / ref.lstrip("/")).is_file(), f"{page.name} référence {ref} introuvable"


def test_service_worker_shell_files_all_exist():
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    shell = re.search(r"SHELL_FILES = \[(.*?)\];", worker, re.S).group(1)
    paths = re.findall(r"'(/[^']+)'", shell)
    assert paths
    missing = [p for p in paths if not (STATIC / p.lstrip("/")).is_file()]
    assert not missing, f"cache.addAll échouerait (fichiers manquants) : {missing}"


def test_images_used_by_vendor_stylesheets_exist():
    for css in STATIC.glob("vendor/*/*.css"):
        for ref in re.findall(r"url\(['\"]?(images/[^)'\"?#]+)", css.read_text(encoding="utf-8")):
            assert (css.parent / ref).is_file(), f"{css.name} référence {ref} introuvable"


def test_vendor_licenses_are_kept():
    for lib in ("leaflet", "leaflet-draw"):
        assert (STATIC / "vendor" / lib / "LICENSE").is_file()


def test_api_actually_serves_the_vendor_files(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from src import api

    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as client:
        for path, kind in (("/vendor/leaflet/leaflet.js", "javascript"), ("/vendor/leaflet/leaflet.css", "css"),
                           ("/vendor/leaflet-draw/leaflet.draw.js", "javascript"), ("/vendor/leaflet/images/marker-icon.png", "png")):
            response = client.get(path)
            assert response.status_code == 200, path
            assert kind in response.headers["content-type"], path
        assert client.get("/onboarding_mobile.html").status_code == 200
