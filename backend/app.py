"""
Flask API server — Athinorama Αρχείο Ταινιών.
"""
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request, abort
from flask_cors import CORS

import firebase_client as db
import scraper
from sheets_sync import sync_rows

# ---------------------------------------------------------------------------
# Ρύθμιση logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Αρχικοποίηση Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# CORS: επέτρεψε Netlify domain και localhost
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5500")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

SCRAPE_API_KEY = os.environ.get("SCRAPE_API_KEY", "")
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")


# ---------------------------------------------------------------------------
# Decorators για authentication
# ---------------------------------------------------------------------------

def require_scrape_key(f):
    """Έλεγχος SCRAPE_API_KEY για scraping endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if SCRAPE_API_KEY:
            provided = (
                request.json.get("api_key") if request.is_json else None
            ) or request.headers.get("X-API-Key", "")
            if provided != SCRAPE_API_KEY:
                abort(401, description="Μη έγκυρο API key")
        return f(*args, **kwargs)
    return decorated


def require_sync_key(f):
    """Έλεγχος SYNC_API_KEY για sync endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if SYNC_API_KEY:
            auth_header = request.headers.get("Authorization", "")
            provided_key = ""
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:]
            elif request.is_json:
                provided_key = request.json.get("api_key", "")
            if provided_key != SYNC_API_KEY:
                abort(401, description="Μη έγκυρο Sync API key")
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Athinorama catalog endpoints
# ---------------------------------------------------------------------------

@app.get("/api/movies")
def list_movies():
    """Αναζήτηση ταινιών με φίλτρα."""
    filters = {
        "q": request.args.get("q"),
        "year_from": request.args.get("year_from"),
        "year_to": request.args.get("year_to"),
        "tmdb_min": request.args.get("tmdb_min"),
        "tmdb_max": request.args.get("tmdb_max"),
        "country": request.args.get("country"),
        "genre": request.args.get("genre"),
        "duration_min": request.args.get("duration_min"),
        "duration_max": request.args.get("duration_max"),
        "sort_by": request.args.get("sort_by", "year"),
        "sort_dir": request.args.get("sort_dir", "desc"),
        "page": request.args.get("page", 1),
        "per_page": request.args.get("per_page", 24),
    }
    # Αφαίρεση None τιμών
    filters = {k: v for k, v in filters.items() if v is not None}

    try:
        result = db.get_movies(filters)
        return jsonify(result), 200
    except Exception as e:
        logger.error("Σφάλμα get_movies: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.get("/api/movies/random")
def random_movie():
    """Επιστρέφει τυχαία ταινία."""
    try:
        movie = db.get_random_movie()
        if not movie:
            return jsonify({"error": "Δεν βρέθηκαν ταινίες"}), 404
        return jsonify(movie), 200
    except Exception as e:
        logger.error("Σφάλμα random_movie: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.get("/api/movies/<movie_id>")
def get_movie(movie_id: str):
    """Λεπτομέρειες μίας ταινίας."""
    try:
        movie = db.get_movie(movie_id)
        if not movie:
            return jsonify({"error": "Η ταινία δεν βρέθηκε"}), 404
        return jsonify(movie), 200
    except Exception as e:
        logger.error("Σφάλμα get_movie %s: %s", movie_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.get("/api/filters/meta")
def filters_meta():
    """Επιστρέφει διαθέσιμες χώρες και είδη για dropdowns."""
    try:
        countries = db.get_distinct_countries()
        genres = db.get_distinct_genres()
        return jsonify({"countries": countries, "genres": genres}), 200
    except Exception as e:
        logger.error("Σφάλμα filters_meta: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# Trailer endpoint
# ---------------------------------------------------------------------------

@app.get("/api/movies/<movie_id>/trailer")
def get_movie_trailer(movie_id: str):
    """Επιστρέφει YouTube video ID για το trailer της ταινίας."""
    try:
        movie = db.get_movie(movie_id)
        if not movie:
            return jsonify({"error": "Η ταινία δεν βρέθηκε"}), 404

        # Cached trailer
        if movie.get("yt_trailer_id"):
            return jsonify({"video_id": movie["yt_trailer_id"]}), 200

        # Αναζήτηση YouTube
        video_id = scraper.find_youtube_trailer(
            title=movie.get("title", ""),
            original_title=movie.get("title_original", ""),
            year=movie.get("year"),
        )

        if video_id:
            db.save_movie_trailer(movie_id, video_id)
            return jsonify({"video_id": video_id}), 200

        return jsonify({"video_id": None}), 200

    except Exception as e:
        logger.error("Σφάλμα get_movie_trailer %s: %s", movie_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# IMDb endpoint
# ---------------------------------------------------------------------------

@app.get("/api/movies/<movie_id>/imdb")
def get_movie_imdb(movie_id: str):
    """Επιστρέφει IMDb URL για την ταινία (cached ή αναζητείται on-demand)."""
    try:
        movie = db.get_movie(movie_id)
        if not movie:
            return jsonify({"error": "Η ταινία δεν βρέθηκε"}), 404

        if movie.get("imdb_url"):
            return jsonify({"imdb_url": movie["imdb_url"]}), 200

        imdb_url = scraper.find_imdb_url(
            title=movie.get("title", ""),
            original_title=movie.get("title_original", ""),
            year=movie.get("year"),
        )

        if imdb_url:
            db.save_movie_imdb_url(movie_id, imdb_url)
            return jsonify({"imdb_url": imdb_url}), 200

        return jsonify({"imdb_url": None}), 200

    except Exception as e:
        logger.error("Σφάλμα get_movie_imdb %s: %s", movie_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# TMDB enrichment endpoint
# ---------------------------------------------------------------------------

_ENRICH_FIELDS = (
    "genre", "director", "cast", "cast_roles", "description",
    "imdb_score", "vote_count", "imdb_url", "imdb_id",
    "tagline", "backdrop_path", "original_language", "production_companies",
    "tmdb_trailer_key",
)
# Πεδία που θεωρούνται "νέα" — αν λείπουν από ταινίες με tmdb_id, κάνουμε re-fetch
# Αν λείπει tmdb_enriched_at σημαίνει ότι το enrichment δεν έχει τρέξει ή τρέξει πριν
# προστεθούν τα νέα πεδία (backdrop, cast_roles κ.λπ.). Επιτρέπει ακριβώς ένα re-fetch.
_NEW_FIELDS = ("tmdb_enriched_at",)


@app.get("/api/movies/<movie_id>/enrich")
def enrich_movie(movie_id: str):
    """Εμπλουτισμός ταινίας με δεδομένα από TMDB."""
    try:
        movie = db.get_movie(movie_id)
        if not movie:
            return jsonify({"error": "Η ταινία δεν βρέθηκε"}), 404

        existing_tmdb_id = movie.get("tmdb_id")
        missing_new = any(not movie.get(f) for f in _NEW_FIELDS)

        # Αν έχει tmdb_id ΚΑΙ όλα τα νέα πεδία → τίποτα να κάνουμε
        if existing_tmdb_id and not missing_new:
            return jsonify({**movie, "enriched": False}), 200

        # Αν έχει tmdb_id αλλά λείπουν νέα πεδία → fetch απευθείας με ID (χωρίς search)
        # Επαλήθευση ότι το αποθηκευμένο tmdb_id ανήκει όντως στη σωστή ταινία.
        if existing_tmdb_id:
            tmdb_data = scraper.fetch_tmdb_data_by_id(existing_tmdb_id)
            if tmdb_data and not scraper.tmdb_matches_movie(tmdb_data, movie):
                logger.warning(
                    "tmdb_id=%s δεν ταιριάζει με '%s' (%s) — αγνοείται, νέα αναζήτηση",
                    existing_tmdb_id, movie.get("title"), movie.get("year"),
                )
                # Καθαρισμός λανθασμένου tmdb_id από τη βάση
                db.save_movie_tmdb_data(movie_id, {"tmdb_id": None})
                movie["tmdb_id"] = None
                existing_tmdb_id = None
                tmdb_data = None

        if not existing_tmdb_id:
            tmdb_data = scraper.find_tmdb_data(
                title=movie.get("title", ""),
                original_title=movie.get("title_original", ""),
                year=movie.get("year"),
            )

        if not tmdb_data:
            return jsonify({"enriched": False, **movie}), 200

        # Ενημέρωσε μόνο κενά πεδία (μη αντικατάσταση υπαρχόντων)
        update = {"tmdb_id": tmdb_data["tmdb_id"]}
        for field in _ENRICH_FIELDS:
            if tmdb_data.get(field) and not movie.get(field):
                update[field] = tmdb_data[field]

        # Ειδικές περιπτώσεις: πεδία με διαφορετικό όνομα μεταξύ Athinorama / TMDB
        if tmdb_data.get("original_title") and not movie.get("title_original"):
            update["title_original"] = tmdb_data["original_title"]
        if tmdb_data.get("year") and not movie.get("year"):
            update["year"] = tmdb_data["year"]

        # Σφραγίδα χρόνου: σήμα ότι το enrichment ολοκληρώθηκε (ακόμα και αν κάποια πεδία είναι κενά)
        update["tmdb_enriched_at"] = datetime.now(timezone.utc).isoformat()

        db.save_movie_tmdb_data(movie_id, update)
        movie.update(update)

        return jsonify({**movie, "enriched": True}), 200

    except Exception as e:
        logger.error("Σφάλμα enrich_movie %s: %s", movie_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# Scraping endpoints
# ---------------------------------------------------------------------------

@app.post("/api/scrape/start")
@require_scrape_key
def scrape_start():
    """Έναρξη scraping σε background thread."""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "full")
    if mode not in ("full", "incremental"):
        return jsonify({"error": "Μη έγκυρο mode. Χρησιμοποίησε 'full' ή 'incremental'"}), 400

    full_rescrape = bool(data.get("full_rescrape", False))
    scrape_id = str(uuid.uuid4())

    # Εγγραφή job στο Firestore
    db.create_scrape_job(scrape_id, mode)

    # Εκκίνηση σε background thread
    t = threading.Thread(
        target=scraper.run_scrape,
        args=(scrape_id, mode, full_rescrape),
        daemon=True,
    )
    t.start()

    return jsonify({"scrape_id": scrape_id, "status": "started", "mode": mode}), 202


@app.post("/api/scrape/test")
@require_scrape_key
def scrape_test():
    """Test scraping: σβήνει τη βάση movies και φέρνει 25 ταινίες."""
    scrape_id = str(uuid.uuid4())
    db.create_scrape_job(scrape_id, "test")

    t = threading.Thread(
        target=scraper.run_test_scrape,
        args=(scrape_id,),
        daemon=True,
    )
    t.start()

    return jsonify({"scrape_id": scrape_id, "status": "started", "mode": "test"}), 202


@app.get("/api/scrape/status")
def scrape_status():
    """Progress ενός ή του πιο πρόσφατου scrape job."""
    scrape_id = request.args.get("scrape_id")
    try:
        if scrape_id:
            job = db.get_scrape_job(scrape_id)
        else:
            job = db.get_latest_scrape_job()

        if not job:
            return jsonify({"error": "Δεν βρέθηκε scrape job"}), 404
        return jsonify(job), 200
    except Exception as e:
        logger.error("Σφάλμα scrape_status: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# Personal watchlist endpoints
# ---------------------------------------------------------------------------

@app.get("/api/user/movies")
def get_user_movies():
    """Λίστα personal εγγραφών."""
    list_type = request.args.get("list_type")
    try:
        movies = db.get_user_movies(list_type)
        return jsonify({"movies": movies, "total": len(movies)}), 200
    except Exception as e:
        logger.error("Σφάλμα get_user_movies: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.post("/api/user/movies")
def add_user_movie():
    """Προσθήκη ταινίας στη personal λίστα."""
    data = request.get_json(force=True, silent=True)
    if not data or not data.get("title"):
        return jsonify({"error": "Λείπει ο τίτλος"}), 400
    try:
        doc_id = db.upsert_user_movie(data)
        return jsonify({"id": doc_id, "status": "created"}), 201
    except Exception as e:
        logger.error("Σφάλμα add_user_movie: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.put("/api/user/movies/<doc_id>")
def update_user_movie(doc_id: str):
    """Ενημέρωση personal εγγραφής."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Δεν δόθηκαν δεδομένα"}), 400
    try:
        ok = db.update_user_movie(doc_id, data)
        if not ok:
            return jsonify({"error": "Δεν βρέθηκε η εγγραφή"}), 404
        return jsonify({"status": "updated"}), 200
    except Exception as e:
        logger.error("Σφάλμα update_user_movie %s: %s", doc_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.delete("/api/user/movies/<doc_id>")
def delete_user_movie(doc_id: str):
    """Διαγραφή από personal λίστα."""
    try:
        ok = db.delete_user_movie(doc_id)
        if not ok:
            return jsonify({"error": "Δεν βρέθηκε η εγγραφή"}), 404
        return jsonify({"status": "deleted"}), 200
    except Exception as e:
        logger.error("Σφάλμα delete_user_movie %s: %s", doc_id, e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


@app.post("/api/user/sync")
@require_sync_key
def user_sync():
    """Sync από Google Sheets → Firestore."""
    data = request.get_json(force=True, silent=True) or {}
    sheet = data.get("sheet", "")
    rows = data.get("rows", [])

    if not isinstance(rows, list):
        return jsonify({"error": "Το 'rows' πρέπει να είναι array"}), 400

    try:
        result = sync_rows(sheet, rows)
        return jsonify(result), 200
    except Exception as e:
        logger.error("Σφάλμα user_sync: %s", e)
        return jsonify({"error": "Εσωτερικό σφάλμα"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
