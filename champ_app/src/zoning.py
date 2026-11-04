"""Découpage d'un raster d'indice en zones et détection des zones à alerter."""

import math

import numpy as np

# Seuils par défaut — À VALIDER PAR UN AGRONOME avant tout usage réel.
# Ce sont des hypothèses de travail, pas des références agronomiques établies.
DEFAULT_THRESHOLDS = {
    "critical": 0.35,
    "warning": 0.55,
}


def get_thresholds(overrides: dict | None = None) -> dict:
    """Retourne les seuils de zonage, avec possibilité de surcharge explicite.

    Lève ValueError si les seuils sont inexploitables : non numériques, NaN/inf,
    hors de la plage du NDVI [-1, 1], ou si critical >= warning. Sans cette
    validation, un seuil NaN désactive silencieusement toutes les alertes et un
    seuil incohérent produit des statuts absurdes.
    """
    thresholds = dict(DEFAULT_THRESHOLDS)
    if overrides:
        thresholds.update({k: float(v) for k, v in overrides.items() if v is not None})

    for name in ("critical", "warning"):
        value = thresholds[name]
        if not math.isfinite(value) or not -1.0 <= value <= 1.0:
            raise ValueError(f"Le seuil '{name}' doit être un nombre compris entre -1 et 1 (reçu : {value}).")
    if thresholds["critical"] >= thresholds["warning"]:
        raise ValueError(
            f"Le seuil 'critical' ({thresholds['critical']}) doit être strictement inférieur "
            f"au seuil 'warning' ({thresholds['warning']})."
        )
    return thresholds


def _row_letters(row: int) -> str:
    """Convertit un index de ligne en lettres façon colonnes Excel : 0->A, 25->Z, 26->AA, ..."""
    letters = ""
    n = row
    while True:
        n, remainder = divmod(n, 26)
        letters = chr(65 + remainder) + letters
        if n == 0:
            break
        n -= 1
    return letters


def zone_id(row: int, col: int) -> str:
    """Convertit (ligne, colonne) en identifiant type 'A1', 'B4', 'AA1'..."""
    return f"{_row_letters(row)}{col + 1}"


def split_into_zones(raster: np.ndarray, n_rows: int, n_cols: int) -> list[dict]:
    """
    Découpe un raster (H, W) en grille de n_rows x n_cols zones et calcule
    la moyenne de l'indice par zone (en ignorant les NaN = pixels masqués).

    Retourne une liste de dicts : {id, row, col, mean_value, valid_pixel_ratio}
    """
    h, w = raster.shape
    row_edges = np.linspace(0, h, n_rows + 1).astype(int)
    col_edges = np.linspace(0, w, n_cols + 1).astype(int)

    zones = []
    for r in range(n_rows):
        for c in range(n_cols):
            tile = raster[row_edges[r]:row_edges[r + 1], col_edges[c]:col_edges[c + 1]]
            valid = ~np.isnan(tile)
            valid_ratio = float(valid.mean()) if tile.size else 0.0
            mean_value = float(np.nanmean(tile)) if valid.any() else float("nan")
            zones.append({
                "id": zone_id(r, c),
                "row": r,
                "col": c,
                "mean_value": mean_value,
                "valid_pixel_ratio": round(valid_ratio, 3),
            })
    return zones


def classify_zone(mean_value: float, thresholds: dict | None = None) -> str:
    """Retourne 'critical', 'warning' ou 'ok' selon la valeur moyenne de la zone."""
    resolved_thresholds = get_thresholds(thresholds)
    if np.isnan(mean_value):
        return "no_data"
    if mean_value < resolved_thresholds["critical"]:
        return "critical"
    if mean_value < resolved_thresholds["warning"]:
        return "warning"
    return "ok"


def annotate_zones(zones: list[dict], thresholds: dict | None = None) -> list[dict]:
    """Ajoute le statut (critical/warning/ok/no_data) à chaque zone."""
    resolved_thresholds = get_thresholds(thresholds)
    for z in zones:
        z["status"] = classify_zone(z["mean_value"], resolved_thresholds)
    return zones