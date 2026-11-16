"""Persistence for treatment confirmations recorded against analysis zones."""

import json
from pathlib import Path

from src.db import get_connection


class TreatmentError(Exception):
    """A treatment cannot be saved because its analysis is invalid."""


def save_treatment(
    db_path: str | Path,
    field_id: int,
    analysis_id: int,
    zone_id: str,
    note: str | None = None,
) -> dict:
    normalized_zone_id = zone_id.strip()
    if not normalized_zone_id:
        raise TreatmentError("L'identifiant de zone est obligatoire.")

    conn = get_connection(db_path)
    try:
        analysis = conn.execute(
            "SELECT id, result_json FROM analyses WHERE id = ? AND field_id = ?",
            (analysis_id, field_id),
        ).fetchone()
        if analysis is None:
            raise TreatmentError("Analyse introuvable pour cette parcelle.")

        result = json.loads(analysis["result_json"])
        known_zone_ids = {zone["id"] for zone in result.get("zones", [])}
        if normalized_zone_id not in known_zone_ids:
            raise TreatmentError("Cette zone n'existe pas dans le résultat de cette analyse.")

        zone = next(z for z in result["zones"] if z["id"] == normalized_zone_id)
        if zone.get("status") == "no_data" or ("ndvi" in zone and zone["ndvi"] is None):
            raise TreatmentError(
                "Cette zone est hors de la parcelle ou sans donnée exploitable : "
                "elle ne peut pas être marquée comme traitée."
            )

        # Idempotent : marquer deux fois la même zone d'une même analyse ne crée pas de doublon
        # (double clic, deux appareils, nouvelle tentative après une coupure réseau).
        existing = conn.execute(
            "SELECT * FROM zone_treatments WHERE field_id = ? AND analysis_id = ? AND zone_id = ? ORDER BY id LIMIT 1",
            (field_id, analysis_id, normalized_zone_id),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "already_recorded": True}

        cursor = conn.execute(
            """INSERT INTO zone_treatments (field_id, analysis_id, zone_id, note)
               VALUES (?, ?, ?, ?)""",
            (field_id, analysis_id, normalized_zone_id, note.strip() if note else None),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM zone_treatments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return {**dict(row), "already_recorded": False}
    finally:
        conn.close()


def list_treatments(db_path: str | Path, field_id: int) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM zone_treatments WHERE field_id = ? ORDER BY treated_at DESC, id DESC",
            (field_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()