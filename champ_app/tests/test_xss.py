"""Teste avec Node.js que des données contrôlées par l'utilisateur (nom de champ...) ne sont
jamais interprétées comme du HTML. Ignoré si Node.js n'est pas installé."""

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

PAYLOADS = [
    "<img src=x onerror=alert(document.domain)>",
    "</div><script>alert(1)</script>",
    '"><svg/onload=alert(1)>',
]


def _function(html: str, name: str) -> str:
    match = re.search(rf"function {name}\([^)]*\)\{{[\s\S]*?\n\}}\n", html)
    assert match, name
    return match.group(0)


def _node(script: str):
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


FAKE_DOM = """
const created = [];
class El {
  constructor(tag){ this.tag = tag; this.children = []; this.attrs = {}; this._text = ''; this.htmlWrites = []; created.push(this); }
  set innerHTML(v){ this.htmlWrites.push(v); }
  set textContent(v){ this._text = String(v); }
  get textContent(){ return this._text; }
  append(...kids){ this.children.push(...kids); }
}
const document = { createElement: (tag) => new El(tag) };
"""


@pytest.mark.parametrize("payload", PAYLOADS)
def test_field_row_renders_malicious_field_name_as_plain_text(payload):
    out = _node(FAKE_DOM + _function(ONBOARDING, "buildFieldRow") + f"""
const row = buildFieldRow({{ id: 7, name: {json.dumps(payload)}, crop: {json.dumps(payload)}, area_ha: 3.2, sowing_date: {json.dumps(payload)} }});
const [nameEl, metaEl] = row.children;
console.log(JSON.stringify({{
  name: nameEl.textContent, meta: metaEl.textContent,
  htmlWrites: created.flatMap(e => e.htmlWrites), href: row.href,
}}));
""".replace("row.href", "row.href"))
    assert out["name"] == payload                      # affiché littéralement, jamais interprété
    assert payload in out["meta"]
    assert out["htmlWrites"] == []                     # aucune écriture via innerHTML
    assert out["href"] is None or "<" not in out["href"]


def test_field_row_link_cannot_be_hijacked_through_the_id():
    out = _node(FAKE_DOM + _function(ONBOARDING, "buildFieldRow") + """
const row = buildFieldRow({ id: '1&x=<script>', name: 'n', crop: 'c', area_ha: 1 });
console.log(JSON.stringify({ href: row.href }));
""")
    assert "<" not in out["href"] and "&x=" not in out["href"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_escape_html_neutralises_markup(payload):
    out = _node(_function(APERCU, "escapeHtml") + f"console.log(JSON.stringify(escapeHtml({json.dumps(payload)})));")
    assert "<" not in out and ">" not in out and '"' not in out


def test_escape_html_keeps_normal_text_readable():
    assert _node(_function(APERCU, "escapeHtml") + "console.log(JSON.stringify(escapeHtml('Z12 & co')));") == "Z12 &amp; co"


def test_no_page_inserts_a_raw_field_name_through_innerhtml():
    for name, html in (("onboarding", ONBOARDING), ("apercu", APERCU)):
        assert not re.search(r"innerHTML[^;]*\$\{\s*f\.name", html), name
        assert not re.search(r"innerHTML[^;]*\$\{\s*(zone|c)\.id\s*\}", html), f"{name} : identifiant de zone non échappé"
