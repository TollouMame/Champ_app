"""Régressions des deux bugs « carte qui disparaît » et « inscription qui passe en silence ».

Les parcours complets ont été vérifiés dans un vrai navigateur (Chromium) ; ces tests, plus légers,
tournent en CI avec Node.js et protègent la logique contre un retour en arrière.
Ignorés si Node.js n'est pas installé.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import auth

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js non installé")

STATIC = Path(__file__).resolve().parent.parent / "static"
ONBOARDING = (STATIC / "onboarding_mobile.html").read_text(encoding="utf-8")
APERCU = (STATIC / "apercu_champ_mobile.html").read_text(encoding="utf-8")


def _function(html: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\{{[\s\S]*?\n\}}\n", html)
    assert match, name
    return match.group(0)


def _node(script: str):
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ------------------------------------------------------------ mot de passe : navigateur == serveur

PASSWORDS = [
    "", "abc", "1234567", "        ", "aaaaaaaa", "abababab", "password", "PASSWORD", "MotDePasse", "123456789",
    "motdepasse123", "Un bon mot de passe !", "  espaces au bord  ", "éèàù12345", "ab12" * 32, "ab12" * 32 + "x",
    "x" * 129, "azertyuiop", "AZERTYUIOP", "qwertyuiop", "abcd", "abcdefgh", "aabbccdd", "😀😀😀😀😀😀😀😀", "a b c d ",
]


@needs_node
def test_browser_and_server_apply_exactly_the_same_password_rules():
    """Un mot de passe accepté par le navigateur mais refusé par le serveur (ou l'inverse) recréerait l'ancien bug."""
    constants = "\n".join(re.search(rf"const {name} = [^;]+;", ONBOARDING).group(0)
                          for name in ("MIN_PASSWORD_LENGTH", "MAX_PASSWORD_LENGTH", "MIN_DISTINCT_CHARACTERS", "COMMON_PASSWORDS"))
    results = _node(f"{constants}\n{_function(ONBOARDING, 'validatePassword')}\n"
                    f"console.log(JSON.stringify({json.dumps(PASSWORDS)}.map(p => validatePassword(p))));")
    mismatches = []
    for password, browser_message in zip(PASSWORDS, results):
        try:
            auth.validate_password(password)
            server_message = ""
        except auth.AuthError as exc:
            server_message = str(exc)
        if browser_message != server_message:
            mismatches.append((password[:20], browser_message, server_message))
    assert not mismatches, mismatches


@needs_node
def test_common_password_lists_are_identical_in_browser_and_server():
    match = re.search(r"const COMMON_PASSWORDS = (\[[^\]]*\]);", ONBOARDING)
    assert set(json.loads(match.group(1).replace("'", '"'))) == set(auth.COMMON_PASSWORDS)


# ------------------------------------------------------------ inscription : plus de faux compte

def test_signup_has_no_offline_demo_path_that_fakes_an_account():
    assert "apiOnline" not in ONBOARDING
    assert "mode démo local" not in ONBOARDING
    assert "enregistré en local uniquement" not in ONBOARDING


def test_signup_only_advances_after_the_server_confirmed_the_account():
    handler = ONBOARDING.split("document.getElementById('btn-signup').addEventListener")[1].split("btn-back-1")[0]
    assert "if(problems.email || problems.password) return;" in handler          # conditions non remplies : on s'arrête
    assert handler.index("created = true") < handler.index("await enterAddField()")  # étape suivante APRÈS confirmation
    assert "!data.access_token" in handler                                       # réponse sans jeton refusée
    assert "if(!created) return;" in handler


def test_field_saving_requires_a_session_and_a_server_confirmation():
    handler = ONBOARDING.split("document.getElementById('btn-save-field').addEventListener")[1].split("btn-goto-dashboard")[0]
    assert "if(!state.token)" in handler
    assert "!field || !field.id" in handler
    assert handler.index("apiCall('/fields'") < handler.index("showStep(3)")


# ------------------------------------------------------------ géolocalisation : ne bloque jamais la carte

def test_geolocation_can_never_block_the_drawing_map():
    locate = _function(ONBOARDING, "getDeviceLocation")
    assert "setTimeout(" in locate and "FALLBACK_LOCATION" in locate        # minuterie indépendante de la permission
    enter = _function(ONBOARDING, "enterAddField")
    assert "initLeafletMap(FALLBACK_LOCATION.lat, FALLBACK_LOCATION.lon)" in enter
    assert "await getDeviceLocation()" not in enter                          # la carte n'attend plus la position
    assert "centerMapOnDevice()" in enter


# ------------------------------------------------------------ carte : hauteur imposée

FAKE_DOM = """
class El {
  constructor(){ this.style = {}; this.children = []; this.className = ''; this.classes = new Set(); this._html = '';
                 this.classList = { toggle: (c, on) => on ? this.classes.add(c) : this.classes.delete(c) }; }
  set innerHTML(v){ this._html = v; this.children = []; }
  appendChild(c){ this.children.push(c); }
  addEventListener(){}
}
const grid = new El();
const document = { getElementById: () => grid, createElement: () => new El() };
const COLS = 8, onMapTap = () => {}, lerpColor = () => 'rgb(0,0,0)';
"""


@needs_node
def test_zone_map_gets_a_real_height_from_the_grid_proportions():
    """Avant : 1 200 cases vides dans une grille sans hauteur = 0 px, la carte disparaissait."""
    out = _node(FAKE_DOM + f"""
let isBlobMap = true, zones = [{{id:'Z1', ndvi:0.6}}], cells = [], currentCell = null;
let labelGrid = {{ rows: 10, cols: 40, data: Array.from({{length: 10}}, () => Array(40).fill(0)) }};
{_function(APERCU, 'renderMap')}
renderMap();
const wide = {{ ...grid.style }}, wideCells = grid.children.length;
labelGrid = {{ rows: 30, cols: 8, data: Array.from({{length: 30}}, () => Array(8).fill(0)) }};
renderMap();
const tall = {{ ...grid.style }};
console.log(JSON.stringify({{ wide, wideCells, tall }}));
""")
    assert out["wide"]["aspectRatio"] == "40 / 10" and out["wide"]["gridTemplateRows"] == "repeat(10, 1fr)"
    assert out["wideCells"] == 400
    assert out["tall"]["aspectRatio"] == "8 / 30"
    # un champ très haut ne dépasse pas ~60 % de la hauteur de l'écran : largeur bornée en conséquence
    assert out["tall"]["maxWidth"] == "16.0vh"


@needs_node
def test_demo_map_resets_the_zone_map_layout_settings():
    out = _node(FAKE_DOM + f"""
let isBlobMap = true, zones = [{{id:'Z1', ndvi:0.6}}], currentCell = null;
let labelGrid = {{ rows: 10, cols: 40, data: Array.from({{length: 10}}, () => Array(40).fill(0)) }};
let cells = [{{id:'A1', ndvi:0.5}}];
{_function(APERCU, 'renderMap')}
renderMap();
isBlobMap = false;
renderMap();
console.log(JSON.stringify(grid.style));
""")
    for prop in ("gridTemplateRows", "aspectRatio", "maxWidth", "marginLeft", "marginRight"):
        assert out[prop] == "", prop
