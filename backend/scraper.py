"""
Scraper για τις κριτικές ταινιών του Athinorama.
Rate limiting: 1-2 δευτερόλεπτα delay μεταξύ requests.
"""
import concurrent.futures
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
    get_existing_movie_ids,
    update_scrape_job,
    clear_movies_collection,
    save_url_list_cache,
    load_url_list_cache,
)

logger = logging.getLogger(__name__)

# Pause/stop control: {scrape_id: 'running'|'paused'|'stopped'}
_scrape_controls: dict = {}


def set_scrape_control(scrape_id: str, control: str) -> None:
    _scrape_controls[scrape_id] = control


def get_scrape_control(scrape_id: str) -> str:
    return _scrape_controls.get(scrape_id, "running")


def _clear_scrape_control(scrape_id: str) -> None:
    _scrape_controls.pop(scrape_id, None)


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

# Μέγιστος χρόνος επεξεργασίας ανά ταινία (seconds) - αν κολλήσει, την προσπερνά
MOVIE_SCRAPE_TIMEOUT = 60

# Delay μεταξύ TMDB API κλήσεων για αποφυγή rate limiting (429)
TMDB_REQUEST_DELAY = 0.2


# ---------------------------------------------------------------------------
# Βοηθητικές
# ---------------------------------------------------------------------------

def _sleep():
    """Rate limiting: 1-2 δευτερόλεπτα pause."""
    time.sleep(random.uniform(1.0, 2.0))


def _tmdb_get(url: str, params: dict) -> requests.Response | None:
    """TMDB GET με rate-limit handling (429 backoff) και retry."""
    for attempt in range(3):
        try:
            time.sleep(TMDB_REQUEST_DELAY)
            r = SESSION.get(url, params=params, timeout=10)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 10))
                logger.warning("TMDB rate limit (429) — αναμονή %ds", retry_after)
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            logger.warning("TMDB request αποτυχία (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


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
    # Άρθρα
    "Ο", "Η", "Το", "Οι", "Τα", "Τους", "Τις",
    # Αόριστα άρθρα / αριθμητικά
    "Ένας", "Ένα", "Μια", "Μία",
    "Δύο", "Τρεις", "Τρία", "Τέσσερις", "Τέσσερα", "Πέντε", "Έξι",
    "Επτά", "Εφτά", "Οκτώ", "Εννιά", "Εννέα", "Δέκα", "Είκοσι",
    "Τριάντα", "Σαράντα", "Πενήντα", "Εκατό", "Χίλια",
    # Προθέσεις / σύνδεσμοι
    "Στην", "Στον", "Στο", "Στα", "Στους", "Στις",
    "Με", "Χωρίς", "Από", "Για", "Προς", "Ως", "Σε", "Κατά",
    "Όταν", "Αν", "Αλλά", "Ενώ", "Καθώς", "Μετά", "Πριν",
    "Παρά", "Ώσπου", "Μόλις", "Αφού",
    # Αντωνυμίες
    "Αυτός", "Αυτή", "Αυτό", "Αυτοί", "Αυτές",
    "Κάποιος", "Κάποια", "Κάποιο", "Κανείς", "Κανένας", "Κανένα",
    "Μερικοί", "Μερικές", "Μερικά", "Όλοι", "Όλες", "Όλα",
    "Τίποτα", "Τίποτε",
    # Επίθετα / μετοχές που ξεκινούν συχνά περιγραφές
    "Βασισμένο", "Βασισμένη", "Βασισμένος",
    "Εμπνευσμένο", "Εμπνευσμένη", "Εμπνευσμένος",
    "Εμπνευσμένα",
    "Νέος", "Νέα", "Νέο", "Νέοι",
    "Μικρός", "Μικρή", "Μικρό",
    "Μεγάλος", "Μεγάλη", "Μεγάλο",
    "Τελευταία", "Τελευταίος", "Τελευταίο",
    "Πρώτος", "Πρώτη", "Πρώτο",
    # Ρήματα / μετοχές
    "Είναι", "Ήταν", "Έχει", "Είχε",
    "Έχοντας", "Ζώντας", "Παίζοντας", "Ψάχνοντας",
    # Επιρρήματα
    "Έτσι", "Τότε", "Εκεί", "Εδώ", "Πάντα", "Ποτέ",
    "Ξαφνικά", "Σύντομα", "Τελικά", "Ακόμα", "Ακόμη",
    "Μόνο", "Μόνος", "Μαζί", "Ήδη", "Κιόλας",
    "Πολύ", "Λίγο", "Αρκετά", "Σχεδόν",
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
    for i, word in enumerate(words[:3]):
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
    # Το element μπορεί να περιέχει "Ελληνικός / Original" — κρατάμε μόνο το τελευταίο τμήμα
    title_original = ""
    orig_el = soup.select_one(".original-title")
    if orig_el:
        span = orig_el.select_one("span")
        raw = span.get_text(strip=True) if span else orig_el.get_text(strip=True)
        title_original = raw.split(" / ")[-1].strip()

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
        r = _tmdb_get(f"{TMDB_BASE}/search/movie", params)
        if r is None:
            logger.warning("TMDB search αποτυχία για '%s'", query)
            return []
        return r.json().get("results", [])

    results = _search(original_title or title)
    if not results and original_title:
        results = _search(title)
    if not results:
        logger.info("TMDB: δεν βρέθηκε αποτέλεσμα για '%s'", title)
        return None

    # Βρες το καλύτερο match βάσει ομοιότητας τίτλου και έτους
    def _title_similarity(a: str, b: str) -> float:
        a, b = a.lower().strip(), b.lower().strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
        matches = sum(c in longer for c in shorter)
        return matches / len(longer)

    search_title = (original_title or title).lower().strip()

    def _score(r: dict) -> tuple:
        rel_year = int((r.get("release_date") or "0-01-01")[:4] or "0")
        year_diff = abs(rel_year - int(year)) if year and rel_year else 999
        title_score = max(
            _title_similarity(search_title, r.get("title", "")),
            _title_similarity(search_title, r.get("original_title", "")),
        )
        return (year_diff, -title_score)

    results.sort(key=_score)
    best = results[0]

    # Απόρριψη αν το match είναι πολύ κακό (έτος > 2 χρόνια διαφορά ΚΑΙ τίτλος ανόμοιος)
    if year or search_title:
        rel_year = int((best.get("release_date") or "0-01-01")[:4] or "0")
        year_diff = abs(rel_year - int(year)) if year and rel_year else 0
        title_score = max(
            _title_similarity(search_title, best.get("title", "")),
            _title_similarity(search_title, best.get("original_title", "")),
        )
        if year_diff > 2 and title_score < 0.4:
            logger.warning(
                "TMDB: δεν βρέθηκε αξιόπιστο match για '%s' (%s) — "
                "καλύτερο αποτέλεσμα: '%s' (%s), year_diff=%d, title_score=%.2f",
                title, year, best.get("title"), rel_year, year_diff, title_score,
            )
            return None

    tmdb_id = best["id"]

    r = _tmdb_get(
        f"{TMDB_BASE}/movie/{tmdb_id}",
        {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos", "language": "el"},
    )
    if r is None:
        logger.warning("TMDB details αποτυχία για id=%s", tmdb_id)
        return None
    data = r.json()

    logger.info("TMDB εμπλουτισμός για '%s' (tmdb_id=%s)", title, tmdb_id)
    return _parse_tmdb_response(data, tmdb_id)


def _parse_tmdb_response(data: dict, tmdb_id: int) -> dict:
    """Εξάγει όλα τα πεδία από TMDB movie response dict."""
    genres    = [g["name"] for g in data.get("genres", [])]
    credits   = data.get("credits", {})
    directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
    cast_raw  = credits.get("cast", [])[:10]
    cast      = [c["name"] for c in cast_raw]
    cast_roles = [{"name": c["name"], "character": c.get("character", "")} for c in cast_raw]
    overview  = data.get("overview", "")
    vote_avg  = data.get("vote_average")
    vote_count = data.get("vote_count")
    imdb_id   = data.get("imdb_id")
    imdb_url  = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None
    tagline   = data.get("tagline") or None
    backdrop  = data.get("backdrop_path") or None
    orig_lang = data.get("original_language") or None
    prod_cos  = [c["name"] for c in data.get("production_companies", [])[:3]] or None

    tmdb_trailer_key = None
    videos = data.get("videos", {}).get("results", [])
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official"):
            tmdb_trailer_key = v["key"]
            break
    if not tmdb_trailer_key:
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                tmdb_trailer_key = v["key"]
                break

    release_year = int((data.get("release_date") or "0-01-01")[:4] or "0") or None

    return {
        "tmdb_id":              tmdb_id,
        "title":                data.get("title") or None,
        "original_title":       data.get("original_title") or None,
        "year":                 release_year,
        "genre":                genres,
        "director":             directors,
        "cast":                 cast,
        "cast_roles":           cast_roles,
        "description":          overview or None,
        "tmdb_score":           round(float(vote_avg), 1) if vote_avg else None,
        "vote_count":           vote_count or None,
        "imdb_url":             imdb_url,
        "imdb_id":              imdb_id,
        "tagline":              tagline,
        "backdrop_path":        backdrop,
        "original_language":    orig_lang,
        "production_companies": prod_cos,
        "tmdb_trailer_key":     tmdb_trailer_key or None,
    }


def tmdb_matches_movie(tmdb_data: dict, movie: dict) -> bool:
    """
    Ελέγχει αν τα TMDB δεδομένα ανήκουν στη σωστή ταινία συγκρίνοντας
    τίτλο και έτος. Επιστρέφει False αν το match είναι προφανώς λάθος.
    """
    def _similarity(a: str, b: str) -> float:
        a, b = a.lower().strip(), b.lower().strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
        return sum(c in longer for c in shorter) / len(longer)

    local_title = (movie.get("title") or "").lower().strip()
    local_original = (movie.get("title_original") or "").lower().strip()
    local_year = movie.get("year")

    tmdb_title = (tmdb_data.get("title") or "").lower().strip()
    tmdb_original = (tmdb_data.get("original_title") or "").lower().strip()
    tmdb_year = tmdb_data.get("year")

    title_score = max(
        _similarity(local_title, tmdb_title),
        _similarity(local_title, tmdb_original),
        _similarity(local_original, tmdb_title) if local_original else 0.0,
        _similarity(local_original, tmdb_original) if local_original else 0.0,
    )

    year_diff = abs(int(local_year) - int(tmdb_year)) if local_year and tmdb_year else 0

    if year_diff > 2 and title_score < 0.4:
        return False
    return True


def fetch_tmdb_data_by_id(tmdb_id: int) -> dict | None:
    """
    Φέρνει TMDB δεδομένα απευθείας με γνωστό tmdb_id (χωρίς search).
    Χρησιμοποιείται για re-enrich ταινιών που έχουν ήδη tmdb_id.
    """
    if not TMDB_API_KEY:
        return None
    r = _tmdb_get(
        f"{TMDB_BASE}/movie/{tmdb_id}",
        {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos", "language": "el"},
    )
    if r is None:
        logger.warning("TMDB fetch_by_id αποτυχία για id=%s", tmdb_id)
        return None
    return _parse_tmdb_response(r.json(), tmdb_id)


_TMDB_MERGE_FIELDS = (
    "genre", "director", "cast", "cast_roles", "description",
    "tmdb_score", "vote_count", "imdb_url", "imdb_id",
    "tagline", "backdrop_path", "original_language", "production_companies",
    "tmdb_trailer_key",
)


def _apply_tmdb_to_movie(movie_data: dict, tmdb_data: dict) -> None:
    """Εφαρμόζει TMDB δεδομένα στο movie dict (in-place). Δεν αντικαθιστά υπάρχοντα πεδία."""
    movie_data["tmdb_id"] = tmdb_data["tmdb_id"]
    for field in _TMDB_MERGE_FIELDS:
        if tmdb_data.get(field) and not movie_data.get(field):
            movie_data[field] = tmdb_data[field]
    # title_original δεν ενημερώνεται από TMDB — μόνο από Athinorama scraping
    if tmdb_data.get("year") and not movie_data.get("year"):
        movie_data["year"] = tmdb_data["year"]
    movie_data["tmdb_enriched_at"] = datetime.now(timezone.utc).isoformat()


def _scrape_and_enrich(movie_url: str) -> dict | None:
    """Scrape μιας ταινίας + TMDB enrich. Τρέχει μέσα σε thread με timeout."""
    data = scrape_movie_details(movie_url)
    if data and TMDB_API_KEY:
        try:
            tmdb_data = find_tmdb_data(
                title=data.get("title", ""),
                original_title=data.get("title_original", ""),
                year=data.get("year"),
            )
            if tmdb_data:
                _apply_tmdb_to_movie(data, tmdb_data)
        except Exception as te:
            logger.warning("TMDB αποτυχία για '%s': %s", data.get("title"), te)
    return data


def _format_duration(seconds: float) -> str:
    """Μετατρέπει seconds σε αναγνώσιμη μορφή."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ω {m}λ {s}δ"
    if m:
        return f"{m}λ {s}δ"
    return f"{s}δ"


def run_scrape(
    scrape_id: str,
    mode: str = "full",
    full_rescrape: bool = False,
    batch_size: int | None = None,
    offset: int = 0,
) -> None:
    """
    Εκτελεί scraping. Καλείται σε background thread.
    - mode: 'full' ή 'incremental'
    - full_rescrape: αν True, αντικαθιστά υπάρχοντα docs
    - batch_size: αν οριστεί, επεξεργάζεται μόνο τόσες ταινίες και σταματά
    - offset: παραλείπει τις πρώτες N ταινίες (για συνέχεια batch)
    """
    logger.info(
        "Έναρξη scraping [%s] mode=%s full_rescrape=%s batch_size=%s offset=%s",
        scrape_id, mode, full_rescrape, batch_size, offset,
    )

    set_scrape_control(scrape_id, "running")
    start_time = time.time()
    done = 0
    errors = 0
    total_found = 0
    processed_in_batch = 0

    try:
        # --- Φάση 1: Discovery (ή φόρτωση από Firestore cache) ---
        if mode == "full":
            all_urls = None

            if offset > 0:
                # Σε συνέχεια batch: φόρτωση από cache αντί για νέο discovery
                all_urls = load_url_list_cache()
                if all_urls:
                    logger.info("Φόρτωση %d URLs από Firestore cache (offset=%d)", len(all_urls), offset)
                else:
                    logger.warning("URL cache δεν βρέθηκε ή έχει λήξει — εκ νέου discovery")

            if all_urls is None:
                update_scrape_job(scrape_id, {"status": "discovering", "done": 0, "errors": 0})
                logger.info("Discovery ταινιών (full mode)...")
                all_urls = list(dict.fromkeys(discover_movie_urls("full")))
                save_url_list_cache(all_urls)
                logger.info("Discovery ολοκληρώθηκε: %d μοναδικά URLs αποθηκεύτηκαν στο Firestore", len(all_urls))
        else:
            # Incremental: γρήγορο discovery μόνο 2 τελευταίων μηνών
            all_urls = list(dict.fromkeys(discover_movie_urls("incremental")))
            logger.info("Incremental discovery: %d URLs", len(all_urls))

        total_found = len(all_urls)
        urls_to_process = all_urls[offset:]

        update_scrape_job(scrape_id, {
            "status": "running",
            "total": total_found,
            "done": done,
            "errors": errors,
            "offset": offset,
            "batch_size": batch_size,
        })

        # --- Φάση 2: Scraping ---
        # Batch-check ποιά IDs υπάρχουν ήδη, αντί για N ξεχωριστά Firestore reads
        if not full_rescrape:
            all_ids = [_extract_id_from_url(u) for u in urls_to_process]
            valid_ids = [id_ for id_ in all_ids if id_]
            existing_ids = get_existing_movie_ids(valid_ids)
            logger.info("Batch check: %d/%d ταινίες υπάρχουν ήδη", len(existing_ids), len(valid_ids))
        else:
            existing_ids = set()

        stopped_by_user = False
        # Ο executor δημιουργείται μία φορά εκτός loop — αποφεύγουμε το overhead
        # δημιουργίας/καταστροφής thread pool για κάθε ταινία
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
          for movie_url in urls_to_process:
            if batch_size and processed_in_batch >= batch_size:
                break

            # Έλεγχος pause/stop
            ctrl = get_scrape_control(scrape_id)
            if ctrl == "stopped":
                stopped_by_user = True
                break
            if ctrl == "paused":
                elapsed_paused = time.time() - start_time
                update_scrape_job(scrape_id, {
                    "status": "paused",
                    "total": total_found,
                    "done": done,
                    "errors": errors,
                    "offset": offset,
                    "batch_size": batch_size,
                    "duration_seconds": int(elapsed_paused),
                    "duration_formatted": _format_duration(elapsed_paused),
                })
                while get_scrape_control(scrape_id) == "paused":
                    time.sleep(2)
                ctrl = get_scrape_control(scrape_id)
                if ctrl == "stopped":
                    stopped_by_user = True
                    break
                update_scrape_job(scrape_id, {"status": "running"})

            update_scrape_job(scrape_id, {
                "total": total_found,
                "done": done,
                "errors": errors,
                "status": "running",
                "current_url": movie_url,
                "offset": offset,
                "batch_size": batch_size,
            })

            processed_in_batch += 1

            movie_id = _extract_id_from_url(movie_url)
            if movie_id and movie_id in existing_ids:
                done += 1
                continue

            try:
                future = executor.submit(_scrape_and_enrich, movie_url)
                try:
                    data = future.result(timeout=MOVIE_SCRAPE_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    errors += 1
                    logger.warning(
                        "⏱ Timeout (%ds) για %s - προσπέρασμα",
                        MOVIE_SCRAPE_TIMEOUT, movie_url,
                    )
                    continue
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
        elapsed = time.time() - start_time
        logger.error("Κρίσιμο σφάλμα scraping: %s", e)
        update_scrape_job(scrape_id, {
            "status": "error",
            "error_message": str(e),
            "total": total_found,
            "done": done,
            "errors": errors,
            "offset": offset,
            "batch_size": batch_size,
            "duration_seconds": int(elapsed),
            "duration_formatted": _format_duration(elapsed),
        })
        _clear_scrape_control(scrape_id)
        return

    elapsed = time.time() - start_time
    duration_fmt = _format_duration(elapsed)

    if stopped_by_user:
        final_status = "stopped"
        next_offset = offset + processed_in_batch
    else:
        # Αν τελείωσε λόγω batch limit → batch_completed, αλλιώς completed
        batch_hit_limit = batch_size and processed_in_batch >= batch_size
        final_status = "batch_completed" if batch_hit_limit else "completed"
        next_offset = (offset + batch_size) if batch_hit_limit else None

    update_scrape_job(scrape_id, {
        "status": final_status,
        "total": total_found,
        "done": done,
        "errors": errors,
        "offset": offset,
        "batch_size": batch_size,
        "next_offset": next_offset,
        "duration_seconds": int(elapsed),
        "duration_formatted": duration_fmt,
    })
    _clear_scrape_control(scrape_id)
    logger.info(
        "Ολοκλήρωση scraping [%s] status=%s: %d/%d ταινίες, %d σφάλματα, next_offset=%s, διάρκεια=%s",
        scrape_id, final_status, done, total_found, errors, next_offset, duration_fmt,
    )


def run_test_scrape(scrape_id: str, limit: int = 25) -> None:
    """
    Test scraping: σβήνει ΟΛΟΚΛΗΡΗ τη βάση movies και φέρνει ακριβώς limit ταινίες.
    Καλείται σε background thread.
    """
    logger.info("Test scraping [%s]: καθαρισμός βάσης + %d ταινίες", scrape_id, limit)

    set_scrape_control(scrape_id, "running")
    start_time = time.time()
    update_scrape_job(scrape_id, {"status": "running", "total": limit, "done": 0, "errors": 0})

    cleared = clear_movies_collection()
    logger.info("Διαγράφηκαν %d ταινίες από τη βάση", cleared)

    done = 0
    errors = 0
    urls_seen = set()
    stopped_by_user = False

    try:
        for movie_url in discover_movie_urls("full"):
            if movie_url in urls_seen:
                continue
            urls_seen.add(movie_url)

            if done >= limit:
                break

            # Έλεγχος pause/stop
            ctrl = get_scrape_control(scrape_id)
            if ctrl == "stopped":
                stopped_by_user = True
                break
            if ctrl == "paused":
                elapsed_paused = time.time() - start_time
                update_scrape_job(scrape_id, {
                    "status": "paused",
                    "total": limit,
                    "done": done,
                    "errors": errors,
                    "duration_seconds": int(elapsed_paused),
                    "duration_formatted": _format_duration(elapsed_paused),
                })
                while get_scrape_control(scrape_id) == "paused":
                    time.sleep(2)
                ctrl = get_scrape_control(scrape_id)
                if ctrl == "stopped":
                    stopped_by_user = True
                    break
                update_scrape_job(scrape_id, {"status": "running"})

            update_scrape_job(scrape_id, {
                "total": limit,
                "done": done,
                "errors": errors,
                "status": "running",
                "current_url": movie_url,
            })

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_scrape_and_enrich, movie_url)
                    try:
                        data = future.result(timeout=MOVIE_SCRAPE_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        errors += 1
                        logger.warning(
                            "⏱ Timeout (%ds) για %s - προσπέρασμα",
                            MOVIE_SCRAPE_TIMEOUT, movie_url,
                        )
                        continue
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
        elapsed = time.time() - start_time
        logger.error("Κρίσιμο σφάλμα test scraping: %s", e)
        update_scrape_job(scrape_id, {
            "status": "error",
            "error_message": str(e),
            "total": limit,
            "done": done,
            "errors": errors,
            "duration_seconds": int(elapsed),
            "duration_formatted": _format_duration(elapsed),
        })
        _clear_scrape_control(scrape_id)
        return

    elapsed = time.time() - start_time
    duration_fmt = _format_duration(elapsed)
    final_status = "stopped" if stopped_by_user else "completed"

    update_scrape_job(scrape_id, {
        "status": final_status,
        "total": limit,
        "done": done,
        "errors": errors,
        "duration_seconds": int(elapsed),
        "duration_formatted": duration_fmt,
    })
    _clear_scrape_control(scrape_id)
    logger.info(
        "Test scraping [%s] %s: %d ταινίες, %d σφάλματα, διάρκεια=%s",
        scrape_id, final_status, done, errors, duration_fmt,
    )
