"""Teste avec Node.js la logique d'affichage de l'écran mobile (badge de source, bandeaux, valeurs nulles).

Les fonctions sont extraites telles quelles de static/apercu_champ_mobile.html puis exécutées
avec un faux `document`. Ignoré si Node.js n'est pas installé.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js non installé")

HTML = (Path(__file__).resolve().parent.parent / "static" / "apercu_champ_mobile.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(rf"function {name}\([^)]*\)\{{[\s\S]*?\n\}}\n", HTML)
    assert match, f"fonction {name} introuvable dans apercu_champ_mobile.html"
    return match.group(0)


def _run(scenario: dict) -> dict:
    script = f"""
let dataSource = {json.dumps(scenario.get('source', 'demo'))};
let analysisDate = {json.dumps(scenario.get('date'))};
let dataQuality = {json.dumps(scenario.get('quality'))};
const els = {{
  'source-badge': {{ textContent: '', className: '' }},
  'demo-banner': {{ shown: null, classList: {{ toggle(_, v) {{ els['demo-banner'].shown = v; }} }} }},
  'quality-banner': {{ textContent: '', shown: null, classList: {{ toggle(_, v) {{ els['quality-banner'].shown = v; }} }} }},
}};
const document = {{ getElementById: (id) => els[id] }};
{_function_source('formatIndex')}
{_function_source('formatAnalysisDate')}
{_function_source('updateSourceBadge')}
updateSourceBadge();
console.log(JSON.stringify({{
  badge: els['source-badge'].textContent, badgeClass: els['source-badge'].className,
  demoBanner: els['demo-banner'].shown, quality: els['quality-banner'].textContent, qualityShown: els['quality-banner'].shown,
  nullIndex: formatIndex(null), undefinedIndex: formatIndex(undefined), nanIndex: formatIndex(NaN), okIndex: formatIndex(0.456),
  date: formatAnalysisDate('2026-11-03 23:49:34'), badDate: formatAnalysisDate('n/a'), noDate: formatAnalysisDate(null),
}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_demo_source_is_never_styled_as_live():
    for source in ("demo", "synthetic"):
        out = _run({"source": source})
        assert out["badgeClass"] == "source-badge demo", source
        assert out["demoBanner"] is True, source
    assert _run({"source": "synthetic"})["badge"] == "ANALYSE DE DÉMO"


def test_sentinel_source_is_live_without_demo_banner_and_shows_date():
    out = _run({"source": "sentinel", "date": "03/11/2026"})
    assert out["badgeClass"] == "source-badge live"
    assert out["demoBanner"] is False
    assert out["badge"] == "DONNÉES SENTINEL · 03/11/2026"


def test_low_quality_image_shows_cloud_warning_with_percentage():
    out = _run({"source": "sentinel", "quality": {"low_quality": True, "valid_pixel_ratio": 0.456}})
    assert out["qualityShown"] is True
    assert "46 %" in out["quality"]
    healthy = _run({"source": "sentinel", "quality": {"low_quality": False, "valid_pixel_ratio": 0.95}})
    assert healthy["qualityShown"] is False and healthy["quality"] == ""
    assert _run({"source": "sentinel"})["qualityShown"] is False   # analyses anciennes sans data_quality


def test_missing_index_values_render_as_dash_instead_of_crashing():
    out = _run({})
    assert (out["nullIndex"], out["undefinedIndex"], out["nanIndex"], out["okIndex"]) == ("—", "—", "—", "0.46")


def test_analysis_date_formatting():
    out = _run({})
    assert out["date"] == "03/11/2026"
    assert out["badDate"] is None and out["noDate"] is None
