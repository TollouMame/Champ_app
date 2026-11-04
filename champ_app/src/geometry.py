"""Masquage d'un raster satellite par le contour réel d'une parcelle.

Sentinel Hub renvoie l'image de la *bbox* de la parcelle. Pour une parcelle qui
n'est pas un rectangle (L, trapèze, contour irrégulier), tout ce qui est dans
la bbox mais hors du contour (route, parcelle voisine, bois) fausserait les
moyennes et les alertes. On invalide donc ces pixels (dataMask = 0) : ils
deviennent NaN dans les indices et sont ignorés par le zonage.
"""

import numpy as np

# En dessous de cette part de pixels exploitables (hors nuages, dans la parcelle),
# l'analyse est refusée : la moyenne ne représenterait plus la parcelle.
MIN_VALID_PIXEL_RATIO = 0.2
# En dessous de celle-ci, l'analyse est produite mais signalée comme peu fiable.
LOW_QUALITY_PIXEL_RATIO = 0.6


def polygon_mask(
    polygon: list[tuple[float, float]] | list[list[float]],
    bbox: tuple[float, float, float, float],
    shape: tuple[int, int],
) -> np.ndarray:
    """Masque booléen (H, W) : True pour les pixels dont le centre est dans le polygone.

    polygon : [(lon, lat), ...] ; bbox : (min_lon, min_lat, max_lon, max_lat) ;
    shape   : (H, W) du raster. Convention d'un raster Sentinel Hub : la ligne 0
    est le nord (max_lat), la colonne 0 est l'ouest (min_lon).
    Règle pair-impair (ray casting), vectorisée sur les pixels.
    """
    height, width = shape
    min_lon, min_lat, max_lon, max_lat = bbox

    lons = min_lon + (np.arange(width) + 0.5) / width * (max_lon - min_lon)
    lats = max_lat - (np.arange(height) + 0.5) / height * (max_lat - min_lat)
    x, y = np.meshgrid(lons, lats)

    points = np.asarray(polygon, dtype=float)
    inside = np.zeros(shape, dtype=bool)
    previous = len(points) - 1
    for current in range(len(points)):
        xi, yi = points[current]
        xj, yj = points[previous]
        if yi != yj:
            crosses = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
            inside ^= crosses
        previous = current
    return inside


def prepare_bands(
    bands: np.ndarray,
    bbox: tuple[float, float, float, float],
    polygon: list | None = None,
) -> tuple[np.ndarray, float]:
    """Applique le contour de la parcelle et mesure la qualité des données.

    bands : (H, W, 4) = [B04, B05, B08, dataMask]. Retourne (bandes masquées,
    part des pixels de la parcelle réellement exploitables) ; le tableau
    d'entrée n'est pas modifié. Lève ValueError si la parcelle ne couvre aucun
    pixel (trop petite pour cette résolution).
    """
    height, width = bands.shape[:2]
    inside = polygon_mask(polygon, bbox, (height, width)) if polygon else np.ones((height, width), dtype=bool)

    if not inside.any():
        raise ValueError("La parcelle est trop petite pour cette résolution : aucun pixel ne la recouvre.")

    clear = bands[:, :, 3] > 0
    valid_ratio = float((clear & inside).sum() / inside.sum())

    masked = bands.copy()
    masked[:, :, 3] = np.where(inside, bands[:, :, 3], 0)
    return masked, valid_ratio
