"""Droits des personnes (RGPD) : export (portabilité / accès) et effacement des données.

- export_user_data : toutes les données personnelles d'un compte, sans le mot de passe
  ni son hash.
- delete_user_account / delete_field : effacement RÉEL. Par défaut SQLite peut se
  contenter de marquer les pages comme libres et laisser le contenu supprimé lisible
  dans le fichier. Selon la façon dont SQLite a été compilé, `secure_delete` est actif
  ou non : on l'active donc explicitement, pour ne pas dépendre de la machine.
- pseudonymize_email : identifiant stable mais non réversible pour les logs, afin de
  repérer une attaque sur un compte sans y écrire l'adresse email en clair.
"""

import datetime as dt
import hashlib
import hmac
from pathlib import Path

from src.analyses import list_analyses
from src.auth import get_user
from src.db import get_connection
from src.tokens import get_secret_key
from src.treatments import list_treatments

EXPORT_FORMAT_VERSION = 1


def pseudonymize_email(email: str) -> str:
    """HMAC-SHA256 tronqué de l'email, clé dérivée du secret serveur.

    Un simple hash de l'email se retrouverait par dictionnaire (les adresses sont
    devinables) ; avec une clé secrète, seul le serveur peut le recalculer.
    """
    try:
        key = get_secret_key().encode()
    except RuntimeError:
        return "indisponible"
    return hmac.new(key, b"log-pseudonym:" + email.strip().lower().encode(), hashlib.sha256).hexdigest()[:16]


def export_user_data(db_path: str | Path, user_id: int) -> dict:
    """Toutes les données du compte, dans un format lisible par une machine (JSON)."""
    user = get_user(db_path, user_id)
    if user is None:
        raise LookupError("Compte introuvable.")

    conn = get_connection(db_path)
    try:
        field_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM fields WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()]
    finally:
        conn.close()

    import json

    fields = []
    for row in field_rows:
        polygon = json.loads(row["polygon_geojson"]) if row.get("polygon_geojson") else None
        fields.append({
            "id": row["id"],
            "name": row["name"],
            "crop": row["crop"],
            "sowing_date": row["sowing_date"],
            "area_ha": row["area_ha"],
            "bbox": [row["bbox_min_lon"], row["bbox_min_lat"], row["bbox_max_lon"], row["bbox_max_lat"]],
            "polygon": polygon,
            "created_at": row["created_at"],
            "analyses": [
                {k: a[k] for k in ("id", "source", "method", "created_at", "parameters", "result")}
                for a in reversed(list_analyses(db_path, row["id"]))  # ordre chronologique
            ],
            "treatments": [
                {k: t[k] for k in ("id", "analysis_id", "zone_id", "note", "treated_at")}
                for t in reversed(list_treatments(db_path, row["id"]))
            ],
        })

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "account": {"id": user["id"], "email": user["email"], "created_at": user["created_at"]},
        "fields": fields,
    }


def _count(conn, query: str, params: tuple) -> int:
    return conn.execute(query, params).fetchone()[0]


def delete_user_account(db_path: str | Path, user_id: int) -> dict:
    """Supprime le compte et TOUT ce qui lui est rattaché. Retourne ce qui a été supprimé."""
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")  # efface le contenu, ne se contente pas de libérer les pages
        deleted = {
            "fields": _count(conn, "SELECT COUNT(*) FROM fields WHERE user_id = ?", (user_id,)),
            "analyses": _count(
                conn,
                "SELECT COUNT(*) FROM analyses WHERE field_id IN (SELECT id FROM fields WHERE user_id = ?)",
                (user_id,),
            ),
            "treatments": _count(
                conn,
                "SELECT COUNT(*) FROM zone_treatments WHERE field_id IN (SELECT id FROM fields WHERE user_id = ?)",
                (user_id,),
            ),
        }
        # fields -> analyses -> zone_treatments sont supprimés en cascade (clés étrangères activées)
        conn.execute("DELETE FROM fields WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return deleted
    finally:
        conn.close()


def delete_field(db_path: str | Path, field_id: int, user_id: int) -> dict | None:
    """Supprime un champ appartenant à user_id (avec ses analyses et traitements).
    Retourne ce qui a été supprimé, ou None si le champ n'existe pas / n'est pas à lui."""
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        if _count(conn, "SELECT COUNT(*) FROM fields WHERE id = ? AND user_id = ?", (field_id, user_id)) == 0:
            return None
        deleted = {
            "analyses": _count(conn, "SELECT COUNT(*) FROM analyses WHERE field_id = ?", (field_id,)),
            "treatments": _count(conn, "SELECT COUNT(*) FROM zone_treatments WHERE field_id = ?", (field_id,)),
        }
        conn.execute("DELETE FROM fields WHERE id = ? AND user_id = ?", (field_id, user_id))
        conn.commit()
        return deleted
    finally:
        conn.close()
