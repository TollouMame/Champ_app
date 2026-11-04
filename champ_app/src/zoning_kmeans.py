"""
Segmentation d'un champ en zones de gestion homogènes, par clustering k-means,
plutôt qu'un découpage en grille arbitraire (zoning.py).

Principe : chaque pixel valide est représenté par un vecteur
[valeur_indice, position_x_normalisée, position_y_normalisée]. Le clustering
sur ce vecteur (plutôt que sur la seule valeur d'indice) encourage des zones
à la fois homogènes en valeur ET spatialement compactes — plus proche de ce
qu'un agronome tracerait à la main qu'une simple grille régulière.

`spatial_weight` contrôle l'importance de la position par rapport à la
valeur de l'indice : plus il est élevé, plus les zones sont compactes
géographiquement (au risque de mélanger des valeurs différentes) ; plus il
est bas, plus le clustering suit fidèlement l'indice (au risque de zones
éparpillées, en mouchetis).
"""

import numpy as np
from sklearn.cluster import KMeans

from src.zoning import classify_zone, get_thresholds


def segment_zones_kmeans(
    raster: np.ndarray,
    n_zones: int = 6,
    spatial_weight: float = 0.4,
    random_state: int = 0,
    thresholds: dict | None = None,
) -> tuple[np.ndarray, list[dict]]:
    """
    raster : (H, W), NaN pour les pixels invalides/masqués.

    Retourne :
      label_map : (H, W) d'entiers, -1 pour les pixels invalides,
                  0..n_zones-1 pour les zones assignées
      zones     : liste de dicts {id, pixel_count, mean_value, status,
                  centroid_row, centroid_col}, triée par position
                  (haut-gauche vers bas-droite) pour un étiquetage stable
    """
    resolved_thresholds = get_thresholds(thresholds)  # valide aussi les seuils
    h, w = raster.shape
    valid_mask = ~np.isnan(raster)

    if not valid_mask.any():
        raise ValueError(
            "Aucun pixel valide dans le raster (image entièrement masquée, ex: couverture "
            "nuageuse totale) : impossible de calculer un zonage k-means."
        )

    yy, xx = np.mgrid[0:h, 0:w]
    x_norm = xx[valid_mask] / w
    y_norm = yy[valid_mask] / h
    values = raster[valid_mask]

    features = np.column_stack([values, x_norm * spatial_weight, y_norm * spatial_weight])

    n_zones = min(n_zones, len(np.unique(values)))  # évite un crash si trop peu de pixels distincts
    kmeans = KMeans(n_clusters=n_zones, random_state=random_state, n_init=10)
    raw_labels = kmeans.fit_predict(features)

    label_map = np.full((h, w), -1, dtype=int)
    label_map[valid_mask] = raw_labels

    # calcule les stats par cluster brut, puis trie par position pour un
    # étiquetage lisible (Z1 en haut à gauche, etc.) plutôt que l'ordre
    # arbitraire renvoyé par k-means
    raw_zones = []
    for cluster_id in range(n_zones):
        cluster_mask = raw_labels == cluster_id
        if not cluster_mask.any():
            continue
        raw_zones.append({
            "raw_id": cluster_id,
            "pixel_count": int(cluster_mask.sum()),
            "mean_value": float(values[cluster_mask].mean()),
            "centroid_row": float(yy[valid_mask][cluster_mask].mean()),
            "centroid_col": float(xx[valid_mask][cluster_mask].mean()),
        })

    raw_zones.sort(key=lambda z: (round(z["centroid_row"] / (h / 3)), z["centroid_col"]))

    zones = []
    id_remap = {}
    for i, z in enumerate(raw_zones):
        zone_id = f"Z{i + 1}"
        id_remap[z["raw_id"]] = i
        zones.append({
            "id": zone_id,
            "pixel_count": z["pixel_count"],
            "area_ratio": round(z["pixel_count"] / valid_mask.sum(), 3),
            "mean_value": round(z["mean_value"], 3),
            "status": classify_zone(z["mean_value"], resolved_thresholds),
            "centroid_row": round(z["centroid_row"], 1),
            "centroid_col": round(z["centroid_col"], 1),
        })

    # remappe label_map pour utiliser le même ordre stable (0-indexé, dans l'ordre de `zones`)
    remapped = np.full_like(label_map, -1)
    for raw_id, new_id in id_remap.items():
        remapped[label_map == raw_id] = new_id
    label_map = remapped

    return label_map, zones


def downsample_label_map(label_map: np.ndarray, out_rows: int = 30, out_cols: int = 40) -> list[list[int]]:
    """
    Réduit le raster de labels (H, W) à une grille out_rows x out_cols par
    échantillonnage au plus proche voisin — suffisant pour un rendu visuel
    mobile sans transmettre chaque pixel satellite individuellement.

    Retourne une liste de listes d'entiers (index de zone, -1 = pas de donnée),
    directement sérialisable en JSON.
    """
    h, w = label_map.shape
    row_idx = np.linspace(0, h - 1, out_rows).astype(int)
    col_idx = np.linspace(0, w - 1, out_cols).astype(int)
    downsampled = label_map[np.ix_(row_idx, col_idx)]
    return downsampled.tolist()


def stats_for_other_raster(raster: np.ndarray, label_map: np.ndarray, zones: list[dict]) -> dict[str, float | None]:
    """
    Réutilise un découpage en zones déjà calculé (sur le NDVI par exemple)
    pour calculer la moyenne d'un AUTRE raster (le NDRE) sur les mêmes zones
    géographiques — plutôt que de reclusteriser indépendamment, ce qui
    donnerait des zones différentes entre indices et rendrait la comparaison
    incohérente.

    Retourne un dict {zone_id: valeur_moyenne (ou None si aucun pixel valide)}.
    """
    result = {}
    for i, z in enumerate(zones):
        zone_mask = (label_map == i) & ~np.isnan(raster)
        result[z["id"]] = round(float(raster[zone_mask].mean()), 3) if zone_mask.any() else None
    return result