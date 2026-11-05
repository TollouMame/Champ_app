import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import api

STATIC = Path(__file__).resolve().parent.parent / "static"
PROD_SECRET = "p" * 48


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as c:
        yield c


def test_security_headers_on_api_responses(client):
    headers = client.get("/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=(self)" in headers["permissions-policy"]
    assert "content-security-policy" not in headers            # inutile sur du JSON
    assert "strict-transport-security" not in headers          # ENV=development (conftest)


def test_security_headers_also_cover_error_responses(client, monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "100")
    response = client.post("/fields", content=b"x" * 500, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_html_pages_get_a_content_security_policy(client):
    for page in ("/onboarding_mobile.html", "/apercu_champ_mobile.html"):
        csp = client.get(page).headers["content-security-policy"]
        assert "connect-src 'self'" in csp                      # bloque l'envoi du token vers un tiers
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "'unsafe-eval'" not in csp


def test_csp_can_be_switched_to_report_only(client, monkeypatch):
    monkeypatch.setenv("CSP_REPORT_ONLY", "1")
    headers = client.get("/onboarding_mobile.html").headers
    assert "content-security-policy-report-only" in headers
    assert "content-security-policy" not in headers


def test_csp_allows_every_external_host_the_pages_actually_use():
    """Si une page référence un nouvel hôte externe sans que la CSP l'autorise, la page serait cassée."""
    csp = api.CONTENT_SECURITY_POLICY
    sources = set(re.findall(r"https://[^\s;]+", csp))

    def allowed(host: str) -> bool:
        return any(s == f"https://{host}" or (s.startswith("https://*.") and host.endswith(s[len("https://*"):])) for s in sources)

    hosts = set()
    for page in STATIC.glob("*.html"):
        for host in re.findall(r"https://([A-Za-z0-9{}.\-]+)", page.read_text(encoding="utf-8")):
            hosts.add(host.replace("{s}", "a"))
    hosts.discard("www.w3.org")
    assert hosts, "aucun hôte externe détecté : le test ne vérifie plus rien"
    not_allowed = sorted(h for h in hosts if not allowed(h))
    assert not not_allowed, f"hôtes utilisés par les pages mais absents de la CSP : {not_allowed}"
    assert "https://fonts.gstatic.com" in sources              # fichiers de police chargés par la feuille Google Fonts


def test_hsts_is_sent_outside_development(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", PROD_SECRET)
    with TestClient(api.app) as client:
        assert "max-age=31536000" in client.get("/health").headers["strict-transport-security"]


def test_interactive_docs_are_only_available_in_explicit_development(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    assert api.get_docs_kwargs() == {}
    for env in ("production", "staging", ""):
        monkeypatch.setenv("ENV", env)
        assert api.get_docs_kwargs() == {"docs_url": None, "redoc_url": None, "openapi_url": None}, env
    monkeypatch.delenv("ENV")
    assert api.get_docs_kwargs()["openapi_url"] is None        # ENV absent = pas de doc (échec fermé)


def test_health_reports_unavailable_database_without_leaking_details(client, monkeypatch):
    def broken(_path):
        raise OSError("disk exploded at /srv/secret/path/app.db")

    monkeypatch.setattr(api, "get_connection", broken)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable"}
    assert "secret" not in response.text
