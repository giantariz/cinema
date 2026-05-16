"""
Scraper για το αρχείο ταινιών του Athinorama.
Rate limiting: 1-2 δευτερόλεπτα delay μεταξύ requests.
"""
import logging
import re
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Generator

import requests
from bs4 import BeautifulSoup

from firebase_client import (
    save_movie,
    get_movie,
    update_scrape_job,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.athinorama.gr"
ARCHIVE_URL = f"{BASE_URL}/movies/archive/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Βοηθητικές
# ---------------------------------------------------------------------------

def _sleep():
    """Rate limiting: 1-2 δευτερόλεπτα pause."""
    time.sleep(random.uniform(1.0, 2.0))


def _safe_get(url: str, retries: int = 3) -> requests.Response | None:
    """GET με retry logic και rate limiting."""
    for attempt in range(retries):
        try:
            _sleep()
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.warning("Αποτυχία GET %s (attempt %d/%d): %s", url, attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _parse_stars(text: str) -> float | None:
    """Μετατροπή αστεριών Athinorama (π.χ. '3.5') σε float."""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _parse_duration(text: str) -> int | None:
    """Μετατροπή διάρκειας (π.χ. '120 λεπτά' ή '2:00') σε λεπτά (int)."""
    if not text:
        return None
    # Μορφή ωρ:λεπτ
    m = re.search(r"(\d+):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Μορφή N λεπτά / N min
    m = re.search(r"(\d+)\s*(?:λεπτ|min)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Απλός αριθμός
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def _extract_id_from_url(url: str) -> str | None:
    """Εξαγωγή Athinorama movie ID από το URL."""
    m = re.search(r"/(\d+)(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Discovery URLs ταινιών
# ---------------------------------------------------------------------------

def discover_movie_urls(mode: str = "full") -> Generator[str, None, None]:
    """
    Ανακαλύπτει URLs ταινιών από το αρχείο Athinorama.
    mode='full' → όλο το αρχείο
    mode='incremental' → μόνο τρέχων + προηγούμενος μήνας
    """
    if mode == "incremental":
        yield from _discover_recent()
    else:
        yield from _discover_full()


def _discover_full() -> Generator[str, None, None]:
    """Scraping ολόκληρου αρχείου μέσω paginated λίστας."""
    page = 1
    while True:
        url = f"{ARCHIVE_URL}?page={page}"
        resp = _safe_get(url)
        if resp is None:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        # Αναζήτηση links ταινιών — προσαρμογή βάσει πραγματικής δομής HTML
        links = soup.select("a[href*='/movies/movie/']")
        if not links:
            # Δοκιμή εναλλακτικών selectors
            links = soup.select("a[href*='/movies/']")

        if not links:
            logger.info("Δεν βρέθηκαν ταινίες στη σελίδα %d — τέλος.", page)
            break

        seen = set()
        for a in links:
            href = a.get("href", "")
            if not href:
                continue
            full_url = href if href.startswith("http") else BASE_URL + href
            if full_url not in seen and "/movie/" in full_url:
                seen.add(full_url)
                yield full_url

        # Έλεγχος για επόμενη σελίδα
        next_link = soup.select_one("a[rel='next'], .pagination .next a, a.next")
        if not next_link:
            break
        page += 1


def _discover_recent() -> Generator[str, None, None]:
    """Incremental mode: μόνο τελευταίοι 2 μήνες."""
    now = datetime.now(timezone.utc)
    months_to_check = [
        (now.year, now.month),
    ]
    # Προηγούμενος μήνας
    prev = now.replace(day=1) - timedelta(days=1)
    months_to_check.append((prev.year, prev.month))

    for year, month in months_to_check:
        url = f"{ARCHIVE_URL}?year={year}&month={month:02d}"
        resp = _safe_get(url)
        if resp is None:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='/movies/movie/']")
        for a in links:
            href = a.get("href", "")
            if not href:
                continue
            full_url = href if href.startswith("http") else BASE_URL + href
            yield full_url


# ---------------------------------------------------------------------------
# Scraping λεπτομερειών ταινίας
# ---------------------------------------------------------------------------

def scrape_movie_details(url: str) -> dict | None:
    """
    Scraping λεπτομερειών μίας ταινίας από το URL της.
    Επιστρέφει dict με τα πεδία ή None αν αποτύχει.
    """
    resp = _safe_get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Εξαγωγή ID από URL
    movie_id = _extract_id_from_url(url)
    if not movie_id:
        logger.warning("Δεν βρέθηκε ID στο URL: %s", url)
        return None

    def _text(selector: str, default: str = "") -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else default

    def _texts(selector: str) -> list[str]:
        return [el.get_text(strip=True) for el in soup.select(selector)]

    # Τίτλοι
    title = (
        _text("h1.movie-title")
        or _text("h1.title")
        or _text("h1")
        or _text(".movie-header h1")
        or _text("title").split("|")[0].strip()
    )

    title_original = (
        _text(".original-title")
        or _text(".movie-original-title")
        or _text("[class*='original']")
    )

    # Έτος παραγωγής
    year_text = (
        _text(".movie-year")
        or _text("[class*='year']")
        or _text(".production-year")
    )
    year = None
    if year_text:
        m = re.search(r"(19|20)\d{2}", year_text)
        if m:
            year = int(m.group())

    # Χώρα
    country = (
        _text(".movie-country")
        or _text("[class*='country']")
    )

    # Είδος (genre)
    genre_els = soup.select(".movie-genre a, [class*='genre'] a, .genres a")
    genre = [el.get_text(strip=True) for el in genre_els]
    if not genre:
        genre_text = _text(".movie-genre") or _text("[class*='genre']")
        genre = [g.strip() for g in re.split(r"[,/]", genre_text) if g.strip()]

    # Σκηνοθεσία
    director_els = soup.select(".movie-director a, [class*='director'] a")
    director = [el.get_text(strip=True) for el in director_els]
    if not director:
        dir_text = _text(".movie-director") or _text("[class*='director']")
        director = [d.strip() for d in dir_text.split(",") if d.strip()]

    # Ηθοποιοί
    cast_els = soup.select(".movie-cast a, [class*='cast'] a, .actors a")
    cast = [el.get_text(strip=True) for el in cast_els]
    if not cast:
        cast_text = _text(".movie-cast") or _text("[class*='cast']") or _text(".actors")
        cast = [c.strip() for c in cast_text.split(",") if c.strip()]

    # Αστεράκια Athinorama
    stars_text = (
        _text("[class*='rating']")
        or _text("[class*='stars']")
        or _text(".movie-rating")
    )
    stars = _parse_stars(stars_text)

    # Διάρκεια
    duration_text = (
        _text(".movie-duration")
        or _text("[class*='duration']")
        or _text("[class*='runtime']")
    )
    duration = _parse_duration(duration_text)

    # Poster URL
    poster_el = soup.select_one(
        "img.movie-poster, .movie-poster img, [class*='poster'] img, img[class*='poster']"
    )
    poster_url = ""
    if poster_el:
        poster_url = poster_el.get("src") or poster_el.get("data-src") or ""
        if poster_url and not poster_url.startswith("http"):
            poster_url = BASE_URL + poster_url

    return {
        "id": movie_id,
        "title": title,
        "title_original": title_original,
        "year": year,
        "country": country,
        "genre": genre,
        "director": director,
        "cast": cast[:20],  # Περιορισμός στους 20 πρώτους
        "stars": stars,
        "duration": duration,
        "poster_url": poster_url,
        "athinorama_url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Κύρια συνάρτηση scraping
# ---------------------------------------------------------------------------

def run_scrape(scrape_id: str, mode: str = "full", full_rescrape: bool = False) -> None:
    """
    Εκτελεί scraping. Καλείται σε background thread.
    - mode: 'full' ή 'incremental'
    - full_rescrape: αν True, αντικαθιστά υπάρχοντα docs
    """
    logger.info("Έναρξη scraping [%s] mode=%s full_rescrape=%s", scrape_id, mode, full_rescrape)

    urls_seen = set()
    done = 0
    errors = 0
    total_found = 0

    try:
        for movie_url in discover_movie_urls(mode):
            if movie_url in urls_seen:
                continue
            urls_seen.add(movie_url)
            total_found += 1

            # Ενημέρωση progress
            update_scrape_job(scrape_id, {
                "total": total_found,
                "done": done,
                "errors": errors,
                "status": "running",
                "current_url": movie_url,
            })

            # Έλεγχος αν ήδη υπάρχει (incremental)
            movie_id = _extract_id_from_url(movie_url)
            if movie_id and not full_rescrape:
                existing = get_movie(movie_id)
                if existing:
                    done += 1
                    continue

            # Scraping λεπτομερειών
            try:
                data = scrape_movie_details(movie_url)
                if data:
                    save_movie(data)
                    done += 1
                    logger.debug("✓ Αποθηκεύτηκε: %s (%s)", data.get("title"), movie_id)
                else:
                    errors += 1
                    logger.warning("✗ Αποτυχία scraping: %s", movie_url)
            except Exception as e:
                errors += 1
                logger.error("✗ Σφάλμα για %s: %s", movie_url, e)

    except Exception as e:
        logger.error("Κρίσιμο σφάλμα scraping: %s", e)
        update_scrape_job(scrape_id, {
            "status": "error",
            "error_message": str(e),
            "total": total_found,
            "done": done,
            "errors": errors,
        })
        return

    # Ολοκλήρωση
    update_scrape_job(scrape_id, {
        "status": "completed",
        "total": total_found,
        "done": done,
        "errors": errors,
    })
    logger.info("Ολοκλήρωση scraping [%s]: %d/%d ταινίες, %d σφάλματα", scrape_id, done, total_found, errors)
