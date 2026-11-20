"""Unified HTTP Server & REST API for BMS Watch.

Serves the Web UI dashboard, REST API endpoints, and Render health checks.
Built using standard library http.server for zero-dependency local execution.
"""
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bms_core import (
    POPULAR_FORMATS,
    POPULAR_REGIONS,
    ROOT_DIR,
    get_bms_movies,
    search_bms_movies,
    get_city_venues,
    get_movie_info,
    today_ist,
    get_bms_regions,
    sb,
    init_db,
    run_check_cycle,
)

STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("bms_watch.server")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ismyhkcsxrswqsfzfrxe.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", ""))
# Comma-separated Supabase user ids and/or emails who see and manage every watch.
ADMINS = {a.strip().lower() for a in os.getenv("ADMIN_UID", "").split(",") if a.strip()}
CRON_SECRET = os.getenv("CRON_SECRET", "")
_token_cache: dict[str, tuple[float, dict]] = {}


def verify_token(token: str) -> dict | None:
    """Supabase user {id, email} for a sign-in access token, or None. Asks Supabase, cached 5 minutes."""
    if not token:
        return None
    hit = _token_cache.get(token)
    if hit and hit[0] > time.time():
        return hit[1]
    req = urllib.request.Request(f"{SUPABASE_URL}/auth/v1/user", headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            u = json.load(r)
    except Exception:
        return None
    user = {"id": u.get("id") or "", "email": (u.get("email") or "").lower()}
    if not user["id"]:
        return None
    user["admin"] = bool(ADMINS & {user["id"].lower(), user["email"]})
    if len(_token_cache) > 500:
        _token_cache.clear()
    _token_cache[token] = (time.time() + 300, user)
    return user

class BMSWatchHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _json(self, status_code: int, data: dict | list, headers: dict = None):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _user(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        return verify_token(auth[7:].strip() if auth.lower().startswith("bearer ") else "")

    def _owned_watch(self, user: dict, wid: str) -> dict | None:
        """The watch if this user may change it (theirs, or any for an admin), else None."""
        rows = sb("GET", f"watches?id=eq.{wid}&select=id,is_active,user_id")
        return rows[0] if rows and (user["admin"] or rows[0]["user_id"] == user["id"]) else None

    def _handle_watch_update(self, wid: str, payload: dict):
        user = self._user()
        if not user:
            return self._json(401, {"error": "Sign in first"})
        w = self._owned_watch(user, wid)
        if not w:
            return self._json(404, {"error": "No such watch"})

        updates = {}
        if "dates" in payload:
            dates = payload["dates"]
            if not dates or not isinstance(dates, list):
                return self._json(400, {"error": "At least one date (YYYYMMDD) is required"})
            if not all(isinstance(d, str) and re.fullmatch(r"\d{8}", d) for d in dates):
                return self._json(400, {"error": "Dates must be YYYYMMDD strings"})
            if min(dates) < today_ist():
                return self._json(400, {"error": "Dates can't be in the past"})
            updates["dates"] = sorted(dates)

        if "cinemas" in payload and isinstance(payload["cinemas"], list):
            updates["cinemas"] = payload["cinemas"]
        if "formats" in payload and isinstance(payload["formats"], list):
            updates["formats"] = payload["formats"]
        if "prime_seats_only" in payload:
            updates["prime_seats_only"] = bool(payload["prime_seats_only"])
        if "min_seats_together" in payload:
            updates["min_seats_together"] = max(1, int(payload["min_seats_together"] or 1))
        if "is_active" in payload:
            updates["is_active"] = bool(payload["is_active"])
        if "movie_title" in payload and payload["movie_title"]:
            updates["movie_title"] = payload["movie_title"]
        if "language" in payload and payload["language"]:
            updates["language"] = payload["language"].strip().lower()

        if not updates:
            return self._json(400, {"error": "No valid fields provided to update"})

        res = sb("PATCH", f"watches?id=eq.{wid}", updates, prefer="return=representation")
        return self._json(200, {"status": "updated", "watch": res[0] if res else {}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        # Healthcheck (Render requirement)
        if path == "/api/health":
            return self._json(200, {"status": "ok", "app": "bms-watch"})

        # Supabase Auth Config (shared project, dedicated bms_watch schema)
        if path == "/api/config":
            return self._json(200, {
                "supabase_url": os.getenv("SUPABASE_URL", "https://ismyhkcsxrswqsfzfrxe.supabase.co"),
                "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "")),
                "turnstile_site_key": os.getenv("TURNSTILE_SITE_KEY", os.getenv("NEXT_PUBLIC_TURNSTILE_SITE_KEY", "1x00000000000000000000AA")),
                "schema": "bms_watch"
            })

        # HTTP Cron endpoint (for external free crons or keep-alive pings)
        # Scheduled check (GitHub Action / external cron). Needs X-Cron-Secret = CRON_SECRET.
        if path == "/api/cron":
            given = self.headers.get("X-Cron-Secret", "")
            if not CRON_SECRET or not hmac.compare_digest(given.encode(), CRON_SECRET.encode()):
                return self._json(403, {"error": "Bad or missing X-Cron-Secret"})
            res = run_check_cycle()
            return self._json(200, res)

        # Dynamic BMS Regions
        if path == "/api/regions":
            return self._json(200, get_bms_regions())

        # Dynamic BMS Live Movies (Running or Upcoming)
        if path == "/api/bms/movies":
            qs = urllib.parse.parse_qs(url.query)
            region = qs.get("region", ["COIM"])[0].upper()
            slug = qs.get("slug", [""])[0].lower()
            mtype = qs.get("type", ["movies"])[0].lower()
            movies = get_bms_movies(region, slug, mtype)
            return self._json(200, movies, headers={"Cache-Control": "public, max-age=60"})

        # Any film on BookMyShow by name, including ones not yet in "upcoming"
        if path == "/api/bms/search":
            qs = urllib.parse.parse_qs(url.query)
            q = qs.get("q", [""])[0].strip()[:80]
            if len(q) < 2:
                return self._json(200, [])
            try:
                movies = search_bms_movies(q, qs.get("region", ["COIM"])[0], qs.get("slug", [""])[0].lower())
            except Exception:
                log.exception("search failed for %r", q)
                return self._json(502, {"error": "Couldn't reach BookMyShow"}, headers={"Cache-Control": "no-store"})
            # Only cache real results, so a retry of the same words always asks BookMyShow again.
            return self._json(200, movies, headers={"Cache-Control": "public, max-age=300" if movies else "no-store"})

        if path == "/api/formats":
            return self._json(200, POPULAR_FORMATS)

        # Dynamic BMS Venues / Cinemas in a city
        if path in ("/api/venues", "/api/bms/venues"):
            qs = urllib.parse.parse_qs(url.query)
            region = qs.get("region", ["COIM"])[0].upper()
            slug = qs.get("slug", ["coimbatore"])[0].lower()
            return self._json(200, get_city_venues(region, slug))

        # Dates + cinemas for the watch form
        if path == "/api/bms/movie-info":
            qs = urllib.parse.parse_qs(url.query)
            event = qs.get("event", [""])[0].upper()
            if not re.fullmatch(r"ET\d+", event):
                return self._json(400, {"error": "unknown film"})
            region = qs.get("region", ["COIM"])[0].upper()
            slug = qs.get("slug", ["coimbatore"])[0].lower()
            movie_slug = qs.get("movie_slug", [""])[0].lower()
            language = qs.get("language", [""])[0].lower()
            try:
                info = get_movie_info(event, region, slug, movie_slug, language=language)
                return self._json(200, info)
            except Exception:
                log.exception("movie-info failed for %s", event)
                today = today_ist()
                city_venues = [v for v in get_city_venues(region, slug) if v]
                return self._json(200, {
                    "today": today,
                    "release_date": None,
                    "earliest_date": today,
                    "bookable_dates": [],
                    "movie_venues": [],
                    "venues": city_venues,
                    "venues_source": "city",
                    "fallback": True,
                })

        # Watches: the signed-in user's own, or everyone's for an admin (ADMIN_UID)
        if path == "/api/watches":
            user = self._user()
            if not user:
                return self._json(401, {"error": "Sign in first"})

            query = "watches?select=*&order=created_at.desc"
            if not user["admin"]:
                query += f"&user_id=eq.{urllib.parse.quote(user['id'])}"
            watches = sb("GET", query)
            return self._json(200, watches, headers={"X-Admin": "1"} if user["admin"] else None)

        # Single watch lookup
        match_watch = re.match(r"^/api/watches/([0-9a-fA-F-]{36})$", path)
        if match_watch:
            user = self._user()
            if not user:
                return self._json(401, {"error": "Sign in first"})
            wid = match_watch.group(1).lower()
            rows = sb("GET", f"watches?id=eq.{wid}&select=*")
            if not rows or (not user["admin"] and rows[0]["user_id"] != user["id"]):
                return self._json(404, {"error": "No such watch"})
            return self._json(200, rows[0])

        # Serve login / sign-in page
        if path in ("/login", "/login.html", "/sign-in", "/sign-in.html"):
            login_path = STATIC_DIR / "login.html"
            if login_path.exists():
                content = login_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                return self.wfile.write(content)

        # Serve static UI (index.html fallback for root or missing paths)
        if path in ("/", "/index.html"):
            index_path = STATIC_DIR / "index.html"
            if index_path.exists():
                content = index_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                return self.wfile.write(content)

        return super().do_GET()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        # Manual Check Trigger
        if path == "/api/check-now":
            if not self._user():
                return self._json(401, {"error": "Sign in first"})
            result = run_check_cycle()
            return self._json(200, result)

        user = self._user() if path.startswith("/api/watches") else None
        if path.startswith("/api/watches") and not user:
            return self._json(401, {"error": "Sign in first"})

        # Create Watch
        if path == "/api/watches":
            title = payload.get("movie_title") or "Custom Movie"
            event_code = payload.get("event_code", "").strip()
            slug = payload.get("movie_slug") or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            region_code = payload.get("region_code", "COIM").upper()
            region_slug = payload.get("region_slug") or next((r["slug"] for r in POPULAR_REGIONS if r["code"] == region_code), "coimbatore")
            dates = payload.get("dates") or []
            cinemas = payload.get("cinemas") or []
            formats = payload.get("formats") or []
            language = (payload.get("language") or "english").strip().lower()
            prime_seats_only = bool(payload.get("prime_seats_only", False))
            min_seats_together = int(payload.get("min_seats_together", 1) or 1)
            email, user_id = user["email"], user["id"]   # from the verified token, never the body

            if not event_code:
                return self._json(400, {"error": "Pick a film first"})
            if not email:
                return self._json(400, {"error": "email is required"})
            if not dates:
                return self._json(400, {"error": "At least one date (YYYYMMDD) is required"})
            if not all(isinstance(d, str) and re.fullmatch(r"\d{8}", d) for d in dates):
                return self._json(400, {"error": "Dates must be YYYYMMDD strings"})
            if min(dates) < today_ist():
                return self._json(400, {"error": "Dates can't be in the past"})
            if not re.fullmatch(r"ET\d+", event_code.upper()):
                return self._json(400, {"error": "event_code looks like ET00526306"})
            event_code = event_code.upper()

            row = sb("POST", "watches", {
                "user_id": user_id, "movie_title": title, "event_code": event_code, "movie_slug": slug,
                "region_code": region_code, "region_slug": region_slug, "language": language,
                "dates": dates, "cinemas": cinemas, "formats": formats, "email": email, "is_active": True,
                "prime_seats_only": prime_seats_only, "min_seats_together": min_seats_together,
            }, prefer="return=representation")
            return self._json(201, {"status": "created", "watch_id": row[0]["id"]})

        # Toggle Active / Paused
        match_toggle = re.match(r"^/api/watches/([0-9a-fA-F-]{36})/toggle$", path)
        if match_toggle:
            wid = match_toggle.group(1).lower()
            w = self._owned_watch(user, wid)
            if not w:
                return self._json(404, {"error": "No such watch"})
            sb("PATCH", f"watches?id=eq.{wid}", {"is_active": not w["is_active"]})
            return self._json(200, {"status": "ok", "watch_id": wid})

        # Update Watch via POST
        match_update = re.match(r"^/api/watches/([0-9a-fA-F-]{36})$", path)
        if match_update:
            return self._handle_watch_update(match_update.group(1).lower(), payload)

        return self._json(404, {"error": "Route not found"})

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        m = re.match(r"^/api/watches/([0-9a-fA-F-]{36})$", path)
        if m:
            return self._handle_watch_update(m.group(1).lower(), payload)

        return self._json(404, {"error": "Route not found"})

    def do_PATCH(self):
        return self.do_PUT()

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")
        match_del = re.match(r"^/api/watches/([0-9a-fA-F-]{36})$", path)
        if match_del:
            wid = match_del.group(1).lower()
            user = self._user()
            if not user or not self._owned_watch(user, wid):
                return self._json(404 if user else 401, {"error": "No such watch" if user else "Sign in first"})
            sb("DELETE", f"watches?id=eq.{wid}")
            return self._json(200, {"status": "deleted", "watch_id": wid})

        return self._json(404, {"error": "Route not found"})


def run_server(port: int = 8000):
    init_db()
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, BMSWatchHandler)
    log.info("BMS Watch Server running at http://localhost:%d (serving %s)", port, STATIC_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopping...")
        httpd.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.getenv("PORT", "8000"))
    run_server(port)
