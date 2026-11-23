"""Core BMS Watch engine: Scraping, Supabase storage, and Email Alerts.

Watches, check state and alert logs live in the shared Supabase project (schema bms_watch),
reached through its REST API with the server key, so the Render site and a local scheduled
check (python run.py --cli) share one database. SQLite only caches BookMyShow film lists.

Runs with ZERO external dependencies (standard library only) for 100% local portability.
"""
import datetime
import html
import json
import logging
import os
import re
import smtplib
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path

# Base paths
ROOT_DIR = Path(__file__).resolve().parent

# Load local .env if present
_env_path = ROOT_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "movies_cache.db"   # film-list cache only; watches live in Supabase
LOG_PATH = DATA_DIR / "bms_watch.log"

# Default configuration
DEFAULT_SMTP_USER = os.getenv("SMTP_USER", "")
DEFAULT_SMTP_PASS = os.getenv("SMTP_PASS", "")
DEFAULT_MAIL_TO = [e.strip() for e in os.getenv("MAIL_TO", DEFAULT_SMTP_USER).split(",") if e.strip()]
DEFAULT_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
DEFAULT_SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

# HTTP Mail Webhook (Google Apps Script / transactional mail API)
# Solves Render Free tier blocking outbound SMTP ports 25, 465, and 587
DEFAULT_MAIL_WEBHOOK_URL = os.getenv(
    "MAIL_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbwiskciP3cX5znCumHIBwF8AdIH6POWsm_gibBxLRWCCCJ-CnM8SdKcxbgF363Ot_Ry/exec"
)
DEFAULT_MAIL_WEBHOOK_TOKEN = os.getenv("MAIL_WEBHOOK_TOKEN", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ismyhkcsxrswqsfzfrxe.supabase.co").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")


def sb(method: str, path: str, body=None, prefer: str = ""):
    """One Supabase REST (PostgREST) call on the bms_watch schema, e.g. sb("GET", "watches?is_active=eq.true").
    Uses the server key, which bypasses row-level security: callers do their own ownership checks."""
    if not SUPABASE_SECRET_KEY:
        raise RuntimeError("SUPABASE_SECRET_KEY is not set")
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Accept-Profile": "bms_watch",
        "Content-Profile": "bms_watch",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {method} {path.split('?')[0]} failed ({e.code}): {e.read()[:300]!r}") from None
    return json.loads(raw) if raw else None

BMS_SHOWTIMES_API = "https://in.bookmyshow.com/api/movies-data/v5/showtimes-by-event/primary-dynamic"
BMS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "x-app-code": "WEB", "x-platform": "DESKTOP", "x-platform-code": "DESKTOP-WEB",
    "Referer": "https://in.bookmyshow.com/", "Accept": "application/json",
}

PILL = {
    "green": ("AVAILABLE", "#1ea83c"),
    "orange": ("FAST FILLING", "#e8a900"),
    "red": ("ALMOST FULL", "#eb4e62"),
    "grey": ("SOLD OUT / UNAVAILABLE", "#999999")
}

RANK = {"SOLD OUT / UNAVAILABLE": 0, "ALMOST FULL": 1, "FAST FILLING": 2, "AVAILABLE": 3}

POPULAR_REGIONS = [
    {"code": "COIM", "slug": "coimbatore", "name": "Coimbatore"},
    {"code": "BANG", "slug": "bengaluru", "name": "Bengaluru"},
    {"code": "CHEN", "slug": "chennai", "name": "Chennai"},
    {"code": "MUMBAI", "slug": "mumbai", "name": "Mumbai"},
    {"code": "NCR", "slug": "national-capital-region-ncr", "name": "Delhi-NCR"},
    {"code": "HYD", "slug": "hyderabad", "name": "Hyderabad"},
    {"code": "KOCH", "slug": "kochi", "name": "Kochi"},
    {"code": "PUNE", "slug": "pune", "name": "Pune"},
    {"code": "KOLK", "slug": "kolkata", "name": "Kolkata"},
    {"code": "AHD", "slug": "ahmedabad", "name": "Ahmedabad"},
    {"code": "TRIV", "slug": "trivandrum", "name": "Trivandrum"},
    {"code": "CHAN", "slug": "chandigarh", "name": "Chandigarh"},
]

POPULAR_FORMATS = ["IMAX 3D", "IMAX 2D", "EPIQ", "PXL", "ICE", "4DX", "ScreenX", "DOLBY ATMOS", "3D", "2D"]

# Common names BMS's own alias data leaves out (it already has bombay, madras, cochin, mysore...)
EXTRA_CITY_ALIASES = {"BANG": ["bangalore"], "KOLK": ["calcutta"], "PUNE": ["poona"], "COIM": ["kovai"]}

REGION_LANGUAGES = {
    "COIM": ["tamil", "english", "hindi"],
    "CHEN": ["tamil", "english", "hindi"],
    "BANG": ["kannada", "english", "hindi", "tamil", "telugu"],
    "MUMBAI": ["hindi", "marathi", "english"],
    "NCR": ["hindi", "english", "punjabi"],
    "HYD": ["telugu", "english", "hindi"],
    "KOCH": ["malayalam", "english", "tamil", "hindi"],
    "TRIV": ["malayalam", "english", "tamil", "hindi"],
    "PUNE": ["marathi", "hindi", "english"],
    "KOLK": ["bengali", "hindi", "english"],
    "AHD": ["gujarati", "hindi", "english"],
    "CHAN": ["hindi", "punjabi", "english"],
}

STATE_LANGUAGES = {
    "kerala": ["malayalam", "english", "tamil", "hindi"],
    "tamil nadu": ["tamil", "english", "hindi"],
    "karnataka": ["kannada", "english", "hindi", "tamil", "telugu"],
    "andhra": ["telugu", "english", "hindi"],
    "telangana": ["telugu", "english", "hindi"],
    "maharashtra": ["marathi", "hindi", "english"],
    "west bengal": ["bengali", "hindi", "english"],
    "gujarat": ["gujarati", "hindi", "english"],
    "punjab": ["punjabi", "hindi", "english"],
}

def get_region_languages(region_code: str) -> list[str]:
    code = (region_code or "").upper()
    if code in REGION_LANGUAGES:
        return REGION_LANGUAGES[code]
    for r in get_bms_regions():
        if r.get("code") == code:
            state = (r.get("state") or "").lower()
            for st, langs in STATE_LANGUAGES.items():
                if st in state:
                    return langs
            break
    return ["malayalam", "tamil", "telugu", "hindi", "english", "kannada"]

log = logging.getLogger("bms_watch.core")

_CACHE_REGIONS = None
_CACHE_MOVIES = {}


def get_bms_regions() -> list[dict]:
    global _CACHE_REGIONS
    if _CACHE_REGIONS:
        return _CACHE_REGIONS

    # 1. Pre-packaged full static catalog (2,000+ cities including Koppam, Perinthalmanna)
    static_file = ROOT_DIR / "static" / "regions.json"
    if static_file.exists():
        try:
            loaded = json.loads(static_file.read_text(encoding="utf-8"))
            if loaded and isinstance(loaded, list):
                _CACHE_REGIONS = loaded
                return _CACHE_REGIONS
        except Exception:
            pass

    # 2. Live fetch from BookMyShow if static file is unavailable
    try:
        req = urllib.request.Request("https://in.bookmyshow.com/api/explore/v1/discover/regions", headers=BMS_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.load(r)
        bms = d.get("BookMyShow", {})
        cities = []
        seen = set()
        for group in ("TopCities", "OtherCities"):
            for c in bms.get(group, []):
                code = c.get("RegionCode")
                if not code or code in seen:
                    continue
                seen.add(code)
                try:
                    lat, lon = float(c["Lat"]), float(c["Long"])
                except (KeyError, TypeError, ValueError):
                    lat = lon = None
                cities.append({
                    "code": code,
                    "name": c.get("RegionName"),
                    "slug": c.get("RegionSlug"),
                    "lat": lat,
                    "lon": lon,
                    "top": group == "TopCities",
                    "aliases": (c.get("Alias") or []) + EXTRA_CITY_ALIASES.get(code, []),
                    "state": c.get("StateName") or "",
                })
        if cities:
            _CACHE_REGIONS = cities
            return cities
    except Exception:
        log.exception("Failed fetching live BMS regions, using fallback list")

    return POPULAR_REGIONS


def resolve_region_slug(region_code: str, fallback_slug: str = "") -> str:
    if fallback_slug and fallback_slug.strip():
        return fallback_slug.strip().lower()
    code = (region_code or "").upper()
    for r in POPULAR_REGIONS:
        if r["code"] == code:
            return r["slug"]
    all_regions = get_bms_regions()
    if all_regions:
        for r in all_regions:
            if r.get("code") == code and r.get("slug"):
                return r["slug"]
    return code.lower()


# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """SQLite holds only the BookMyShow film-list cache; losing it is harmless."""
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bms_movies_cache (
            region_code TEXT NOT NULL,
            explore_type TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(region_code, explore_type)
        )""")


def get_cached_movies_from_db(region_code: str, explore_type: str) -> tuple[float, list[dict]] | None:
    try:
        with get_db() as conn:
            cur = conn.execute(
                "SELECT data, updated_at FROM bms_movies_cache WHERE region_code = ? AND explore_type = ?",
                (region_code.upper(), explore_type.lower()),
            )
            row = cur.fetchone()
            if row:
                data = json.loads(row["data"])
                if isinstance(data, list) and data:
                    return float(row["updated_at"]), data
    except Exception:
        pass
    return None


def save_cached_movies_to_db(region_code: str, explore_type: str, movies: list[dict], updated_at: float):
    if not movies:
        return
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bms_movies_cache (region_code, explore_type, data, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (region_code.upper(), explore_type.lower(), json.dumps(movies), updated_at),
            )
            conn.commit()
    except Exception:
        log.exception("Failed saving movies cache to DB for %s (%s)", region_code, explore_type)


def fetch_live_bms_movies(region_code: str, region_slug: str, explore_type: str) -> list[dict]:
    import uuid
    exp = "upcoming-movies" if explore_type in ("upcoming", "upcoming-movies") else "movies"
    params = urllib.parse.urlencode({"region": region_code})

    # Primary URL includes region_slug, secondary fallback endpoint uses region query only
    url_candidates = [
        f"https://in.bookmyshow.com/api/explore/v1/discover/{exp}-{urllib.parse.quote(region_slug)}?{params}",
        f"https://in.bookmyshow.com/api/explore/v1/discover/{exp}?{params}",
    ]

    for attempt, url in enumerate(url_candidates):
        for retry in range(2):
            try:
                headers = dict(BMS_HEADERS)
                headers["x-region-code"] = region_code
                headers["x-region-slug"] = region_slug
                headers["x-bms-id"] = str(uuid.uuid4())

                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as r:
                    d = json.load(r)

                movies, seen = [], set()

                def walk(o):
                    if isinstance(o, list):
                        for x in o:
                            walk(x)
                    elif isinstance(o, dict):
                        cta_url = str(o.get("ctaUrl", ""))
                        analytics = o.get("analytics") or {}
                        event_code = analytics.get("event_code") or ""
                        if not event_code:
                            m_code = re.search(r"\b(ET\d+)\b", cta_url)
                            if m_code:
                                event_code = m_code.group(1)

                        if event_code and event_code not in seen:
                            seen.add(event_code)
                            m_slug = re.search(r"/movies/(?:[^/]+/)?([^/?#]+)/(?:ET\d+)", cta_url)
                            texts = [c.get("text", "") for t in o.get("text", []) for c in t.get("components", []) if c.get("type") == "text"]
                            card_lang = analytics.get("language", "").strip().lower()
                            m_langs = []
                            if card_lang:
                                m_langs.append(card_lang)
                            if len(texts) > 1:
                                sub_langs = re.findall(
                                    r"(tamil|telugu|hindi|malayalam|kannada|english|bengali|marathi|gujarati|punjabi|japanese)",
                                    texts[1],
                                    re.IGNORECASE,
                                )
                                for sl in sub_langs:
                                    sl = sl.lower()
                                    if sl not in m_langs:
                                        m_langs.append(sl)

                            raw_name = analytics.get("title") or (texts[0] if texts else (o.get("image") or {}).get("altText", ""))
                            slug = m_slug.group(1) if m_slug else re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-")

                            movies.append({
                                "name": raw_name,
                                "subtitle": " · ".join(t.strip() for t in texts[1:] if t.strip()),
                                "image": (o.get("image") or {}).get("url", ""),
                                "url": cta_url or f"https://in.bookmyshow.com/movies/{region_slug}/{slug}/{event_code}",
                                "event_code": event_code,
                                "slug": slug,
                                "type": explore_type,
                                "language": m_langs[0] if m_langs else "",
                                "languages": m_langs,
                            })
                            return
                        for v in o.values():
                            walk(v)

                walk(d.get("listings", []))
                if movies:
                    return movies
            except Exception as e:
                log.warning("Attempt %d (retry %d) failed fetching BMS movies for %s (%s) from %s: %s", attempt + 1, retry + 1, region_code, explore_type, url, e)
                time.sleep(0.4)

    return []


def search_bms_movies(query: str, region_code: str = "COIM", region_slug: str = "") -> list[dict]:
    """Films matching `query` from BookMyShow's own search box, including ones not yet listed as upcoming."""
    import uuid
    region_code = (region_code or "COIM").upper()
    region_slug = resolve_region_slug(region_code, region_slug)
    url = "https://in.bookmyshow.com/api/v1/search/dynamic?" + urllib.parse.urlencode({"q": query, "instant": "true", "firstLoad": "false"})
    # BookMyShow sometimes errors or sends a reply without results; retry those. A real
    # no-match says totalHits: 0 and is returned as [] straight away.
    for attempt in range(3):
        try:
            headers = dict(BMS_HEADERS, **{"x-region-code": region_code, "x-region-slug": region_slug, "x-bms-id": str(uuid.uuid4())})
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as r:
                d = json.load(r)
            if (d.get("metadata") or {}).get("totalHits") == 0:
                return []
            cards = (((d.get("data") or {}).get("widgets") or {}).get("search-vi-v2-results") or {}).get("data", {}).get("cards", {}).get("data", [])
            if cards:
                break
            err = RuntimeError("search reply had no results")
        except Exception as e:
            err = e
        log.warning("BMS search %r attempt %d failed: %s", query, attempt + 1, err)
        time.sleep(0.5 * (attempt + 1))
    else:
        raise err
    movies = []
    for c in cards:
        x = c.get("data") or {}
        code, cta_url = x.get("identifier") or "", (x.get("cta") or {}).get("url", "")
        if (x.get("analytics") or {}).get("type") != "movies" or not re.fullmatch(r"ET\d+", code):
            continue
        m_slug = re.search(r"/movies/(?:[^/]+/)?([^/?#]+)/ET\d+", cta_url)
        langs = re.findall(r"[A-Za-z]+", re.sub(r"&\s*\d+\s*more", "", x.get("result-language") or ""))
        movies.append({
            "name": x.get("result-title", ""),
            "subtitle": " · ".join(s for s in ((x.get("result-context") or "").strip(" |"), x.get("result-language") or "") if s),
            "image": x.get("result-poster-url") or x.get("result-icon", ""),
            "url": cta_url,
            "event_code": code,
            "slug": m_slug.group(1) if m_slug else "",
            "type": "search",
            "language": langs[0].lower() if langs else "",
            "languages": [l.lower() for l in langs],
        })
    return movies


def get_bms_movies(region_code: str = "COIM", region_slug: str = "", explore_type: str = "movies") -> list[dict]:
    region_code = (region_code or "COIM").upper()
    region_slug = resolve_region_slug(region_code, region_slug)
    canonical_type = "upcoming" if explore_type in ("upcoming", "upcoming-movies") else "movies"
    cache_key = (region_code, canonical_type)
    now = time.time()

    # 1. Fresh in-memory cache check (TTL: 600 seconds / 10 minutes)
    if cache_key in _CACHE_MOVIES:
        ts, data = _CACHE_MOVIES[cache_key]
        if now - ts < 600 and data:
            return data

    # 2. Fresh SQLite DB cache check (TTL: 600 seconds / 10 minutes)
    db_cached = get_cached_movies_from_db(region_code, canonical_type)
    if db_cached:
        ts, data = db_cached
        if now - ts < 600 and data:
            _CACHE_MOVIES[cache_key] = (ts, data)
            return data

    # 3. Live fetch from BMS with retries and alternate endpoints
    movies = fetch_live_bms_movies(region_code, region_slug, canonical_type)
    if movies:
        _CACHE_MOVIES[cache_key] = (now, movies)
        save_cached_movies_to_db(region_code, canonical_type, movies, now)
        return movies

    # 4. Fallback to stale in-memory cache if live fetch failed
    if cache_key in _CACHE_MOVIES:
        ts, data = _CACHE_MOVIES[cache_key]
        if data:
            log.warning("Serving %d stale in-memory movies for %s (%s) (age: %.1fs)", len(data), region_code, canonical_type, now - ts)
            return data

    # 5. Fallback to stale SQLite cache if available
    if db_cached:
        ts, data = db_cached
        if data:
            log.warning("Serving %d stale DB movies for %s (%s) (age: %.1fs)", len(data), region_code, canonical_type, now - ts)
            _CACHE_MOVIES[cache_key] = (ts, data)
            return data

    log.error("Could not obtain movies for %s (%s) from live BMS or cache", region_code, canonical_type)
    return []


# ---------------------------------------------------------------------------
# BMS API Client
# ---------------------------------------------------------------------------

def fetch_bms_showtimes(event_code: str, date: str, region_code: str, region_slug: str, language: str = "english") -> dict:
    q = urllib.parse.urlencode({
        "etCodes": "*", "dateCode": date, "isDesktop": "true", "regionCode": region_code,
        "xLocationShared": "false", "memberId": "", "lsId": "", "subCode": "",
        "appCode": "WEB", "language": language, "refEventCode": event_code,
    })
    headers = dict(BMS_HEADERS)
    headers["x-region-code"] = region_code
    headers["x-region-slug"] = region_slug

    req = urllib.request.Request(f"{BMS_SHOWTIMES_API}?{q}", headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))  # BMS is India-only


def today_ist() -> str:
    return datetime.datetime.now(IST).strftime("%Y%m%d")


def response_date(resp: dict) -> str:
    """BMS answers a not-yet-bookable date with the FIRST bookable date's shows; this is the date it actually returned."""
    return str(resp.get("data", {}).get("additionalData", {}).get("dateCode", ""))


def bookable_dates(resp: dict) -> list[str]:
    found = set()

    def walk(o):
        if isinstance(o, dict):
            cta = o.get("cta")
            if isinstance(cta, dict) and cta.get("type") == "dateSelector":
                code = str(cta.get("additionalData", {}).get("dateCode", ""))
                if len(code) == 8 and code.isdigit():
                    found.add(code)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(resp.get("data", {}))
    return sorted(found)


_CACHE_RELEASE = {}
_CACHE_VENUES = {}


def get_release_date(event_code: str, region_slug: str, movie_slug: str) -> str | None:
    """Release date (YYYYMMDD) scraped from the public movie page; None if unknown."""
    if event_code in _CACHE_RELEASE:
        return _CACHE_RELEASE[event_code]
    url = f"https://in.bookmyshow.com/movies/{urllib.parse.quote(region_slug)}/{urllib.parse.quote(movie_slug or 'movie')}/{urllib.parse.quote(event_code)}"
    try:
        req = urllib.request.Request(url, headers={**BMS_HEADERS, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=12) as r:
            page = r.read().decode("utf-8", "ignore")
    except Exception:
        log.warning("Could not read release date for %s", event_code)
        return None
    m = re.search(r'"releaseDate":"(\d{4})-(\d{2})-(\d{2})T', page)
    _CACHE_RELEASE[event_code] = "".join(m.groups()) if m else None
    return _CACHE_RELEASE[event_code]


def get_city_venues(region_code: str, region_slug: str = "") -> list[str]:
    """Fetch all movie theatres in the given region from BMS mobile venues API.
    Falls back to collecting venues from top running films' showtimes if API fails."""
    cached = _CACHE_VENUES.get(region_code)
    if cached and time.time() - cached[0] < 6 * 3600:
        return cached[1]

    # Primary: Official BMS mobile venues endpoint
    try:
        params = urllib.parse.urlencode({
            "token": "67x1xa33b4x422b361ba",
            "regionCode": region_code,
            "eventType": "MT",
        })
        url = f"https://in.bookmyshow.com/api/v2/mobile/venues?{params}"
        headers = {**BMS_HEADERS, "x-app-code": "WEB", "Accept": "application/json"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
            venues_raw = data.get("venues") or []
            names = set()
            for v in venues_raw:
                name = v.get("VenueName") or v.get("venueName") or v.get("name")
                if name:
                    names.add(name.strip())
            if names:
                result = sorted(names)
                _CACHE_VENUES[region_code] = (time.time(), result)
                return result
    except Exception:
        log.exception("Failed fetching venues from mobile API for %s", region_code)

    # Fallback: scrape from top running movies
    try:
        movies = [m for m in get_bms_movies(region_code, region_slug, "movies") if m.get("event_code")]

        def venues_for(m):
            try:
                lang = m.get("language") or (get_region_languages(region_code)[0] if get_region_languages(region_code) else "english")
                return set(parse_bms_showtimes(fetch_bms_showtimes(m["event_code"], today_ist(), region_code, region_slug, language=lang)))
            except Exception:
                return set()

        with ThreadPoolExecutor(max_workers=6) as pool:
            venues = sorted(set().union(*pool.map(venues_for, movies[:10])))
        if venues:
            _CACHE_VENUES[region_code] = (time.time(), venues)
            return venues
    except Exception:
        log.exception("Fallback venue scrape failed for %s", region_code)

    return []


def get_movie_info(event_code: str, region_code: str, region_slug: str, movie_slug: str, language: str = "") -> dict:
    """What the watch form needs: earliest watchable date, already-bookable dates, and cinemas."""
    today = today_ist()
    raw = {}
    dates = []
    movie_venues = []

    # Build ordered list of candidate languages to test
    candidates = []
    if language and language.strip():
        candidates.append(language.strip().lower())
    for r_lang in get_region_languages(region_code):
        if r_lang not in candidates:
            candidates.append(r_lang)
    for default_lang in ["english", "hindi", "tamil", "malayalam"]:
        if default_lang not in candidates:
            candidates.append(default_lang)

    chosen_lang = candidates[0]
    for lang in candidates:
        try:
            r = fetch_bms_showtimes(event_code, today, region_code, region_slug, language=lang)
            d = [x for x in bookable_dates(r) if x >= today]
            if d:
                raw = r
                chosen_lang = lang
                dates = d
                break
            if not raw:
                raw = r
                chosen_lang = lang
        except Exception:
            continue

    if dates:
        parsed = parse_bms_showtimes(raw)
        movie_venues = sorted(v for v in parsed if isinstance(v, str) and v.strip())
        # If today has 0 venues (e.g. shows start tomorrow or next week), fetch the earliest bookable date
        if not movie_venues and dates[0] != today:
            try:
                r_first = fetch_bms_showtimes(event_code, dates[0], region_code, region_slug, language=chosen_lang)
                parsed_first = parse_bms_showtimes(r_first)
                movie_venues = sorted(v for v in parsed_first if isinstance(v, str) and v.strip())
            except Exception:
                pass

    city_venues = []
    try:
        city_venues = [v for v in get_city_venues(region_code, region_slug) if isinstance(v, str) and v.strip()]
    except Exception:
        log.warning("Could not fetch city venues for %s", region_code)

    all_venues = sorted(set(city_venues) | set(movie_venues))

    release = None
    if not dates and movie_slug:
        try:
            release = get_release_date(event_code, region_slug, movie_slug)
        except Exception:
            pass

    earliest = max(today, dates[0] if dates else (release or today))
    return {
        "today": today,
        "release_date": release,
        "earliest_date": earliest,
        "bookable_dates": dates,
        "movie_venues": movie_venues,
        "venues": all_venues or city_venues or movie_venues,
        "venues_source": "movie" if movie_venues else "city",
        "language": chosen_lang,
    }


def parse_bms_showtimes(resp: dict) -> dict:
    """Parses BMS showtimes response into:
    {
      "Venue Name": {
         "show_key": {
            "time": "07:00 AM",
            "format": "EPIQ",
            "pill": "AVAILABLE",
            "avail_status": 1,
            "status_label": "AVAILABLE (availStatus=1)",
            "venue_code": "BWCB",
            "session_id": "56348",
            "categories": [{"type": "ELITE", "status": "AVAILABLE", "price": "500"}]
         }
      }
    }
    """
    venues = {}

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            if o.get("type") == "venue-card":
                v_data = o.get("additionalData", {})
                v_name = str(v_data.get("venueName") or o.get("id") or "").strip()
                if not v_name:
                    return
                v_code = v_data.get("venueCode", "")
                shows = {}

                for sec in o.get("showtimesSections", []):
                    fmt = " ".join(c.get("text", "") for t in sec.get("text", []) for c in t.get("components", [])).strip()
                    for s in sec.get("showtimes", []):
                        ad = s.get("additionalData", {})
                        style = s.get("styleId", "")
                        pill_label = next((v[0] for k, v in PILL.items() if style.startswith(k)), style or "?")
                        avail_status = int(ad.get("availStatus", 0) or 0)
                        time_str = ad.get("showTime", s.get("title", ""))
                        screen_attr = s.get("screenAttr", "")
                        session_id = ad.get("sessionId", "")

                        # Category & price parsing from bottomsheet
                        cats = []
                        custom_cta = s.get("customGestureCTA", {}).get("additionalData", {}).get("bottomSheetData", {})
                        for w in custom_cta.get("widgets", []):
                            vdata = w.get("variableData", {})
                            if "seatType" in vdata:
                                cats.append({
                                    "type": vdata.get("seatType", ""),
                                    "status": vdata.get("seatAvalibility", ""),
                                    "price": vdata.get("seatCost", "")
                                })

                        key = f"{time_str} {screen_attr} [{fmt}]".strip().replace("  ", " ")
                        shows[key] = {
                            "time": time_str,
                            "screen_attr": screen_attr,
                            "format_str": fmt,
                            "pill": pill_label,
                            "avail_status": avail_status,
                            "status_label": f"{pill_label} (availStatus={avail_status})",
                            "venue_code": v_code,
                            "session_id": session_id,
                            "categories": cats,
                        }
                if shows:
                    venues[v_name] = shows
            else:
                for v in o.values():
                    walk(v)

    walk(resp.get("data", {}).get("showtimeWidgets", []))
    return venues


# ---------------------------------------------------------------------------
# Diff & Matching Engine
# ---------------------------------------------------------------------------

def calculate_diff(old_venues: dict, new_venues: dict) -> list[tuple]:
    """-> [(kind, cinema, show_key, old_status_label, new_status_label, new_show_obj)]"""
    out = []
    for v in sorted(set(old_venues) | set(new_venues)):
        if v not in old_venues:
            out.append(("new_cinema", v, "", "", "", None))
            for k, s in new_venues[v].items():
                out.append(("new_show", v, k, "", s["status_label"], s))
        elif v not in new_venues:
            out.append(("cinema_gone", v, "", "", "", None))
        else:
            o, n = old_venues[v], new_venues[v]
            for k in sorted(set(o) | set(n)):
                if k not in o:
                    out.append(("new_show", v, k, "", n[k]["status_label"], n[k]))
                elif k not in n:
                    out.append(("show_gone", v, k, o[k]["status_label"], "", None))
                elif o[k]["status_label"] != n[k]["status_label"]:
                    out.append(("changed", v, k, o[k]["status_label"], n[k]["status_label"], n[k]))
    return out


def is_status_increase(old_label: str, new_label: str, kind: str) -> bool:
    if kind in ("new_cinema", "new_show"):
        return True
    if kind == "changed":
        old_rank = RANK.get(old_label.split(" (")[0], -1)
        new_rank = RANK.get(new_label.split(" (")[0], -1)
        return new_rank > old_rank or ("availStatus=0" in old_label and "availStatus=1" in new_label)
    return False


PRIME_CATEGORY_KEYWORDS = {
    "prime", "executive", "club", "royal", "premium", "recliner",
    "vip", "gold", "diamond", "platinum", "balcony", "sofa", "elite", "lounge"
}


def is_prime_category(cat_name: str) -> bool:
    name = (cat_name or "").lower()
    return any(kw in name for kw in PRIME_CATEGORY_KEYWORDS)


def check_seats_headless(venue_code: str, session_id: str, min_together: int = 1) -> dict:
    """Attempts to inspect exact seat layout via Playwright headless Chrome.
    Adheres strictly to security and compliance guidelines: if BookMyShow presents a
    Cloudflare Turnstile or anti-bot challenge ('Attention Required! | Cloudflare'),
    it does NOT attempt to bypass or solve it, and cleanly returns {'ok': False, 'blocked_by_cloudflare': True}.
    """
    if not venue_code or not session_id:
        return {"ok": False, "reason": "missing_session"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "reason": "playwright_not_installed"}

    url = f"https://in.bookmyshow.com/booktickets/{venue_code}/{session_id}"
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    exec_path = chrome_path if os.path.exists(chrome_path) else (edge_path if os.path.exists(edge_path) else None)

    try:
        with sync_playwright() as p:
            launch_args = {"headless": True}
            if exec_path:
                launch_args["executable_path"] = exec_path
            browser = p.chromium.launch(**launch_args)
            try:
                page = browser.new_page()
                page.set_default_timeout(12000)
                resp = page.goto(url, wait_until="domcontentloaded")
                title = page.title()

                # Stop immediately if Cloudflare challenge is encountered
                if "attention required" in title.lower() or "cloudflare" in title.lower() or (resp and resp.status == 403):
                    log.info("BookMyShow presented Cloudflare challenge for %s. Bypassing is prohibited; using API category verification.", url)
                    return {"ok": False, "blocked_by_cloudflare": True}

                result = page.evaluate("""() => {
                    const seats = Array.from(document.querySelectorAll('a._available, ._available, [data-seat-status="0"]'));
                    return seats.map(s => s.innerText.trim() || s.getAttribute('data-seat-id') || '');
                }""")
                clean_seats = [s for s in result if s]
                return {"ok": True, "seats": clean_seats, "count": len(clean_seats)}
            finally:
                browser.close()
    except Exception as e:
        log.warning("Headless seat lookup failed for %s/%s: %s", venue_code, session_id, e)
        return {"ok": False, "error": str(e)}


def evaluate_prime_seats(show_obj: dict, old_show_obj: dict | None = None, min_together: int = 1) -> dict:
    """Evaluates whether prime/center seating is available and calculates seat deltas.
    Attempts headless browser inspection if accessible, falling back to official BMS category API metadata.
    """
    venue_code = show_obj.get("venue_code", "")
    session_id = show_obj.get("session_id", "")

    cats = show_obj.get("categories") or []
    prime_cats = [c for c in cats if is_prime_category(c.get("type", ""))]
    avail_prime_cats = [c for c in prime_cats if "avail" in c.get("status", "").lower()]

    old_cats = (old_show_obj or {}).get("categories") or []
    old_prime_cats = [c for c in old_cats if is_prime_category(c.get("type", ""))]
    old_avail_cats = [c for c in old_prime_cats if "avail" in c.get("status", "").lower()]

    # Attempt headless layout inspection
    headless_res = check_seats_headless(venue_code, session_id, min_together)

    if headless_res.get("ok"):
        seats = headless_res.get("seats", [])
        old_seats = (old_show_obj or {}).get("prime_seat_ids") or []
        newly_freed = set(seats) - set(old_seats)
        delta = len(newly_freed) if old_seats else len(seats)
        show_obj["prime_seat_ids"] = seats

        has_prime = len(seats) >= min_together
        if has_prime:
            seat_summary = f"{len(seats)} Prime Seat(s) available"
            if delta > 0 and len(old_seats) > 0:
                seat_summary += f" (+{delta} newly freed)"
            if seats[:6]:
                seat_summary += f": {', '.join(seats[:6])}{'...' if len(seats) > 6 else ''}"
        else:
            seat_summary = "Prime center seats unavailable"

        return {
            "is_prime_available": has_prime,
            "available_seats_count": len(seats),
            "more_seats_count": delta if has_prime else 0,
            "seat_labels": seats,
            "summary": seat_summary,
            "method": "headless_layout",
        }

    # Fallback to verified BMS category metadata
    has_prime = (len(avail_prime_cats) > 0) if cats else (show_obj.get("avail_status", 0) >= 2)
    delta_cats = len(avail_prime_cats) - len(old_avail_cats)

    cat_names = [f"{c['type']} ({c.get('price', '')})" for c in avail_prime_cats]
    if has_prime and cat_names:
        summary = f"Prime seats available: {', '.join(cat_names)}"
        if delta_cats > 0 and len(old_avail_cats) > 0:
            summary += f" (+{delta_cats} tier(s) freed up)"
    elif has_prime:
        summary = "Prime seating available"
    else:
        summary = "Prime seats unavailable (budget/front rows only)"

    return {
        "is_prime_available": has_prime,
        "available_seats_count": len(avail_prime_cats),
        "more_seats_count": max(0, delta_cats) if has_prime else 0,
        "seat_labels": [],
        "prime_categories": cat_names,
        "summary": summary,
        "method": "api_categories",
    }


def matches_watch_filters(diff_item: tuple, watch: dict) -> bool:
    """Checks if a detected diff matches the user's specific cinema and format criteria."""
    kind, cinema, show_key, old_label, new_label, show_obj = diff_item
    if not is_status_increase(old_label, new_label, kind):
        return False

    # Filter cinemas
    target_cinemas = watch.get("cinemas") or []
    if target_cinemas and not any(c.lower() in cinema.lower() for c in target_cinemas):
        return False

    # Filter formats
    target_formats = watch.get("formats") or []
    if target_formats and show_key:
        if not any(f.lower() in show_key.lower() for f in target_formats):
            return False

    # Filter prime seating if enabled
    if watch.get("prime_seats_only"):
        if not show_obj:
            return False
        prime_info = show_obj.get("prime_info")
        if not prime_info or not prime_info.get("is_prime_available"):
            return False

    return True


# ---------------------------------------------------------------------------
# Email Notification Generator
# ---------------------------------------------------------------------------

def generate_email_html(watch: dict, changes_by_date: dict) -> str:
    region_slug = watch.get("region_slug", "coimbatore")
    movie_slug = watch.get("movie_slug", "movie")
    event_code = watch.get("event_code", "")
    movie_title = watch.get("movie_title", "Movie")

    cell = "padding:10px 12px;border-bottom:1px solid #eeeeee;font-size:14px;vertical-align:top"
    badge_colors = {
        "new_cinema": ("NEW CINEMA", "#1ea83c"),
        "new_show": ("NEW SHOW", "#2f80ed"),
        "changed": ("SEATS OPENED", "#e8a900"),
    }

    date_sections = []
    for date, changes in changes_by_date.items():
        book_url = f"https://in.bookmyshow.com/movies/{region_slug}/{movie_slug}/buytickets/{event_code}/{date}"
        try:
            nice_date = time.strftime("%a %d %b %Y", time.strptime(date, "%Y%m%d"))
        except Exception:
            nice_date = date

        rows = []
        for kind, cinema, show_key, old_label, new_label, show_obj in changes:
            badge_label, badge_color = badge_colors.get(kind, ("UPDATE", "#666666"))
            status_text = f"<b style='color:#1ea83c'>{html.escape(new_label.split(' (')[0])}</b>"

            # Direct session checkout link if available
            direct_link = ""
            if show_obj and show_obj.get("venue_code") and show_obj.get("session_id"):
                session_url = f"https://in.bookmyshow.com/booktickets/{show_obj['venue_code']}/{show_obj['session_id']}"
                direct_link = f"<br><a href='{session_url}' style='color:#eb4e62;font-size:12px;font-weight:700'>Direct Seat Selection &rsaquo;</a>"

            prime_badge = ""
            if show_obj and show_obj.get("prime_info"):
                p_info = show_obj["prime_info"]
                p_text = html.escape(p_info.get("summary", ""))
                if p_info.get("is_prime_available"):
                    prime_badge = f"<div style='margin-top:4px;font-size:12px;color:#92400e;background:#fef3c7;padding:3px 8px;border-radius:4px;display:inline-block'>👑 <b>{p_text}</b></div>"
                else:
                    prime_badge = f"<div style='margin-top:4px;font-size:12px;color:#64748b;background:#f1f5f9;padding:3px 8px;border-radius:4px;display:inline-block'>👑 <i>{p_text}</i></div>"

            rows.append(f"""
            <tr>
              <td style="{cell};width:100px">
                <span style="background:{badge_color};color:#ffffff;padding:3px 7px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap">
                  {badge_label}
                </span>
              </td>
              <td style="{cell}">
                <b>{html.escape(cinema)}</b>
                {f"<div style='color:#555555;font-size:13px;margin-top:2px'>{html.escape(show_key)}</div>" if show_key else ""}
                {prime_badge}
                {direct_link}
              </td>
              <td style="{cell};text-align:right">
                {status_text}
              </td>
            </tr>
            """)

        date_sections.append(f"""
        <div style="margin-top:20px;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
          <div style="background:#f8fafc;padding:10px 14px;border-bottom:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:700;font-size:15px;color:#1e293b">{nice_date}</span>
            <a href="{book_url}" style="font-size:13px;color:#eb4e62;text-decoration:none;font-weight:600">View on BMS &rsaquo;</a>
          </div>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse">
            {"".join(rows)}
          </table>
        </div>
        """)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:auto;color:#1e293b">
      <div style="background:#eb4e62;color:#ffffff;padding:20px;border-radius:8px 8px 0 0">
        <div style="font-size:13px;text-transform:uppercase;letter-spacing:1px;opacity:0.9">BMS Watch Alert</div>
        <div style="font-size:22px;font-weight:800;margin-top:4px">{html.escape(movie_title)}</div>
        <div style="font-size:14px;opacity:0.9;margin-top:2px">{region_slug.title()} &middot; Advance Bookings Update</div>
      </div>
      <div style="padding:16px 20px 24px;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px;background:#ffffff">
        <p style="margin:4px 0 16px;font-size:14px;color:#475569">
          New booking availability or screens were detected for your watched movie on BookMyShow:
        </p>
        {"".join(date_sections)}
        <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f1f5f9;font-size:12px;color:#94a3b8">
          Sent by BMS Watch at {datetime.datetime.now().strftime("%d %b %Y %H:%M:%S")}.
        </div>
      </div>
    </div>
    """


def send_email_alert(watch: dict, changes_by_date: dict, smtp_user: str = "", smtp_pass: str = "", smtp_host: str = "", smtp_port: int = 0):
    user = smtp_user or DEFAULT_SMTP_USER
    recipient = watch.get("email") or (DEFAULT_MAIL_TO[0] if DEFAULT_MAIL_TO else "")

    if not recipient:
        log.warning("Email not sent: missing recipient for watch %s", watch.get("id"))
        return False

    movie_title = watch.get("movie_title", "Movie")
    subject = f"🎟️ BMS Watch: Tickets Available for {movie_title}!"
    html_body = generate_email_html(watch, changes_by_date)
    text_body = f"BookMyShow Update for {movie_title}!\n\nTickets/screens opened for your watch.\nCheck BookMyShow now."

    # Priority 1: HTTP Mail Webhook (Google Apps Script / Mail API)
    # Allows sending over HTTPS (port 443), avoiding Render Free tier SMTP firewall blocks
    webhook_url = os.getenv("MAIL_WEBHOOK_URL", DEFAULT_MAIL_WEBHOOK_URL)
    webhook_token = os.getenv("MAIL_WEBHOOK_TOKEN", DEFAULT_MAIL_WEBHOOK_TOKEN)

    if webhook_url:
        payload = {
            "token": webhook_token,
            "to": recipient,
            "subject": subject,
            "htmlBody": html_body,
            "textBody": text_body,
        }
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                res_body = r.read().decode("utf-8")
                try:
                    res_json = json.loads(res_body)
                except Exception:
                    res_json = {}
                if res_json.get("status") == "ok" or res_json.get("success"):
                    log.info("Email alert successfully sent via Webhook to %s for watch %s", recipient, watch.get("id"))
                    return True
                elif res_json.get("error"):
                    log.error("Webhook rejected email to %s: %s", recipient, res_json.get("error"))
                    # If webhook rejected unauthorized or other error, fallback to SMTP if configured
                    if not (smtp_pass or DEFAULT_SMTP_PASS):
                        return False
                else:
                    log.info("Webhook replied %d: %s", r.status, res_body[:200])
                    return True
        except Exception as e:
            log.warning("Webhook email delivery failed (%s), attempting SMTP fallback: %s", type(e).__name__, e)

    # Priority 2: SMTP SSL Fallback (for local CLI runs or paid instances)
    pwd = smtp_pass or DEFAULT_SMTP_PASS
    host = smtp_host or DEFAULT_SMTP_HOST
    port = smtp_port or DEFAULT_SMTP_PORT

    if not user or not pwd:
        log.warning("Email not sent: missing SMTP credentials (user=%s, recipient=%s)", user, recipient)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL(host, port, timeout=20) as s:
        s.login(user, pwd)
        s.send_message(msg)

    log.info("Email alert successfully sent via SMTP to %s for watch %s", recipient, watch.get("id"))
    return True


# ---------------------------------------------------------------------------
# Poller & Execution Cycle
# ---------------------------------------------------------------------------

_CHECK_LOCK = threading.Lock()


def run_check_cycle() -> dict:
    """Executes a single checking cycle across all active watches.
    Returns: summary dict with change count and logs.
    Overlapping calls in this process (scheduled check + manual "look now") return {"status": "busy"}
    instead of running twice and sending duplicate emails.
    ponytail: the lock is per process, so a local scheduled check and Look now on Render running in
    the same few seconds can both email one change; add a lock row in Supabase if that ever happens.
    """
    if not _CHECK_LOCK.acquire(blocking=False):
        return {"status": "busy", "watches_checked": 0, "total_changes": 0}
    try:
        return _run_check_cycle()
    finally:
        _CHECK_LOCK.release()


def _run_check_cycle() -> dict:
    watches = sb("GET", "watches?is_active=eq.true&select=*")
    if not watches:
        log.info("No active watches to check.")
        return {"status": "ok", "watches_checked": 0, "total_changes": 0}

    # Deduplicate checks by (event_code, region_code, date)
    today = today_ist()
    needed_queries = {
        (w["event_code"], w["region_code"], w["region_slug"], w.get("language") or "english", d)
        for w in watches for d in (w.get("dates") or []) if d >= today
    }
    log.info("Checking %d unique showtime query(s) for %d active watch(es)...", len(needed_queries), len(watches))

    # Fetch and parse
    new_snapshots = {}
    for event_code, region_code, region_slug, lang, date in needed_queries:
        key = f"{event_code}:{region_code}:{date}"
        try:
            raw = fetch_bms_showtimes(event_code, date, region_code, region_slug, lang)
            # Not bookable yet -> BMS returns another date's shows. Treat as "no shows yet".
            new_snapshots[key] = parse_bms_showtimes(raw) if response_date(raw) == date else {}
        except Exception:
            log.exception("Failed fetching showtimes for %s", key)

    # Previous snapshots for every key in one request
    old_snapshots = {}
    if new_snapshots:
        keys = ",".join(f'"{k}"' for k in new_snapshots)
        old_snapshots = {r["key"]: r["data"] for r in sb("GET", f"state_cache?select=key,data&key=in.({urllib.parse.quote(keys)})")}

    total_changes = 0
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for w in watches:
        watch_changes_by_date = {}
        is_initial_check = w.get("last_alert_at") is None
        for d in w.get("dates") or []:
            key = f"{w['event_code']}:{w['region_code']}:{d}"
            if key not in new_snapshots:
                continue
            # For new watches that haven't received an alert yet, diff against {} so they
            # get alerted if shows are ALREADY bookable. Subsequent runs diff against old_snapshots.
            base_snapshot = {} if is_initial_check else (old_snapshots.get(key) or {})
            diffs = calculate_diff(base_snapshot, new_snapshots[key])
            matched = []
            for diff in diffs:
                kind, cinema, show_key, old_label, new_label, show_obj = diff
                # Always evaluate prime seat info when availability increased
                if show_obj and is_status_increase(old_label, new_label, kind):
                    if "prime_info" not in show_obj:
                        old_show = base_snapshot.get(cinema, {}).get(show_key)
                        show_obj["prime_info"] = evaluate_prime_seats(
                            show_obj, old_show, min_together=w.get("min_seats_together", 1)
                        )
                if matches_watch_filters(diff, w):
                    matched.append(diff)
            if matched:
                watch_changes_by_date[d] = matched
                total_changes += len(matched)

        update = {"last_checked_at": now_str}
        if watch_changes_by_date:
            n = sum(map(len, watch_changes_by_date.values()))
            prime_details = ""
            for changes_list in watch_changes_by_date.values():
                for c in changes_list:
                    p_info = (c[5] or {}).get("prime_info")
                    if p_info and p_info.get("summary"):
                        prime_details = f" ({p_info['summary']})"
                        break
                if prime_details:
                    break

            log.info("Watch %s (%s): %d matching change(s) found!%s", w["id"], w["movie_title"], n, prime_details)
            try:
                if send_email_alert(w, watch_changes_by_date):
                    sb("POST", "alert_logs", {"watch_id": w["id"], "user_id": w["user_id"], "title": f"Alert for {w['movie_title']}",
                                              "details": f"{n} change(s) detected{prime_details}", "sent_to": w.get("email", ""), "created_at": now_str})
                    update["last_alert_at"] = now_str
            except Exception:
                log.exception("Failed sending email alert for watch %s", w["id"])
        sb("PATCH", f"watches?id=eq.{w['id']}", update)

    # Save the new snapshots (upsert on key)
    if new_snapshots:
        sb("POST", "state_cache?on_conflict=key", [{"key": k, "data": v, "updated_at": now_str} for k, v in new_snapshots.items()],
           prefer="resolution=merge-duplicates")

    return {"status": "ok", "watches_checked": len(watches), "total_changes": total_changes}
