"""
Google Sheets → Firestore sync logic.
Δέχεται JSON payload από το Apps Script και κάνει upsert στο user_movies.
"""
import logging
from datetime import datetime, timezone

from firebase_client import upsert_user_movie, find_athinorama_id

logger = logging.getLogger(__name__)

# Αντιστοίχιση sheet name → list_type
SHEET_TO_LIST_TYPE = {
    "movies i have seen": "seen",
    "favourite movies": "favourite",
    "series i have seen": "series_seen",
    "favourite series": "series_favourite",
}


def _normalize_sheet_name(sheet_name: str) -> str:
    """Κανονικοποιεί το όνομα sheet για αντιστοίχιση."""
    return sheet_name.lower().strip()


def _infer_list_type(sheet_name: str, row: dict) -> str:
    """Βρίσκει list_type από το sheet name ή από το ίδιο το row."""
    # Αν το row έχει ήδη list_type
    if row.get("list_type"):
        return row["list_type"]

    # Αντιστοίχιση από sheet name
    normalized = _normalize_sheet_name(sheet_name)
    for key, value in SHEET_TO_LIST_TYPE.items():
        if key in normalized or normalized in key:
            return value

    return "seen"  # default


def _parse_year(val) -> int | None:
    """Μετατρέπει string ή int σε έτος."""
    if val is None:
        return None
    try:
        y = int(str(val).strip())
        if 1900 <= y <= 2100:
            return y
    except (ValueError, TypeError):
        pass
    return None


def _parse_float(val) -> float | None:
    """Μετατρέπει string σε float."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def sync_rows(sheet_name: str, rows: list[dict]) -> dict:
    """
    Κάνει upsert για κάθε row από το Google Sheet.
    Επιστρέφει {"synced": N, "errors": [...]}
    """
    synced = 0
    errors = []

    for i, row in enumerate(rows):
        try:
            title = str(row.get("title") or "").strip()
            if not title:
                # Παράλειψη κενών γραμμών
                continue

            list_type = _infer_list_type(sheet_name, row)
            year = _parse_year(row.get("year"))

            # Προσπάθεια αντιστοίχισης με Athinorama ID
            athinorama_id = find_athinorama_id(title, year)

            entry = {
                "title": title,
                "title_greek": str(row.get("title_greek") or title).strip(),
                "list_type": list_type,
                "year": year,
                "genre": str(row.get("genre") or "").strip(),
                "imdb_score": _parse_float(row.get("imdb_score")),
                "tmdb_score": _parse_float(row.get("tmdb_score")),
                "rotten_tomatoes": str(row.get("rotten_tomatoes") or "").strip(),
                "cast": str(row.get("cast") or "").strip(),
                "director": str(row.get("director") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "duration": str(row.get("duration") or "").strip(),
                "trailer_link": str(row.get("trailer_link") or "").strip(),
                "poster": str(row.get("poster") or "").strip(),
                "similar": str(row.get("similar") or "").strip(),
                "athinorama_id": athinorama_id,
                "synced_from_sheets": True,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }

            # Αφαίρεση None τιμών για καθαρότητα doc
            entry = {k: v for k, v in entry.items() if v is not None and v != ""}

            upsert_user_movie(entry)
            synced += 1
            logger.debug("✓ Sync: %s [%s]", title, list_type)

        except Exception as e:
            err_msg = f"Row {i} ('{row.get('title', '?')}'): {e}"
            errors.append(err_msg)
            logger.error("✗ Σφάλμα sync: %s", err_msg)

    return {"synced": synced, "errors": errors}
