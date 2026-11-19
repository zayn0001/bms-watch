"""Core BMS Watch engine: Scraping, Prime Seating Geometry, SQLite Storage, and Email Alerts.

Runs with ZERO external dependencies (standard library only) for 100% local portability.
"""
import datetime
import html
import json
import logging
import os
import smtplib
import sqlite3
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

# Base paths
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bms_watch.db"
LOG_PATH = DATA_DIR / "bms_watch.log"

# Default configuration
DEFAULT_SMTP_USER = os.getenv("SMTP_USER", "")
DEFAULT_SMTP_PASS = os.getenv("SMTP_PASS", "")
DEFAULT_MAIL_TO = [e.strip() for e in os.getenv("MAIL_TO", DEFAULT_SMTP_USER).split(",") if e.strip()]
DEFAULT_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
DEFAULT_SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

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

log = logging.getLogger("bms_watch.core")


# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,                  -- Supabase user ID UUID
            movie_title TEXT NOT NULL,
            event_code TEXT NOT NULL,
            movie_slug TEXT NOT NULL,
            region_code TEXT NOT NULL,
            region_slug TEXT NOT NULL,
            language TEXT DEFAULT 'english',
            dates TEXT NOT NULL,           -- JSON array of YYYYMMDD strings
            cinemas TEXT DEFAULT '[]',     -- JSON array of cinema names (empty = all)
            formats TEXT DEFAULT '[]',     -- JSON array of formats (empty = all)
            prime_seats_only INTEGER DEFAULT 0,
            min_seats_together INTEGER DEFAULT 2,
            email TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_alert_at TEXT
        );

        CREATE TABLE IF NOT EXISTS state_cache (
            key TEXT PRIMARY KEY,          -- event_code:region_code:date
            data TEXT NOT NULL,            -- JSON snapshot of showtimes
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER,
            title TEXT NOT NULL,
            details TEXT,
            sent_to TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(watch_id) REFERENCES watches(id) ON DELETE CASCADE
        );
        """)
        # Safe migration for existing DB
        try:
            conn.execute("ALTER TABLE watches ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass  # Already exists
        # Seed default watch from bms_watch.py if database is fresh
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM watches")
        if cur.fetchone()[0] == 0:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn.execute("""
            INSERT INTO watches (
                movie_title, event_code, movie_slug, region_code, region_slug,
                language, dates, cinemas, formats, prime_seats_only, min_seats_together,
                email, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, (
                "Avengers: Doomsday", "ET00526306", "avengers-doomsday",
                "COIM", "coimbatore", "english",
                json.dumps(["20261217", "20261218", "20261219", "20261220", "20261221"]),
                json.dumps([]),
                json.dumps([]),
                0, 2,
                DEFAULT_SMTP_USER or "mishal0404@gmail.com",
                now
            ))
            conn.commit()


# ---------------------------------------------------------------------------
# Prime Seating Detection Engine
# ---------------------------------------------------------------------------

class PrimeSeatingEngine:
    """Evaluates spatial geometry & seat categories to identify prime sweet-spot seating."""

    PREMIUM_CATEGORIES = {"RECLINER", "VIP", "CLUB", "PLATINUM", "EXECUTIVE", "ELITE", "BALCONY"}
    AVOID_CATEGORIES = {"NORMAL", "BUDGET", "FRONT", "SILVER"}

    @staticmethod
    def is_sweet_spot(row_idx: int, total_rows: int, col_idx: int, total_cols: int) -> bool:
        """Determines if a seat coordinate lies within the optical and acoustic sweet spot."""
        if total_rows <= 0 or total_cols <= 0:
            return True
        # Middle to back rows: 40% to 85% depth from screen
        row_ratio = row_idx / total_rows
        in_row_zone = 0.35 <= row_ratio <= 0.90

        # Center columns: 35% to 65% of screen width
        col_ratio = col_idx / total_cols
        in_col_zone = 0.32 <= col_ratio <= 0.68

        return in_row_zone and in_col_zone

    @classmethod
    def evaluate_layout(cls, seat_layout: dict, min_adjacent: int = 2) -> dict:
        """Evaluates a raw BookMyShow seat layout dictionary.
        Returns: { 'has_prime_seats': bool, 'prime_count': int, 'categories': list }
        """
        prime_count = 0
        categories_found = set()

        areas = seat_layout.get("areas", []) or seat_layout.get("colAreas", {}).get("objArea", [])
        for area in areas:
            desc = (area.get("AreaDesc") or area.get("areaDesc") or "").upper()
            rows = area.get("objRow", []) or area.get("rows", [])
            total_rows = len(rows)

            for r_idx, row in enumerate(rows):
                seats = row.get("objSeat", []) or row.get("seats", [])
                total_cols = len(seats)
                adjacent_streak = 0

                for c_idx, seat in enumerate(seats):
                    # Check seat status: 0 or "0" or "available" is open
                    status = str(seat.get("SeatStatus", seat.get("status", "1"))).lower()
                    is_available = status in ("0", "available")

                    # Check position
                    is_prime_pos = cls.is_sweet_spot(r_idx, total_rows, c_idx, total_cols)
                    is_premium_tier = any(p in desc for p in cls.PREMIUM_CATEGORIES)

                    if is_available and (is_prime_pos or is_premium_tier):
                        adjacent_streak += 1
                        if adjacent_streak >= min_adjacent:
                            prime_count += 1
                            categories_found.add(desc)
                    else:
                        adjacent_streak = 0

        return {
            "has_prime_seats": prime_count > 0,
            "prime_count": prime_count,
            "categories": sorted(categories_found)
        }


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
                v_name = v_data.get("venueName", o.get("id"))
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


def matches_watch_filters(diff_item: tuple, watch: dict) -> bool:
    """Checks if a detected diff matches the user's specific cinema and format criteria."""
    kind, cinema, show_key, old_label, new_label, show_obj = diff_item
    if not is_status_increase(old_label, new_label, kind):
        return False

    # Filter cinemas
    target_cinemas = json.loads(watch.get("cinemas") or "[]")
    if target_cinemas and not any(c.lower() in cinema.lower() for c in target_cinemas):
        return False

    # Filter formats
    target_formats = json.loads(watch.get("formats") or "[]")
    if target_formats and show_key:
        if not any(f.lower() in show_key.lower() for f in target_formats):
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
    pwd = smtp_pass or DEFAULT_SMTP_PASS
    host = smtp_host or DEFAULT_SMTP_HOST
    port = smtp_port or DEFAULT_SMTP_PORT
    recipient = watch.get("email") or (DEFAULT_MAIL_TO[0] if DEFAULT_MAIL_TO else "")

    if not user or not pwd or not recipient:
        log.warning("Email not sent: missing SMTP credentials or recipient (user=%s, recipient=%s)", user, recipient)
        return False

    movie_title = watch.get("movie_title", "Movie")
    subject = f"🎟️ BMS Watch: Tickets Available for {movie_title}!"
    html_body = generate_email_html(watch, changes_by_date)
    text_body = f"BookMyShow Update for {movie_title}!\n\nTickets/screens opened for your watch.\nCheck BookMyShow now."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL(host, port, timeout=20) as s:
        s.login(user, pwd)
        s.send_message(msg)

    log.info("Email alert successfully sent to %s for watch %s", recipient, watch.get("id"))
    return True


# ---------------------------------------------------------------------------
# Poller & Execution Cycle
# ---------------------------------------------------------------------------

def run_check_cycle() -> dict:
    """Executes a single checking cycle across all active watches.
    Returns: summary dict with change count and logs.
    """
    init_db()
    with get_db() as conn:
        watches = [dict(row) for row in conn.execute("SELECT * FROM watches WHERE is_active = 1").fetchall()]

    if not watches:
        log.info("No active watches to check.")
        return {"status": "ok", "watches_checked": 0, "total_changes": 0}

    # Deduplicate checks by (event_code, region_code, date)
    needed_queries = set()
    for w in watches:
        dates = json.loads(w.get("dates") or "[]")
        for d in dates:
            needed_queries.add((w["event_code"], w["region_code"], w["region_slug"], w.get("language", "english"), d))

    log.info("Checking %d unique showtime query(s) for %d active watch(es)...", len(needed_queries), len(watches))

    # Fetch and parse
    new_snapshots = {}
    for event_code, region_code, region_slug, lang, date in needed_queries:
        key = f"{event_code}:{region_code}:{date}"
        try:
            raw = fetch_bms_showtimes(event_code, date, region_code, region_slug, lang)
            new_snapshots[key] = parse_bms_showtimes(raw)
        except Exception:
            log.exception("Failed fetching showtimes for %s", key)

    total_changes = 0
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Compare with cached state and evaluate each watch
    with get_db() as conn:
        for w in watches:
            dates = json.loads(w.get("dates") or "[]")
            watch_changes_by_date = {}

            for d in dates:
                key = f"{w['event_code']}:{w['region_code']}:{d}"
                if key not in new_snapshots:
                    continue

                new_data = new_snapshots[key]
                cur = conn.execute("SELECT data FROM state_cache WHERE key = ?", (key,))
                row = cur.fetchone()
                old_data = json.loads(row[0]) if row else {}

                diffs = calculate_diff(old_data, new_data)
                matched = [diff for diff in diffs if matches_watch_filters(diff, w)]
                if matched:
                    watch_changes_by_date[d] = matched
                    total_changes += len(matched)

            if watch_changes_by_date:
                log.info("Watch #%s (%s): %d matching change(s) found!", w["id"], w["movie_title"], sum(map(len, watch_changes_by_date.values())))
                try:
                    sent = send_email_alert(w, watch_changes_by_date)
                    if sent:
                        conn.execute("INSERT INTO alert_logs (watch_id, title, details, sent_to, created_at) VALUES (?, ?, ?, ?, ?)", (
                            w["id"],
                            f"Alert for {w['movie_title']}",
                            f"{sum(map(len, watch_changes_by_date.values()))} change(s) detected",
                            w.get("email", ""),
                            now_str
                        ))
                        conn.execute("UPDATE watches SET last_alert_at = ? WHERE id = ?", (now_str, w["id"]))
                except Exception:
                    log.exception("Failed sending email alert for watch #%s", w["id"])

            conn.execute("UPDATE watches SET last_checked_at = ? WHERE id = ?", (now_str, w["id"]))

        # Update cache with new snapshots
        for key, snapshot in new_snapshots.items():
            conn.execute("INSERT OR REPLACE INTO state_cache (key, data, updated_at) VALUES (?, ?, ?)", (
                key, json.dumps(snapshot), now_str
            ))
        conn.commit()

    return {"status": "ok", "watches_checked": len(watches), "total_changes": total_changes}
