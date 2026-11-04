"""
Pipeline complet : bandes satellite -> indices -> zonage -> export JSON + carte PNG.

Usage :
    python -m src.main --demo                # données synthétiques (aucune clé requise)
    python -m src.main --bbox <lon,lat,lon,lat>  # vraies données (nécessite SH_CLIENT_ID/SECRET)

Le mode --demo permet de valider toute la logique de traitement sans
dépendre d'une connexion à Sentinel Hub.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

from src.indices import compute_all_indices
from src.zoning import split_into_zones, annotate_zones, DEFAULT_THRESHOLDS, get_thresholds
from src.zoning_kmeans import segment_zones_kmeans, stats_for_other_raster, downsample_label_map
from src.demo_data import generate_synthetic_bands

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# même rampe de couleurs que les prototypes UI (critique -> stress -> sain)
FIELD_CMAP = LinearSegmentedColormap.from_list(
    "field_health", ["#C4623A", "#D9A441", "#9FBB6C", "#6FA059"]
)


def run_pipeline(
    bands: np.ndarray,
    n_rows: int = 8,
    n_cols: int = 12,
    method: str = "grid",
    n_zones: int = 6,
    thresholds: dict | None = None,
) -> dict:
    """
    method="grid"   : grille régulière n_rows x n_cols (rapide, zones fixes)
    method="kmeans" : n_zones zones homogènes réelles (recommandé pour de vraies
                      recommandations agronomiques, cf. compare_zoning.py)
    """
    resolved_thresholds = get_thresholds(thresholds)  # lève ValueError si seuils invalides
    indices_rasters = compute_all_indices(bands)

    if method == "kmeans":
        label_map, ndvi_zones = segment_zones_kmeans(
            indices_rasters["ndvi"], n_zones=n_zones, thresholds=resolved_thresholds
        )
        ndre_by_zone = stats_for_other_raster(indices_rasters["ndre"], label_map, ndvi_zones)
        merged = [{
            "id": z["id"],
            "ndvi": z["mean_value"],
            "ndre": ndre_by_zone[z["id"]],
            "status": z["status"],
            "area_ratio": z["area_ratio"],
        } for z in ndvi_zones]
        grid_info = {"method": "kmeans", "n_zones": len(ndvi_zones)}
        # grille basse résolution pour un rendu visuel côté app mobile
        # (chaque cellule = index dans `merged`, -1 = pas de donnée)
        label_grid = {"rows": 30, "cols": 40, "data": downsample_label_map(label_map, 30, 40)}
    else:
        ndvi_zones = annotate_zones(split_into_zones(indices_rasters["ndvi"], n_rows, n_cols), resolved_thresholds)
        ndre_zones = annotate_zones(split_into_zones(indices_rasters["ndre"], n_rows, n_cols), resolved_thresholds)
        merged = [{
            "id": zv["id"],
            "ndvi": round(zv["mean_value"], 3) if not np.isnan(zv["mean_value"]) else None,
            "ndre": round(zr["mean_value"], 3) if not np.isnan(zr["mean_value"]) else None,
            "status": zv["status"],
            "valid_pixel_ratio": zv["valid_pixel_ratio"],
        } for zv, zr in zip(ndvi_zones, ndre_zones)]
        grid_info = {"method": "grid", "rows": n_rows, "cols": n_cols}

    alerts = [z for z in merged if z["status"] == "critical"]

    return {
        "thresholds_used": resolved_thresholds,
        "grid": grid_info,
        "zones": merged,
        "alert_count": len(alerts),
        "alert_zone_ids": [z["id"] for z in alerts],
        "field_mean_ndvi": (
            None
            if np.all(np.isnan(indices_rasters["ndvi"]))
            else round(float(np.nanmean(indices_rasters["ndvi"])), 3)
        ),
        "label_grid": label_grid if method == "kmeans" else None,
    }, indices_rasters


def save_map_png(raster: np.ndarray, path: Path, title: str):
    plt.figure(figsize=(6, 4.5))
    plt.imshow(raster, cmap=FIELD_CMAP, vmin=0, vmax=1)
    plt.colorbar(label=title)
    plt.title(title)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(path, dpi=150, facecolor="#1C1710")
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="utiliser des données synthétiques")
    parser.add_argument("--bbox", type=str, help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--rows", type=int, default=8)
    parser.add_argument("--cols", type=int, default=12)
    parser.add_argument("--method", choices=["grid", "kmeans"], default="grid")
    parser.add_argument("--n-zones", type=int, default=6, help="nombre de zones (mode kmeans uniquement)")
    parser.add_argument("--critical-threshold", type=float, default=DEFAULT_THRESHOLDS["critical"], help="seuil critique du zonage")
    parser.add_argument("--warning-threshold", type=float, default=DEFAULT_THRESHOLDS["warning"], help="seuil warning du zonage")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    thresholds = {"critical": args.critical_threshold, "warning": args.warning_threshold}

    if args.demo or not args.bbox:
        print("→ Mode démo : génération de bandes synthétiques (pas d'appel réseau).")
        bands = generate_synthetic_bands()
    else:
        from src.fetch_sentinel import SentinelHubConfigError, fetch_field_bands
        try:
            bbox_parts = args.bbox.split(",")
            if len(bbox_parts) != 4:
                raise ValueError("--bbox doit contenir exactement 4 valeurs : min_lon,min_lat,max_lon,max_lat")
            bbox_coords = tuple(float(x) for x in bbox_parts)
        except ValueError as exc:
            print(f"→ Erreur de paramètres : {exc}")
            return 2

        print(f"→ Récupération Sentinel Hub pour bbox={bbox_coords}...")
        try:
            bands = fetch_field_bands(bbox_coords)
        except SentinelHubConfigError as exc:
            print(f"→ Erreur Sentinel Hub : {exc}")
            return 2
        except ValueError as exc:
            print(f"→ Erreur de paramètres : {exc}")
            return 2
        except Exception as exc:  # pragma: no cover - defensive CLI path
            print(f"→ Erreur Sentinel Hub inattendue : {exc}")
            return 2

    result, rasters = run_pipeline(
        bands,
        n_rows=args.rows,
        n_cols=args.cols,
        method=args.method,
        n_zones=args.n_zones,
        thresholds=thresholds,
    )

    json_path = OUTPUT_DIR / "zones.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ Données de zones exportées : {json_path}")

    save_map_png(rasters["ndvi"], OUTPUT_DIR / "carte_ndvi.png", "NDVI")
    save_map_png(rasters["ndre"], OUTPUT_DIR / "carte_ndre.png", "NDRE")
    print(f"→ Cartes exportées dans : {OUTPUT_DIR}")

    print(f"\nNDVI moyen de la parcelle : {result['field_mean_ndvi']}")
    print(f"Zones en alerte critique : {result['alert_count']} ({', '.join(result['alert_zone_ids']) or 'aucune'})")


if __name__ == "__main__":
    sys.exit(main())