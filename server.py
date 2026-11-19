"""Unified HTTP Server & REST API for BMS Watch.

Serves the Web UI dashboard, REST API endpoints, and Render health checks.
Built using standard library http.server for zero-dependency local execution.
"""
import json
import logging
import os
import re
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bms_core import (
    DATA_DIR,
    POPULAR_FORMATS,
    POPULAR_REGIONS,
    ROOT_DIR,
    get_bms_movies,
    get_city_venues,
    get_movie_info,
    today_ist,
    get_bms_regions,
    get_db,
    init_db,
    run_check_cycle,
)

STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("bms_watch.server")

class BMSWatchHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _json(self, status_code: int, data: dict | list):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        if path == "/api/cron":
            res = run_check_cycle()
            return self._json(200, res)

        # Dynamic BMS Regions
        if path == "/api/regions":
            return self._json(200, get_bms_regions())

        # Dynamic BMS Live Movies (Running or Upcoming)
        if path == "/api/bms/movies":
            qs = urllib.parse.parse_qs(url.query)
            region = qs.get("region", ["COIM"])[0].upper()
            slug = qs.get("slug", ["coimbatore"])[0].lower()
            mtype = qs.get("type", ["movies"])[0].lower()
            movies = get_bms_movies(region, slug, mtype)
            return self._json(200, movies)

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

        # Watches list (filtered by user if user_id or email query param provided)
        if path == "/api/watches":
            qs = urllib.parse.parse_qs(url.query)
            user_id = qs.get("user_id", [None])[0]
            email_filter = qs.get("email", [None])[0]

            with get_db() as conn:
                if user_id:
                    rows = conn.execute("SELECT * FROM watches WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
                elif email_filter:
                    rows = conn.execute("SELECT * FROM watches WHERE email = ? ORDER BY id DESC", (email_filter,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM watches ORDER BY id DESC").fetchall()

                watches = [dict(row) for row in rows]
                for w in watches:
                    w["dates"] = json.loads(w.get("dates") or "[]")
                    w["cinemas"] = json.loads(w.get("cinemas") or "[]")
                    w["formats"] = json.loads(w.get("formats") or "[]")
            return self._json(200, watches)

        # Alert logs
        if path == "/api/logs":
            with get_db() as conn:
                logs = [dict(row) for row in conn.execute("SELECT * FROM alert_logs ORDER BY id DESC LIMIT 50").fetchall()]
            return self._json(200, logs)

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
            result = run_check_cycle()
            return self._json(200, result)

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
            email = payload.get("email", "").strip()
            user_id = payload.get("user_id")

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

            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO watches (
                    user_id, movie_title, event_code, movie_slug, region_code, region_slug,
                    language, dates, cinemas, formats,
                    email, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    user_id, title, event_code, slug, region_code, region_slug,
                    language, json.dumps(dates), json.dumps(cinemas), json.dumps(formats),
                    email, now
                ))
                new_id = cur.lastrowid
                conn.commit()

            return self._json(201, {"status": "created", "watch_id": new_id})

        # Toggle Active / Paused
        match_toggle = re.match(r"^/api/watches/(\d+)/toggle$", path)
        if match_toggle:
            wid = int(match_toggle.group(1))
            with get_db() as conn:
                conn.execute("UPDATE watches SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (wid,))
                conn.commit()
            return self._json(200, {"status": "ok", "watch_id": wid})

        return self._json(404, {"error": "Route not found"})

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")
        match_del = re.match(r"^/api/watches/(\d+)$", path)
        if match_del:
            wid = int(match_del.group(1))
            with get_db() as conn:
                conn.execute("DELETE FROM watches WHERE id = ?", (wid,))
                conn.commit()
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
