"""
Scraper για τις κριτικές ταινιών του Athinorama.
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
ARCHIVE_URL = f"{BASE_URL}/cinema/cinema-reviews/"

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
    """Μετατροπή αστεριών Athinorama (π.χ. '3' ή '2,5') σε float."""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _parse_duration(text: str) -> int | None:
    """Μετατροπή διάρκειας (π.χ. '100΄' ή '85 λεπτά') σε λεπτά (int)."""
    if not text:
        return None
    # Μορφή ωρ:λεπτ
    m = re.search(r"(\d+):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Μορφή N λεπτά / N min / N΄
    m = re.search(r"(\d+)\s*(?:λεπτ|min|΄|')", text, re.IGNORECASE)
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


def _parse_meta_block(text: str) -> dict:
    """
    Εξαγωγή country/year/duration από το metadata block της ταινίας.
    Μορφή: 'Χώρα1, Χώρα2. 2025. Διάρκεια: 100΄. Διανομή: X'
    """
    result = {"country": "", "year": None, "duration": None}

    # Έτος
    m = re.search(r"\b(19|20)\d{2}\b", text)
    if m:
        result["year"] = int(m.group())

    # Διάρκεια
    m = re.search(r"Διάρκεια[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        result["duration"] = int(m.group(1))

    # Χώρα: το πρώτο τμήμα πριν την πρώτη τελεία που περιέχει έτος
    parts = text.split(".")
    if parts:
        country_part = parts[0].strip()
        # Αφαιρούμε αν είναι μόνο αριθμός (έτος)
        if not re.match(r"^\d+$", country_part):
            result["country"] = country_part

    return result


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


def _extract_movie_links(soup: BeautifulSoup) -> list[str]:
    """Εξάγει μοναδικά URLs ταινιών από μια σελίδα λίστας."""
    seen = set()
    results = []
    for a in soup.select("a[href*='/cinema/cinema-reviews/']"):
        href = a.get("href", "")
        if not href:
            continue
        full_url = href if href.startswith("http") else BASE_URL + href
        # Φιλτράρουμε: πρέπει να έχει ID (αριθμό) μετά το /cinema-reviews/
        if re.search(r"/cinema-reviews/\d+/", full_url) and full_url not in seen:
            seen.add(full_url)
            results.append(full_url)
    return results


def _discover_full() -> Generator[str, None, None]:
    """Scraping ολόκληρου αρχείου μέσω paginated λίστας."""
    page = 1
    while True:
        url = f"{ARCHIVE_URL}?page={page}"
        resp = _safe_get(url)
        if resp is None:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        links = _extract_movie_links(soup)

        if not links:
            logger.info("Δεν βρέθηκαν ταινίες στη σελίδα %d — τέλος.", page)
            break

        yield from links

        next_link = soup.select_one("a.pagination__next, a.next-page, a[rel='next']")
        if not next_link:
            break
        page += 1


def _discover_recent() -> Generator[str, None, None]:
    """Incremental mode: μόνο τελευταίοι 2 μήνες."""
    now = datetime.now(timezone.utc)
    months_to_check = [(now.year, now.month)]
    prev = now.replace(day=1) - timedelta(days=1)
    months_to_check.append((prev.year, prev.month))

    for year, month in months_to_check:
        url = f"{ARCHIVE_URL}?year={year}&month={month:02d}"
        resp = _safe_get(url)
        if resp is None:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        yield from _extract_movie_links(soup)


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

    movie_id = _extract_id_from_url(url)
    if not movie_id:
        logger.warning("Δεν βρέθηκε ID στο URL: %s", url)
        return None

    def _text(selector: str, default: str = "") -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else default

    # Τίτλος — πρώτο h1 της σελίδας
    title = _text("h1") or _text("title").split("|")[0].strip()

    # Πρωτότυπος τίτλος
    title_original = ""
    orig_el = soup.select_one(".original-title")
    if orig_el:
        span = orig_el.select_one("span")
        title_original = span.get_text(strip=True) if span else orig_el.get_text(strip=True)

    # Αστεράκια — από το span μέσα στο .rating-stars
    stars = None
    rating_el = soup.select_one(".rating-stars")
    if rating_el:
        span = rating_el.select_one("span")
        stars_text = span.get_text(strip=True) if span else rating_el.get_text(strip=True)
        stars = _parse_stars(stars_text)

    # Metadata block (em tag): "Χώρα. Έτος. Διάρκεια: N΄. Διανομή: X"
    year = None
    country = ""
    duration = None
    for em in soup.find_all("em"):
        em_text = em.get_text(strip=True)
        if re.search(r"\b(19|20)\d{2}\b", em_text) or "Διάρκεια" in em_text:
            meta = _parse_meta_block(em_text)
            year = meta["year"]
            country = meta["country"]
            duration = meta["duration"]
            break

    # Διάρκεια fallback από span
    if duration is None:
        for span in soup.find_all("span"):
            if "Διάρκεια" in span.get_text():
                duration = _parse_duration(span.get_text(strip=True))
                if duration:
                    break

    # Poster — η εικόνα της ταινίας (από ImagesDatabase, όχι thumbnail)
    poster_url = ""
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "")
        # Η κύρια εικόνα έχει width >= 100 και src από ImagesDatabase
        if "/Content/ImagesDatabase/" in src and img.get("width") and int(img.get("width", 0)) >= 100:
            poster_url = src if src.startswith("http") else BASE_URL + src
            break

    return {
        "id": movie_id,
        "title": title,
        "title_original": title_original,
        "year": year,
        "country": country,
        "genre": [],
        "director": [],
        "cast": [],
        "stars": stars,
        "duration": duration,
        "poster_url": poster_url,
        "athinorama_url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Κύρια συνάρτηση scraping
# ---------------------------------------------------------------------------

def find_youtube_trailer(title: str, original_title: str = "", year: int | None = None) -> str | None:
    """
    Ψάχνει YouTube για trailer της ταινίας.
    Επιστρέφει το πρώτο video ID ή None.
    """
    query = f"{title} trailer"
    if year:
        query += f" {year}"

    import urllib.parse
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"

    try:
        resp = SESSION.get(search_url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("YouTube search αποτυχία για '%s': %s", title, e)
        return None

    # Εξαγωγή video IDs από το HTML (ytInitialData)
    video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text)
    # Αφαίρεση duplicates διατηρώντας σειρά
    seen = set()
    unique_ids = []
    for vid in video_ids:
        if vid not in seen:
            seen.add(vid)
            unique_ids.append(vid)

    if unique_ids:
        logger.info("Βρέθηκε trailer για '%s': %s", title, unique_ids[0])
        return unique_ids[0]

    # Fallback: δοκιμή με original title
    if original_title and original_title != title:
        return find_youtube_trailer(original_title, year=year)

    return None


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

            update_scrape_job(scrape_id, {
                "total": total_found,
                "done": done,
                "errors": errors,
                "status": "running",
                "current_url": movie_url,
            })

            movie_id = _extract_id_from_url(movie_url)
            if movie_id and not full_rescrape:
                existing = get_movie(movie_id)
                if existing:
                    done += 1
                    continue

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

    update_scrape_job(scrape_id, {
        "status": "completed",
        "total": total_found,
        "done": done,
        "errors": errors,
    })
    logger.info("Ολοκλήρωση scraping [%s]: %d/%d ταινίες, %d σφάλματα", scrape_id, done, total_found, errors)
