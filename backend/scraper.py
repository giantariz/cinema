"""
Scraper για τις κριτικές ταινιών του Athinorama.
Rate limiting: 1-2 δευτερόλεπτα delay μεταξύ requests.
"""
import logging
import os
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
    clear_movies_collection,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.athinorama.gr"
ARCHIVE_URL = f"{BASE_URL}/cinema/cinema-reviews/"
MOVIEARCHIVE_URL = f"{BASE_URL}/cinema/moviearchive/"

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

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"


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


# Γνωστά είδη ταινιών (Athinorama)
_KNOWN_GENRES = [
    "Επιστημονικής Φαντασίας", "Ρομαντική Κωμωδία", "Βιογραφικό Δράμα",
    "Περιπέτεια", "Ντοκιμαντέρ", "Βιογραφικό", "Βιογραφία",
    "Ψυχολογικό", "Αστυνομική", "Αστυνομικό", "Κωμωδία", "Ρομάντζο",
    "Ρομαντική", "Φαντασίας", "Ιστορικό", "Ιστορική", "Μυστηρίου",
    "Animation", "Παιδικό", "Μουσικό", "Μουσική", "Δράσης",
    "Κοινωνικό", "Θρίλλερ", "Θρίλερ", "Τρόμος", "Τρόμου", "Δράμα",
]
# Ελληνικά άρθρα / λέξεις που ξεκινούν πρόταση (δεν είναι ονοματεπώνυμο)
_GREEK_SENTENCE_STARTERS = {
    "Μια", "Μία", "Ένας", "Ένα", "Ο", "Η", "Το", "Οι", "Τα", "Τους", "Τις",
    "Στην", "Στον", "Στο", "Στα", "Στους", "Με", "Όταν", "Αν", "Σε", "Από",
    "Κατά", "Για", "Προς", "Ως", "Είναι", "Ήταν", "Αυτός", "Αυτή", "Αυτό",
}


def _extract_from_dirty_description(text: str) -> dict:
    """
    Ανιχνεύει αν η περιγραφή περιέχει embedded metadata του Athinorama
    (μορφή: '[title] [genre] [year] Διάρκεια: N΄ [Director] [synopsis]').
    Αν ναι, εξάγει genre, director και καθαρή description.
    """
    result = {"genre": [], "director": [], "description": text}

    # Ψάχνουμε "Διάρκεια: N΄" στις πρώτες 350 χαρακτήρες
    dur_match = re.search(r"Διάρκεια:\s*\d+\s*[΄΄'']\s*", text[:350])
    if not dur_match:
        return result

    prefix = text[:dur_match.start()]
    after_dur = text[dur_match.end():].strip()

    # Εξαγωγή είδους από το prefix (longest match πρώτα)
    for genre in sorted(_KNOWN_GENRES, key=len, reverse=True):
        if genre in prefix:
            result["genre"] = [genre]
            break

    # Εξαγωγή σκηνοθέτη: κεφαλαία ονόματα στην αρχή του after_dur
    words = after_dur.split()
    skip = 0
    director_parts = []
    for i, word in enumerate(words[:6]):
        clean_word = re.sub(r"[.,;]$", "", word)
        if clean_word in _GREEK_SENTENCE_STARTERS:
            break
        if re.match(r"^[Α-ΩΆΈΉΊΌΎΏA-ZÀ-Ö]", clean_word):
            director_parts.append(clean_word)
            skip = i + 1
        else:
            break

    if director_parts:
        result["director"] = [" ".join(director_parts)]

    clean_desc = " ".join(words[skip:]).strip()
    result["description"] = clean_desc or after_dur

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
        if re.search(r"/cinema-reviews/\d+/", full_url) and full_url not in seen:
            seen.add(full_url)
            results.append(full_url)
    return results


def _extract_movie_links_flexible(soup: BeautifulSoup) -> list[str]:
    """Εξάγει URLs ταινιών από moviearchive ή cinema-reviews σελίδες."""
    seen = set()
    results = []
    patterns = [
        r"/cinema/cinema-reviews/\d+",
        r"/cinema/movies/\d+",
        r"/cinema/\w+-reviews/\d+",
    ]
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue
        full_url = href if href.startswith("http") else BASE_URL + href
        for pattern in patterns:
            if re.search(pattern, full_url) and full_url not in seen:
                seen.add(full_url)
                results.append(full_url)
                break
    return results


def _discover_full() -> Generator[str, None, None]:
    """Scraping ολόκληρου αρχείου: πρώτα cinema-reviews, μετά moviearchive (1-10)."""
    # Φάση 1: cinema-reviews paginated archive
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

    # Φάση 2: moviearchive pages (βαθμολογία 1=0.5★ εως 10=5★)
    yield from _discover_moviearchive()


def _discover_moviearchive() -> Generator[str, None, None]:
    """
    Scraping του αρχείου moviearchive του Athinorama.
    10 σελίδες βαθμολογίας (1=0.5★ εως 10=5★), καθεμία με pagination.
    Περιέχει ~17.000 ταινίες συνολικά.
    """
    seen: set[str] = set()
    for rating in range(1, 11):
        page = 1
        while True:
            url = f"{MOVIEARCHIVE_URL}{rating}"
            if page > 1:
                url += f"?page={page}"

            resp = _safe_get(url)
            if resp is None:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            links = _extract_movie_links_flexible(soup)

            new_links = [l for l in links if l not in seen]
            if not new_links:
                logger.info("moviearchive/%d σελίδα %d: χωρίς νέες ταινίες — τέλος.", rating, page)
                break

            for link in new_links:
                seen.add(link)
                yield link

            next_link = soup.select_one(
                "a.pagination__next, a[rel='next'], .pager a.next, li.next a, a.next"
            )
            if not next_link:
                break
            page += 1
        logger.info("moviearchive/%d: ολοκληρώθηκε.", rating)


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
        if "/Content/ImagesDatabase/" in src and img.get("width") and int(img.get("width", 0)) >= 100:
            poster_url = src if src.startswith("http") else BASE_URL + src
            break

    # Περιγραφή — δοκιμή γνωστών selectors του Athinorama
    description = ""
    for selector in [
        ".article-description",
        ".movie-synopsis",
        ".synopsis",
        ".review-intro",
        ".article-intro",
        ".item-description",
        ".review-body > p:first-child",
        "article .text > p:first-child",
        ".page-content > p:first-child",
    ]:
        el = soup.select_one(selector)
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 30:
                description = txt
                break

    # Fallback: πρώτη αρκετά μεγάλη παράγραφος στο κύριο περιεχόμενο
    if not description:
        for p in soup.select("article p, .content p, main p, .text p"):
            txt = p.get_text(" ", strip=True)
            if len(txt) > 80:
                description = txt
                break

    # Αν η περιγραφή περιέχει embedded metadata, εξάγουμε genre/director/clean desc
    genre: list[str] = []
    director: list[str] = []
    extracted = _extract_from_dirty_description(description)
    genre = extracted["genre"]
    director = extracted["director"]
    description = extracted["description"]

    return {
        "id": movie_id,
        "title": title,
        "title_original": title_original,
        "year": year,
        "country": country,
        "genre": genre,
        "director": director,
        "cast": [],
        "stars": stars,
        "duration": duration,
        "poster_url": poster_url,
        "description": description,
        "athinorama_url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Κύρια συνάρτηση scraping
# ---------------------------------------------------------------------------

def _has_greek(text: str) -> bool:
    """Ελέγχει αν ένα κείμενο περιέχει ελληνικούς χαρακτήρες."""
    return bool(re.search(r"[Ͱ-Ͽἀ-῿]", text))


def _yt_search(query: str) -> list[tuple[str, str]]:
    """
    Αναζήτηση YouTube. Επιστρέφει λίστα (video_id, title) χωρίς duplicates.
    """
    import urllib.parse
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("YouTube search αποτυχία για '%s': %s", query, e)
        return []

    html = resp.text
    # Εξαγωγή video IDs και τίτλων από ytInitialData JSON
    ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    raw_titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"', html)

    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for i, vid in enumerate(ids):
        if vid not in seen:
            seen.add(vid)
            title = raw_titles[i] if i < len(raw_titles) else ""
            results.append((vid, title))
    return results


def find_youtube_trailer(title: str, original_title: str = "", year: int | None = None) -> str | None:
    """
    Ψάχνει YouTube για trailer της ταινίας.
    Προτιμά βίντεο με ελληνικό τίτλο ή ελληνικούς υπότιτλους.
    Επιστρέφει το πρώτο κατάλληλο video ID ή None.
    """
    year_str = str(year) if year else ""

    # Φάση 1: αναζήτηση με ελληνικούς όρους — επιστρέφουμε μόνο αν βρούμε ελληνικό τίτλο
    greek_queries = [
        f"{title} τρέιλερ {year_str}".strip(),
        f"{title} trailer ελληνικοί υπότιτλοι {year_str}".strip(),
        f"{title} trailer greek subtitles {year_str}".strip(),
    ]
    for q in greek_queries:
        for vid_id, vid_title in _yt_search(q)[:8]:
            if _has_greek(vid_title):
                logger.info("Ελληνικό trailer για '%s': %s (%s)", title, vid_id, vid_title)
                return vid_id

    # Φάση 2: οποιοδήποτε αποτέλεσμα από αναζήτηση με ελληνικό τίτλο
    results = _yt_search(f"{title} trailer {year_str}".strip())
    if results:
        logger.info("Trailer για '%s': %s", title, results[0][0])
        return results[0][0]

    # Fallback: πρωτότυπος τίτλος
    if original_title and original_title != title:
        results = _yt_search(f"{original_title} trailer {year_str}".strip())
        if results:
            return results[0][0]

    return None


def find_imdb_url(title: str, original_title: str = "", year: int | None = None) -> str | None:
    """
    Αναζητά το IMDb URL για μια ταινία μέσω του IMDb suggestion API.
    Επιστρέφει το URL (π.χ. https://www.imdb.com/title/tt1234567/) ή None.
    """
    import urllib.parse

    query = original_title or title
    if year:
        query += f" {year}"

    encoded = urllib.parse.quote(query)
    first_char = urllib.parse.quote(query[0].lower()) if query else "a"
    url = f"https://v2.sg.media-imdb.com/suggestion/h/{first_char}/{encoded}.json"

    try:
        resp = SESSION.get(url, timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("d", [])

        for item in results[:5]:
            item_id = item.get("id", "")
            if not item_id.startswith("tt"):
                continue
            item_year = item.get("y")
            # Αποδεκτό εύρος: ±1 χρόνος
            if year and item_year and abs(int(item_year) - int(year)) > 1:
                continue
            imdb_url = f"https://www.imdb.com/title/{item_id}/"
            logger.info("Βρέθηκε IMDb για '%s': %s", title, imdb_url)
            return imdb_url
    except Exception as e:
        logger.warning("IMDb search αποτυχία για '%s': %s", query, e)

    return None


def find_tmdb_data(title: str, original_title: str = "", year: int | None = None) -> dict | None:
    """
    Εμπλουτισμός ταινίας μέσω TMDB API.
    Επιστρέφει dict με genre, director, cast, description, imdb_score, imdb_url ή None.
    """
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY δεν έχει οριστεί")
        return None

    def _search(query: str) -> list:
        params = {"api_key": TMDB_API_KEY, "query": query, "language": "el"}
        if year:
            params["year"] = year
        try:
            r = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=10)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            logger.warning("TMDB search αποτυχία για '%s': %s", query, e)
            return []

    results = _search(original_title or title)
    if not results and original_title:
        results = _search(title)
    if not results:
        logger.info("TMDB: δεν βρέθηκε αποτέλεσμα για '%s'", title)
        return None

    # Βρες το καλύτερο match βάσει έτους
    best = results[0]
    if year:
        for r in results:
            rel_year = int((r.get("release_date") or "0-01-01")[:4] or "0")
            if abs(rel_year - int(year)) <= 1:
                best = r
                break

    tmdb_id = best["id"]

    try:
        r = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "append_to_response": "credits", "language": "el"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("TMDB details αποτυχία για id=%s: %s", tmdb_id, e)
        return None

    genres    = [g["name"] for g in data.get("genres", [])]
    credits   = data.get("credits", {})
    directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
    cast      = [c["name"] for c in credits.get("cast", [])[:10]]
    overview  = data.get("overview", "")
    vote_avg  = data.get("vote_average")
    imdb_id   = data.get("imdb_id")
    imdb_url  = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None

    logger.info("TMDB εμπλουτισμός για '%s' (tmdb_id=%s)", title, tmdb_id)
    return {
        "tmdb_id":    tmdb_id,
        "genre":      genres,
        "director":   directors,
        "cast":       cast,
        "description": overview or None,
        "imdb_score": round(float(vote_avg), 1) if vote_avg else None,
        "imdb_url":   imdb_url,
        "imdb_id":    imdb_id,
    }


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


def run_test_scrape(scrape_id: str, limit: int = 25) -> None:
    """
    Test scraping: σβήνει ΟΛΟΚΛΗΡΗ τη βάση movies και φέρνει ακριβώς limit ταινίες.
    Καλείται σε background thread.
    """
    logger.info("Test scraping [%s]: καθαρισμός βάσης + %d ταινίες", scrape_id, limit)

    update_scrape_job(scrape_id, {"status": "running", "total": limit, "done": 0, "errors": 0})

    cleared = clear_movies_collection()
    logger.info("Διαγράφηκαν %d ταινίες από τη βάση", cleared)

    done = 0
    errors = 0
    urls_seen = set()

    try:
        for movie_url in discover_movie_urls("full"):
            if movie_url in urls_seen:
                continue
            urls_seen.add(movie_url)

            if done >= limit:
                break

            update_scrape_job(scrape_id, {
                "total": limit,
                "done": done,
                "errors": errors,
                "status": "running",
                "current_url": movie_url,
            })

            try:
                data = scrape_movie_details(movie_url)
                if data:
                    save_movie(data)
                    done += 1
                    logger.debug("✓ Test: %s", data.get("title"))
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                logger.error("✗ Test σφάλμα για %s: %s", movie_url, e)

    except Exception as e:
        logger.error("Κρίσιμο σφάλμα test scraping: %s", e)
        update_scrape_job(scrape_id, {
            "status": "error",
            "error_message": str(e),
            "total": limit,
            "done": done,
            "errors": errors,
        })
        return

    update_scrape_job(scrape_id, {
        "status": "completed",
        "total": limit,
        "done": done,
        "errors": errors,
    })
    logger.info("Test scraping [%s] ολοκληρώθηκε: %d ταινίες, %d σφάλματα", scrape_id, done, errors)
