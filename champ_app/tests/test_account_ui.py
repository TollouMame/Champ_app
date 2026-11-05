"""Teste avec Node.js l'affichage des erreurs de l'API (plus de « [object Object] ») et la présence
des commandes d'export / suppression de compte. Ignoré si Node.js n'est pas installé."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js non installé")

STATIC = Path(__file__).resolve().parent.parent / "static"
ONBOARDING = (STATIC / "onboarding_mobile.html").read_text(encoding="utf-8")
APERCU = (STATIC / "apercu_champ_mobile.html").read_text(encoding="utf-8")


def _function(html: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\{{[\s\S]*?\n\}}\n", html)
    assert match, name
    return match.group(0)


def _run(script: str):
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _describe(html: str, status: int, body) -> str:
    """Appelle describeApiError avec une réponse factice (body = objet JSON ou texte brut)."""
    return _run(f"""
{_function(html, 'describeApiError')}
const raw = {json.dumps(body if isinstance(body, str) else json.dumps(body))};
const isJson = {json.dumps(not isinstance(body, str))};
const res = {{ status: {status}, json: async () => {{ if(!isJson) throw new SyntaxError('not json'); return JSON.parse(raw); }} }};
describeApiError(res).then(m => console.log(JSON.stringify(m)));
""")


SHAPES = [
    (401, {"detail": {"error": "auth_error", "detail": "Email ou mot de passe incorrect.", "message": "Email ou mot de passe incorrect."}}, "Email ou mot de passe incorrect."),
    (403, {"detail": {"error": "invalid_password", "detail": "Mot de passe incorrect.", "message": "Mot de passe incorrect."}}, "Mot de passe incorrect."),
    (422, {"detail": [{"msg": "Field required", "loc": ["body", "x"]}]}, "Field required"),
    (404, {"detail": "Not Found"}, "Not Found"),
    (429, {"error": "rate_limited", "detail": "Trop de tentatives, réessayez plus tard.", "message": "Trop de tentatives, réessayez plus tard."}, "Trop de tentatives, réessayez plus tard."),
    (500, "Internal Server Error", "Erreur serveur (code 500)."),
    (502, {}, "Erreur serveur (code 502)."),
]


@pytest.mark.parametrize("html_name,html", [("onboarding", ONBOARDING), ("apercu", APERCU)])
@pytest.mark.parametrize("status,body,expected", SHAPES)
def test_api_errors_are_shown_with_the_server_message(html_name, html, status, body, expected):
    message = _describe(html, status, body)
    assert message == expected
    assert "[object Object]" not in message


def test_api_call_throws_readable_error_and_returns_data_on_success():
    script = f"""
const state = {{ token: 'tok' }};
const API_BASE = '';
{_function(ONBOARDING, 'describeApiError')}
{_function(ONBOARDING, 'apiCall')}
const calls = [];
globalThis.fetch = async (url, opts) => {{
  calls.push({{ url, auth: opts.headers['Authorization'] }});
  if (url === '/ok') return {{ ok: true, status: 200, json: async () => ({{ hello: 'monde' }}) }};
  return {{ ok: false, status: 403, json: async () => ({{ detail: {{ error: 'invalid_password', detail: 'Mot de passe incorrect.', message: 'Mot de passe incorrect.' }} }}) }};
}};
(async () => {{
  const ok = await apiCall('/ok', {{ method: 'GET' }});
  let message = null;
  try {{ await apiCall('/account/delete', {{ method: 'POST', body: '{{}}' }}); }} catch (e) {{ message = e.message; }}
  console.log(JSON.stringify({{ ok, message, auth: calls[0].auth }}));
}})();
"""
    out = _run(script)
    assert out == {"ok": {"hello": "monde"}, "message": "Mot de passe incorrect.", "auth": "Bearer tok"}


def test_onboarding_offers_data_export_and_account_deletion():
    for element_id in ("btn-export-data", "btn-goto-delete", "step-delete", "delete-password", "btn-delete-account", "btn-cancel-delete"):
        assert f'id="{element_id}"' in ONBOARDING, element_id
    assert "/account/export" in ONBOARDING and "/account/delete" in ONBOARDING
    handler = ONBOARDING.split("getElementById('btn-delete-account').addEventListener")[1]
    assert "showStep('welcome')" in handler          # retour à l'accueil après suppression
    assert "clearSession()" in handler               # la session locale est effacée
    assert "if(!password)" in handler                # pas de requête sans mot de passe saisi


def test_service_worker_never_caches_personal_data_endpoints():
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    assert "/account" not in worker
