"""Régressions du premier lot de correctifs de l'interface (vérifiées aussi dans un vrai Chromium).

Ignorés si Node.js n'est pas installé."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js non installé")

STATIC = Path(__file__).resolve().parent.parent / "static"
APERCU = (STATIC / "apercu_champ_mobile.html").read_text(encoding="utf-8")
ONBOARDING = (STATIC / "onboarding_mobile.html").read_text(encoding="utf-8")


def _function(html: str, name: str) -> str:
    match = re.search(rf"^(?:async )?function {name}\([^)]*\)\{{[\s\S]*?\n\}}\n", html, re.M)
    assert match, name
    return match.group(0)


def _node(script: str):
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


FAKE_DOM = """
class El {
  constructor(){ this.children = []; this.className = ''; this.textContent = ''; this.value = ''; this.disabled = false;
                 this._classes = new Set(); this._html = '';
                 this.classList = { toggle: (c, on) => { on ? this._classes.add(c) : this._classes.delete(c); },
                                    add: c => this._classes.add(c), remove: c => this._classes.delete(c), contains: c => this._classes.has(c) }; }
  set innerHTML(v){ this._html = v; if (v === '') this.children = []; }
  get innerHTML(){ return this._html; }
  appendChild(c){ this.children.push(c); }
}
const els = {};
const document = { getElementById: id => (els[id] = els[id] || new El()), createElement: () => new El() };
"""

HELPERS = ("escapeHtml", "formatIndex", "recommendationFor")


def _helpers():
    return "\n".join(_function(APERCU, name) for name in HELPERS)


# ---------------------------------------------------------------- couleur d'un NDVI négatif

def test_negative_ndvi_is_never_drawn_with_the_healthy_colour():
    stops = re.search(r"const colorStops = \[[\s\S]*?\];", APERCU).group(0)
    out = _node(f"{stops}\n{_function(APERCU, 'lerpColor')}\n"
                "console.log(JSON.stringify({ neg: lerpColor(-0.2), deep: lerpColor(-5), zero: lerpColor(0), one: lerpColor(1), big: lerpColor(3) }));")
    assert out["neg"] == out["deep"] == out["zero"]          # l'extrémité la plus basse
    assert out["zero"] != out["one"]                          # avant : -0,2 donnait la couleur « sain » de 1
    assert out["big"] == out["one"]


# ---------------------------------------------------------------- onglet Alertes

def _alerts(zones, treated=(), blob=True):
    return _node(FAKE_DOM + _helpers() + f"""
let isBlobMap = {json.dumps(blob)}, cells = {json.dumps(zones if not blob else [])}, zones = {json.dumps(zones if blob else [])};
let treatedZones = new Set({json.dumps(list(treated))});
{_function(APERCU, 'renderAlerts')}
renderAlerts();
const list = document.getElementById('alerts-list');
console.log(JSON.stringify({{
  rows: list.children.filter(c => c.className.startsWith('alert-row')).map(r => ({{ cls: r.className, html: r.innerHTML }})),
  empty: list.innerHTML.includes('empty-state') ? list.innerHTML : '',
  more: list.children.filter(c => c.className === 'alert-more').map(c => c.textContent),
  dot: document.getElementById('nav-dot').classList.contains('show'),
}}));
""")


def _zone(i, ndvi, status):
    return {"id": f"Z{i}", "ndvi": ndvi, "ndre": 0.3, "status": status}


def test_alerts_tab_lists_nothing_when_every_zone_is_healthy():
    out = _alerts([_zone(i, 0.8, "ok") for i in range(1, 7)])
    assert out["rows"] == [] and "Aucune alerte" in out["empty"]
    assert out["dot"] is False                                # le point rouge disparaît


def test_alerts_tab_lists_only_real_alerts_most_severe_first():
    zones = [_zone(1, 0.8, "ok"), _zone(2, 0.50, "warning"), _zone(3, 0.20, "critical"), _zone(4, 0.30, "critical"), _zone(5, 0.7, "ok")]
    out = _alerts(zones)
    ids = [re.search(r'alert-zone">(Z\d)', row["html"]).group(1) for row in out["rows"]]
    assert ids == ["Z3", "Z4", "Z2"]                          # critiques d'abord (NDVI croissant), puis à surveiller
    assert out["rows"][2]["cls"].endswith("warning")
    assert "critique" in out["rows"][0]["html"] and "à surveiller" in out["rows"][2]["html"]
    assert out["dot"] is True


def test_alerts_tab_flags_treated_zones_and_caps_the_list():
    zones = [_zone(i, 0.1 + i / 100, "critical") for i in range(1, 12)]
    out = _alerts(zones, treated=["Z1"])
    assert len(out["rows"]) == 8 and out["more"] == ["… et 3 autre(s) zone(s)."]
    assert "traitée" in out["rows"][0]["html"] and "traitée" not in out["rows"][1]["html"]


def test_alerts_tab_works_for_the_demo_grid_too():
    out = _alerts([_zone(1, 0.8, "ok"), _zone(2, 0.2, "critical")], blob=False)
    assert len(out["rows"]) == 1


# ---------------------------------------------------------------- bouton « traité »

def _button_state(zone, analysis_id=7, treated=()):
    return _node(FAKE_DOM + f"""
let currentAnalysisId = {json.dumps(analysis_id)}, treatedZones = new Set({json.dumps(list(treated))});
const TREAT_LABEL = 'Marquer cette zone comme traitée';
{_function(APERCU, 'zoneIsTreatable')}
{_function(APERCU, 'setTreatmentButton')}
const button = new El(); button.classList.add('done');
setTreatmentButton(button, {json.dumps(zone)});
console.log(JSON.stringify({{ text: button.textContent, disabled: button.disabled, done: button.classList.contains('done') }}));
""")


def test_treatment_button_always_reflects_the_real_state_of_the_zone():
    free = {"id": "Z1", "ndvi": 0.6, "status": "ok"}
    assert _button_state(free) == {"text": "Marquer cette zone comme traitée", "disabled": False, "done": False}   # « done » résiduel retiré
    assert _button_state(free, treated=["Z1"]) == {"text": "Zone marquée comme traitée ✓", "disabled": True, "done": True}
    assert _button_state({"id": "Z2", "ndvi": 0.6, "status": "ok"}, treated=["Z1"])["disabled"] is False             # une AUTRE zone n'est pas bloquée
    assert _button_state(free, analysis_id=None)["disabled"] is True and "analyse" in _button_state(free, analysis_id=None)["text"]
    outside = {"id": "A9", "ndvi": None, "status": "no_data"}
    assert _button_state(outside) == {"text": "Zone hors parcelle : rien à traiter", "disabled": True, "done": False}
    assert _button_state(None)["disabled"] is True


def test_map_tap_targets_the_tapped_zone_and_the_position_button_uses_the_current_cell():
    tap = _function(APERCU, "onMapTap")
    assert "selectedZone = zone" in tap and "markZoneTreated(selectedZone, tapButton)" in tap
    assert "markBtn.addEventListener('click', ()=> markZoneTreated(currentCell, markBtn));" in APERCU
    assert "markBtn.textContent = 'Zone marquée comme traitée ✓'" not in APERCU      # l'ancien état collé a disparu


def test_selection_and_treated_state_are_reset_when_a_new_analysis_is_displayed():
    assert "resetSelection();" in _function(APERCU, "useAnalysis")
    assert "resetSelection();" in _function(APERCU, "useDemoData")


# ---------------------------------------------------------------- « Actualiser »

def test_refresh_note_tells_when_the_analysis_is_already_up_to_date():
    out = _node(FAKE_DOM + f"""
{_function(APERCU, 'formatIsoDate')}
{_function(APERCU, 'showRefreshNote')}
const note = document.getElementById('refresh-note');
showRefreshNote({{ reused: true, parameters: {{ acquisition_date: '2026-11-05' }} }});
const reused = {{ text: note.textContent, shown: note.classList.contains('show') }};
showRefreshNote({{ reused: false }});
console.log(JSON.stringify({{ reused, fresh: {{ text: note.textContent, shown: note.classList.contains('show') }} }}));
""")
    assert out["reused"]["shown"] and "Déjà à jour" in out["reused"]["text"] and "05/11/2026" in out["reused"]["text"]
    assert out["fresh"] == {"text": "", "shown": False}


# ---------------------------------------------------------------- formulaire d'ajout de champ

def test_field_form_is_reset_each_time_it_is_opened_and_on_logout():
    out = _node(FAKE_DOM + f"""
const state = {{ name: 'Ancien', crop: 'Orge', polygon: [[1,2]], areaHa: 5, fieldId: 9 }};
let cleared = 0; let drawnItems = {{ clearLayers: () => {{ cleared++; }} }};
const crop = document.getElementById('field-crop'); crop.options = [1, 2, 3]; crop.selectedIndex = 2;
document.getElementById('field-name').value = 'Ancien'; document.getElementById('field-sowing').value = '2026-03-01';
document.getElementById('err-draw').textContent = 'erreur';
document.getElementById('btn-save-field').disabled = true;
{_function(ONBOARDING, 'resetFieldForm')}
resetFieldForm();
console.log(JSON.stringify({{ state, cleared, name: document.getElementById('field-name').value, sowing: document.getElementById('field-sowing').value,
  crop: crop.selectedIndex, area: document.getElementById('area-readout').innerHTML, err: document.getElementById('err-draw').textContent,
  saveDisabled: document.getElementById('btn-save-field').disabled }}));
""")
    assert out["state"] == {"name": "", "crop": "", "polygon": None, "areaHa": 0, "fieldId": None}
    assert out["cleared"] == 1 and out["name"] == "" and out["sowing"] == "" and out["crop"] == 0
    assert "—" in out["area"] and out["err"] == "" and out["saveDisabled"] is False

    assert "resetFieldForm();" in _function(ONBOARDING, "enterAddField")
    assert "resetFieldForm();" in ONBOARDING.split("getElementById('btn-logout').addEventListener")[1].split("});")[0]
    assert "resetFieldForm();" in ONBOARDING.split("getElementById('btn-delete-account').addEventListener")[1]


# ---------------------------------------------------------------- textes en dur

def test_no_hard_coded_crop_text_is_shown_for_a_loaded_field():
    assert "stade montaison" not in APERCU
    assert "pos-crop').textContent = field.crop" in APERCU


def test_alert_dot_is_hidden_until_there_is_an_alert():
    assert re.search(r'<div class="nav-dot" id="nav-dot"></div>', APERCU)      # plus de « show » en dur
