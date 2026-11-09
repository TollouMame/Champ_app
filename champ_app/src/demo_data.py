"""
Générateur de bandes synthétiques, utilisé quand aucune connexion à Sentinel
Hub n'est disponible (pas de clé API, ou environnement sans accès réseau).

Simule une parcelle avec une zone de stress (drainage) et une zone saine,
pour pouvoir tester tout le pipeline de calcul d'indices et de zonage.
"""

import numpy as np


def synthetic_size_for_bbox(bbox: tuple[float, float, float, float], long_side: int = 160, min_side: int = 32) -> tuple[int, int]:
    """(hauteur, largeur) d'une trame de démonstration qui a les PROPORTIONS RÉELLES du champ.

    Avant, la démonstration était toujours en 120 x 160 : un champ en longueur était étiré en
    4:3 sur la carte. Le côté court est borné (min_side) pour que le coin masqué de la
    démonstration ne recouvre jamais toute la trame.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    width_m = max((max_lon - min_lon) * 111_320 * math.cos(math.radians((min_lat + max_lat) / 2)), 1e-9)
    height_m = max((max_lat - min_lat) * 111_320, 1e-9)
    if width_m >= height_m:
        return max(min_side, round(long_side * height_m / width_m)), long_side
    return long_side, max(min_side, round(long_side * width_m / height_m))


def generate_synthetic_bands(height: int = 120, width: int = 160, seed: int = 0) -> np.ndarray:
    """
    Retourne un array (H, W, 4) = [B04 (rouge), B05 (red edge), B08 (NIR), dataMask]
    avec des valeurs de réflectance plausibles (échelle 0-1) et un patron
    spatial reproductible (zone de stress + zone saine + bruit).
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]

    # zone de stress hydrique dans un coin (simule un problème de drainage réel)
    stress = np.exp(-(((xx - width * 0.15) ** 2 + (yy - height * 0.8) ** 2) / (2 * (width * 0.18) ** 2)))
    # zone saine au centre
    healthy = np.exp(-(((xx - width * 0.55) ** 2 + (yy - height * 0.4) ** 2) / (2 * (width * 0.35) ** 2)))

    # NDVI cible synthétique entre ~0.15 (stress) et ~0.85 (sain)
    target_ndvi = 0.55 + 0.35 * healthy - 0.45 * stress
    target_ndvi += rng.normal(0, 0.03, size=target_ndvi.shape)
    target_ndvi = np.clip(target_ndvi, 0.02, 0.95)

    # on construit des réflectances rouge/NIR cohérentes avec ce NDVI cible
    nir = np.clip(0.3 + 0.35 * target_ndvi + rng.normal(0, 0.01, target_ndvi.shape), 0.05, 0.9)
    red = np.clip(nir * (1 - target_ndvi) / (1 + target_ndvi), 0.02, 0.5)
    red_edge = np.clip((red + nir) / 2 - 0.05 * healthy, 0.02, 0.7)

    data_mask = np.ones_like(target_ndvi)
    # on simule un petit coin hors-parcelle (masqué), comme un vrai raster satellite
    data_mask[:15, :15] = 0

    bands = np.stack([red, red_edge, nir, data_mask], axis=-1).astype(np.float32)
    return bands
