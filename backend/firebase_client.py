"""
Firebase Firestore client — αρχικοποίηση και βοηθητικές συναρτήσεις.
"""
import os
import json
import math
import random
import re
import unicodedata
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------------------------------------------------------------------
# Αρχικοποίηση
# ---------------------------------------------------------------------------

_db = None


def _get_db():
    global _db
    if _db is None:
        if not firebase_admin._apps:
            sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
            if not sa_json:
                raise RuntimeError("Λείπει το FIREBASE_SERVICE_ACCOUNT_JSON")
            sa_info = json.loads(sa_json)
            cred = credentials.Certificate(sa_info)
            firebase_admin.initialize_app(cred)
        _db = firestore.client()
    return _db


# ---------------------------------------------------------------------------
# Βοηθητικές
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Μετατροπή κειμένου σε slug για doc IDs."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text.strip("_")[:80]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Movies (Athinorama catalog)
# ---------------------------------------------------------------------------

def save_movie(movie: dict) -> str:
    """Αποθηκεύει ταινία στο Firestore. Επιστρέφει doc ID."""
    db = _get_db()
    doc_id = str(movie["id"])
    db.collection("movies").document(doc_id).set(movie, merge=True)
    return doc_id


def get_movie(movie_id: str) -> dict | None:
    """Επιστρέφει μία ταινία βάσει ID."""
    db = _get_db()
    doc = db.collection("movies").document(str(movie_id)).get()
    if doc.exists:
        return {"id": doc.id, **doc.to_dict()}
    return None


def get_movies(filters: dict) -> dict:
    """
    Αναζήτηση ταινιών με φίλτρα.
    Επιστρέφει dict: movies, total, page, per_page, pages.
    """
    db = _get_db()
    query = db.collection("movies")

    # Φίλτρα που μπορεί να χειριστεί Firestore άμεσα
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    imdb_min = filters.get("imdb_min")
    imdb_max = filters.get("imdb_max")
    country = filters.get("country")
    genre = filters.get("genre")
    duration_min = filters.get("duration_min")
    duration_max = filters.get("duration_max")

    if year_from:
        query = query.where("year", ">=", int(year_from))
    if year_to:
        query = query.where("year", "<=", int(year_to))
    if imdb_min:
        query = query.where("imdb_score", ">=", float(imdb_min))
    if imdb_max:
        query = query.where("imdb_score", "<=", float(imdb_max))
    if country:
        query = query.where("country", "==", country)
    if duration_min:
        query = query.where("duration", ">=", int(duration_min))
    if duration_max:
        query = query.where("duration", "<=", int(duration_max))

    # Ταξινόμηση
    sort_by = filters.get("sort_by", "year")
    sort_dir = filters.get("sort_dir", "desc")
    valid_sorts = {"year", "stars", "title", "duration"}
    if sort_by not in valid_sorts:
        sort_by = "year"
    direction = firestore.Query.DESCENDING if sort_dir == "desc" else firestore.Query.ASCENDING
    query = query.order_by(sort_by, direction=direction)

    # Φέρε όλα (Firestore δεν έχει OFFSET — κάνουμε pagination στον server)
    docs = query.stream()
    movies = []
    q_lower = (filters.get("q") or "").lower()
    genre_lower = (genre or "").lower()

    for doc in docs:
        data = {"id": doc.id, **doc.to_dict()}

        # Fulltext φίλτρο τίτλου (στον server)
        if q_lower:
            title = (data.get("title") or "").lower()
            title_orig = (data.get("title_original") or "").lower()
            if q_lower not in title and q_lower not in title_orig:
                continue

        # Genre φίλτρο (array contains)
        if genre_lower:
            genres = [g.lower() for g in (data.get("genre") or [])]
            if not any(genre_lower in g for g in genres):
                continue

        movies.append(data)

    total = len(movies)
    per_page = int(filters.get("per_page", 24))
    page = int(filters.get("page", 1))
    pages = math.ceil(total / per_page) if total else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    end = start + per_page

    return {
        "movies": movies[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


def get_random_movie() -> dict | None:
    """Επιστρέφει τυχαία ταινία από τη βάση."""
    db = _get_db()
    # Χρησιμοποιούμε random doc ID offset
    docs = list(db.collection("movies").limit(500).stream())
    if not docs:
        return None
    doc = random.choice(docs)
    return {"id": doc.id, **doc.to_dict()}


def save_movie_trailer(movie_id: str, video_id: str) -> None:
    """Αποθηκεύει το YouTube trailer video ID σε υπάρχον movie doc."""
    db = _get_db()
    db.collection("movies").document(str(movie_id)).set(
        {"yt_trailer_id": video_id}, merge=True
    )


def save_movie_imdb_url(movie_id: str, imdb_url: str) -> None:
    """Αποθηκεύει το IMDb URL σε υπάρχον movie doc."""
    db = _get_db()
    db.collection("movies").document(str(movie_id)).set(
        {"imdb_url": imdb_url}, merge=True
    )


def save_movie_tmdb_data(movie_id: str, data: dict) -> None:
    """Αποθηκεύει δεδομένα TMDB enrichment σε υπάρχον movie doc."""
    db = _get_db()
    db.collection("movies").document(str(movie_id)).set(data, merge=True)


def clear_movies_collection() -> int:
    """Διαγράφει όλα τα docs από τη collection movies. Επιστρέφει πόσα διαγράφηκαν."""
    db = _get_db()
    deleted = 0
    while True:
        docs = list(db.collection("movies").limit(500).stream())
        if not docs:
            break
        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        deleted += len(docs)
    return deleted


def get_distinct_countries() -> list[str]:
    """Επιστρέφει λίστα χωρών για dropdown."""
    db = _get_db()
    docs = db.collection("movies").select(["country"]).stream()
    countries = sorted({d.to_dict().get("country", "") for d in docs if d.to_dict().get("country")})
    return countries


def get_distinct_genres() -> list[str]:
    """Επιστρέφει λίστα ειδών για dropdown."""
    db = _get_db()
    docs = db.collection("movies").select(["genre"]).stream()
    genres = set()
    for d in docs:
        for g in (d.to_dict().get("genre") or []):
            genres.add(g)
    return sorted(genres)


# ---------------------------------------------------------------------------
# Scrape jobs
# ---------------------------------------------------------------------------

def create_scrape_job(scrape_id: str, mode: str) -> None:
    """Δημιουργεί νέο scrape job στο Firestore."""
    db = _get_db()
    db.collection("scrape_jobs").document(scrape_id).set({
        "scrape_id": scrape_id,
        "mode": mode,
        "status": "running",
        "total": 0,
        "done": 0,
        "errors": 0,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    })


def update_scrape_job(scrape_id: str, data: dict) -> None:
    """Ενημερώνει το progress ενός scrape job."""
    db = _get_db()
    data["updated_at"] = _now_iso()
    db.collection("scrape_jobs").document(scrape_id).set(data, merge=True)


def get_scrape_job(scrape_id: str) -> dict | None:
    """Επιστρέφει το status ενός scrape job."""
    db = _get_db()
    doc = db.collection("scrape_jobs").document(scrape_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_latest_scrape_job() -> dict | None:
    """Επιστρέφει το πιο πρόσφατο scrape job."""
    db = _get_db()
    docs = (
        db.collection("scrape_jobs")
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.to_dict()
    return None


# ---------------------------------------------------------------------------
# User movies (personal watchlist)
# ---------------------------------------------------------------------------

def upsert_user_movie(entry: dict) -> str:
    """Προσθέτει / ενημερώνει εγγραφή στο user_movies. Επιστρέφει doc ID."""
    db = _get_db()
    list_type = entry.get("list_type", "seen")
    title = entry.get("title", "untitled")
    doc_id = f"{list_type}_{_slugify(title)}"

    entry["created_at"] = entry.get("created_at", _now_iso())
    entry["updated_at"] = _now_iso()

    db.collection("user_movies").document(doc_id).set(entry, merge=True)
    return doc_id


def get_user_movies(list_type: str | None = None) -> list[dict]:
    """Επιστρέφει personal εγγραφές, προαιρετικά φιλτραρισμένες ανά list_type."""
    db = _get_db()
    query = db.collection("user_movies")
    if list_type:
        query = query.where("list_type", "==", list_type)
    docs = query.stream()
    return [{"id": doc.id, **doc.to_dict()} for doc in docs]


def update_user_movie(doc_id: str, data: dict) -> bool:
    """Ενημερώνει εγγραφή στο user_movies. Επιστρέφει True αν υπάρχει."""
    db = _get_db()
    ref = db.collection("user_movies").document(doc_id)
    if not ref.get().exists:
        return False
    data["updated_at"] = _now_iso()
    ref.update(data)
    return True


def delete_user_movie(doc_id: str) -> bool:
    """Διαγράφει εγγραφή από user_movies. Επιστρέφει True αν υπήρχε."""
    db = _get_db()
    ref = db.collection("user_movies").document(doc_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def find_athinorama_id(title: str, year: int | None = None) -> str | None:
    """
    Fuzzy match τίτλου για να βρούμε athinorama_id.
    Αναζήτηση σε title και title_original.
    """
    db = _get_db()
    title_lower = title.lower().strip()

    # Προσπάθεια exact match
    docs = (
        db.collection("movies")
        .where("title", "==", title)
        .limit(5)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        if year is None or data.get("year") == year:
            return doc.id

    # Partial match μέσα στο title πεδίο
    docs_all = db.collection("movies").select(["title", "title_original", "year"]).stream()
    for doc in docs_all:
        data = doc.to_dict()
        t = (data.get("title") or "").lower()
        to = (data.get("title_original") or "").lower()
        if title_lower in t or title_lower in to or t in title_lower:
            if year is None or data.get("year") == year:
                return doc.id

    return None
