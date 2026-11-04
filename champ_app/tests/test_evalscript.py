"""Exécute l'evalscript Sentinel Hub avec Node.js pour vérifier le masque de nuages.

Le vrai service n'est pas joignable dans les tests : on évalue le même JavaScript
localement, pixel par pixel, pour chaque classe de la bande SCL.
Ignoré si Node.js n'est pas installé.
"""

import json
import shutil
import subprocess

import pytest

from src.fetch_sentinel import EVALSCRIPT_BANDS

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js non installé")

SCL_CLASSES = {
    0: "sans donnée", 1: "saturé", 2: "zones sombres", 3: "ombre de nuage", 4: "végétation",
    5: "sol nu", 6: "eau", 7: "non classé", 8: "nuage moyen", 9: "nuage haut", 10: "cirrus", 11: "neige",
}


def _run(sample: dict):
    script = EVALSCRIPT_BANDS + f"\nconsole.log(JSON.stringify([setup(), evaluatePixel({json.dumps(sample)})]));"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_evalscript_requests_scl_and_keeps_four_output_bands():
    setup, _ = _run({"B04": 0.1, "B05": 0.2, "B08": 0.4, "SCL": 4, "dataMask": 1})
    assert "SCL" in setup["input"]
    assert setup["output"]["bands"] == 4


@pytest.mark.parametrize("scl,label", SCL_CLASSES.items())
def test_evalscript_keeps_only_vegetation_and_bare_soil(scl, label):
    _, pixel = _run({"B04": 0.05, "B05": 0.15, "B08": 0.45, "SCL": scl, "dataMask": 1})
    assert pixel[:3] == [0.05, 0.15, 0.45]
    expected = 1 if scl in (4, 5) else 0
    assert pixel[3] == expected, f"SCL {scl} ({label})"


def test_evalscript_never_marks_a_pixel_valid_when_it_has_no_data():
    _, pixel = _run({"B04": 0, "B05": 0, "B08": 0, "SCL": 4, "dataMask": 0})
    assert pixel[3] == 0
