"""Persistence and retrieval of field analysis results."""

import json
from pathlib import Path

from src.db import get_connection


def save_analysis(
    db_path: str | Path,
    field_id: int,
    source: str,
    method: str,
    parameters: dict,
    result: dict,
) -> dict:
    """Store one completed analysis and return its public metadata."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO analyses (field_id, source, method, parameters_json, result_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                field_id,
                source,
                method,
                # allow_nan=False : un NaN ne doit jamais être persisté (il rendrait
                # ensuite toute la liste d'analyses du champ illisible en JSON)
                json.dumps(parameters, ensure_ascii=False, allow_nan=False),
                json.dumps(result, ensure_ascii=False, allow_nan=False),
            ),
        )
        conn.commit()
        analysis_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        return _decode_analysis(dict(row))
    finally:
        conn.close()


def list_analyses(db_path: str | Path, field_id: int) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM analyses WHERE field_id = ? ORDER BY created_at DESC, id DESC",
            (field_id,),
        ).fetchall()
        return [_decode_analysis(dict(row)) for row in rows]
    finally:
        conn.close()


def _loads_lenient(raw: str):
    """json.loads qui remplace NaN/Infinity par None, pour que les analyses déjà
    enregistrées avec un NaN (avant ce correctif) restent affichables."""
    return json.loads(raw, parse_constant=lambda _constant: None)


def _decode_analysis(analysis: dict) -> dict:
    return {
        "id": analysis["id"],
        "field_id": analysis["field_id"],
        "source": analysis["source"],
        "method": analysis["method"],
        "parameters": _loads_lenient(analysis["parameters_json"]),
        "result": _loads_lenient(analysis["result_json"]),
        "created_at": analysis["created_at"],
    }
