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
    get_db,
    init_db,
    run_check_cycle,
)

STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("bms_watch.server")

UPCOMING_MOVIES = [
    {
        "title": "Avengers: Doomsday",
        "event_code": "ET00526306",
        "slug": "avengers-doomsday",
        "release_date": "2026-12-18",
        "tag": "Marvel Studios Blockbuster",
        "poster": "https://assets-in.bmscdn.com/iedb/movies/images/mobile/thumbnail/xlarge/avengers-doomsday-et00526306-1734005886.jpg",
    },
    {
        "title": "Avatar: Fire and Ash",
        "event_code": "ET00311714",
        "slug": "avatar-fire-and-ash",
        "release_date": "2025-12-19",
        "tag": "James Cameron IMAX 3D Event",
        "poster": "https://assets-in.bmscdn.com/iedb/movies/images/mobile/thumbnail/xlarge/avatar-fire-and-ash-et00311714-1734005000.jpg",
    },
    {
        "title": "The Batman Part II",
        "event_code": "ET00358821",
        "slug": "the-batman-part-ii",
        "release_date": "2026-10-02",
        "tag": "DC Universe",
        "poster": "https://assets-in.bmscdn.com/iedb/movies/images/mobile/thumbnail/xlarge/the-batman-part-ii-et00358821-1683180000.jpg",
    }
]


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

        # Reference data
        if path == "/api/regions":
            return self._json(200, POPULAR_REGIONS)

        if path == "/api/formats":
            return self._json(200, POPULAR_FORMATS)

        if path == "/api/pre-release":
            return self._json(200, UPCOMING_MOVIES)

        # Watches list
        if path == "/api/watches":
            with get_db() as conn:
                watches = [dict(row) for row in conn.execute("SELECT * FROM watches ORDER BY id DESC").fetchall()]
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
            prime_seats = 1 if payload.get("prime_seats_only") else 0
            min_seats = int(payload.get("min_seats_together", 2))
            email = payload.get("email", "").strip()

            if not event_code:
                return self._json(400, {"error": "event_code is required (e.g. ET00526306)"})
            if not email:
                return self._json(400, {"error": "email is required"})
            if not dates:
                return self._json(400, {"error": "At least one date (YYYYMMDD) is required"})

            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO watches (
                    movie_title, event_code, movie_slug, region_code, region_slug,
                    language, dates, cinemas, formats, prime_seats_only, min_seats_together,
                    email, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, 'english', ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    title, event_code, slug, region_code, region_slug,
                    json.dumps(dates), json.dumps(cinemas), json.dumps(formats),
                    prime_seats, min_seats, email, now
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
