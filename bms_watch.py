"""Watch BookMyShow showtimes/availability and email on any change.

Usage:
  python bms_watch.py            # check once (for Task Scheduler)
  python bms_watch.py --loop 300 # check every 300s forever
  python bms_watch.py --test     # offline self-check of the diff logic

Email settings: SMTP_* / MAIL_TO constants below. Log: bms_watch.log next to this file.
"""
import html, json, logging, os, smtplib, sys, time, urllib.parse, urllib.request
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Load local .env if present
_env_path = Path(__file__).with_name(".env")
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

EVENT = "ET00526306"          # Avengers: Doomsday
MOVIE_SLUG = "avengers-doomsday"
REGION = "COIM"               # Coimbatore
REGION_SLUG = "coimbatore"
LANGUAGE = "english"
DATES = ["20261217", "20261218", "20261219", "20261220", "20261221"]          # add more YYYYMMDD dates to watch
STATE = Path(__file__).with_name("state.json")
LOG = Path(__file__).with_name("bms_watch.log")

SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_TO = [e.strip() for e in os.getenv("MAIL_TO", SMTP_USER).split(",") if e.strip()]
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

API = "https://in.bookmyshow.com/api/movies-data/v5/showtimes-by-event/primary-dynamic"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "x-app-code": "WEB", "x-region-code": REGION, "x-region-slug": REGION_SLUG,
    "x-platform": "DESKTOP", "x-platform-code": "DESKTOP-WEB",
    "Referer": "https://in.bookmyshow.com/", "Accept": "application/json",
}
# pill colour the site shows for each show -> (label, colour used in the email)
PILL = {"green": ("AVAILABLE", "#1ea83c"), "orange": ("FAST FILLING", "#e8a900"),
        "red": ("ALMOST FULL", "#eb4e62"), "grey": ("SOLD OUT / UNAVAILABLE", "#999999")}

log = logging.getLogger("bms_watch")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr:  # pythonw (Task Scheduler) has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def fetch(date):
    q = urllib.parse.urlencode({
        "etCodes": "*", "dateCode": date, "isDesktop": "true", "regionCode": REGION,
        "xLocationShared": "false", "memberId": "", "lsId": "", "subCode": "",
        "appCode": "WEB", "language": LANGUAGE, "refEventCode": EVENT,
    })
    req = urllib.request.Request(f"{API}?{q}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse(resp):
    """-> {"Venue": {"07:00 AM EPIQ (Format)": "status"}}"""
    venues = {}

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            if o.get("type") == "venue-card":
                shows = {}
                for sec in o.get("showtimesSections", []):
                    fmt = " ".join(c.get("text", "") for t in sec.get("text", []) for c in t.get("components", [])).strip()
                    for s in sec.get("showtimes", []):
                        ad = s.get("additionalData", {})
                        style = s.get("styleId", "")
                        pill = next((v[0] for k, v in PILL.items() if style.startswith(k)), style or "?")
                        key = f"{ad.get('showTime', s.get('title'))} {s.get('screenAttr', '')} [{fmt}]".replace("  ", " ")
                        shows[key] = f"{pill} (availStatus={ad.get('availStatus')})"
                venues[o.get("additionalData", {}).get("venueName", o.get("id"))] = shows
            else:
                for v in o.values():
                    walk(v)

    walk(resp.get("data", {}).get("showtimeWidgets", []))
    return venues


def diff(old, new):
    """-> [(kind, cinema, show, old_status, new_status)], kind in new_cinema/cinema_gone/new_show/show_gone/changed"""
    out = []
    for v in sorted(set(old) | set(new)):
        if v not in old:
            out.append(("new_cinema", v, "", "", ""))
            out += [("new_show", v, k, "", s) for k, s in new[v].items()]
        elif v not in new:
            out.append(("cinema_gone", v, "", "", ""))
        else:
            o, n = old[v], new[v]
            for k in sorted(set(o) | set(n)):
                if k not in o:
                    out.append(("new_show", v, k, "", n[k]))
                elif k not in n:
                    out.append(("show_gone", v, k, o[k], ""))
                elif o[k] != n[k]:
                    out.append(("changed", v, k, o[k], n[k]))
    return out


RANK = {"SOLD OUT / UNAVAILABLE": 0, "ALMOST FULL": 1, "FAST FILLING": 2, "AVAILABLE": 3}


def rank(status):
    """(pill rank, bookable) e.g. "FAST FILLING (availStatus=1)" -> (2, True); None if label unknown."""
    label, _, rest = status.partition(" (availStatus=")
    r = RANK.get(label)
    return None if r is None else (r, rest.rstrip(")") != "0")


def is_increase(c):
    """Email-worthy: new cinema, a new bookable show, or a show whose availability went up."""
    kind, _, _, old, new = c
    if kind == "new_cinema":
        return True
    if kind == "new_show":
        return rank(new) is None or rank(new) > (0, False)
    if kind == "changed":
        return rank(old) is None or rank(new) is None or rank(new) > rank(old)
    return False  # show removed / cinema gone


def as_text(date, c):
    kind, v, show, old, new = c
    status = f"{old} -> {new}" if old and new else old or new
    return f"[{date}] {kind}: {v}" + (f" | {show}" if show else "") + (f" | {status}" if status else "")


BADGE = {"new_cinema": ("NEW CINEMA", "#1ea83c"), "cinema_gone": ("CINEMA GONE", "#eb4e62"),
         "new_show": ("NEW SHOW", "#2f80ed"), "show_gone": ("SHOW REMOVED", "#eb4e62"), "changed": ("CHANGED", "#e8a900")}


def pill(status):
    if not status:
        return ""
    colour = next((c for label, c in PILL.values() if status.startswith(label)), "#666666")
    label = html.escape(status.split(" (")[0])
    return (f'<span style="display:inline-block;padding:2px 8px;border:1px solid {colour};color:{colour};'
            f'border-radius:4px;font-size:12px;font-weight:600;white-space:nowrap">{label}</span>')


def as_html(changes_by_date):
    cell = "padding:8px 10px;border-bottom:1px solid #eeeeee;font-size:14px;vertical-align:top"
    parts = []
    for date, changes in changes_by_date.items():
        url = f"https://in.bookmyshow.com/movies/{REGION_SLUG}/{MOVIE_SLUG}/buytickets/{EVENT}/{date}"
        nice = time.strftime("%a %d %b %Y", time.strptime(date, "%Y%m%d"))
        rows = []
        for kind, v, show, old, new in changes:
            label, colour = BADGE[kind]
            status = f"{pill(old)} &rarr; {pill(new)}" if old and new else pill(old or new)
            rows.append(
                f'<tr><td style="{cell}"><span style="background:{colour};color:#fff;padding:2px 6px;border-radius:3px;'
                f'font-size:11px;font-weight:700;white-space:nowrap">{label}</span></td>'
                f'<td style="{cell}"><b>{html.escape(v)}</b>'
                + (f'<br><span style="color:#666">{html.escape(show)}</span>' if show else "")
                + f'</td><td style="{cell}">{status}</td></tr>')
        parts.append(
            f'<h3 style="margin:24px 0 8px;font-size:16px;color:#333">{nice} '
            f'<a href="{url}" style="font-size:13px;color:#eb4e62;font-weight:normal">Book &rsaquo;</a></h3>'
            f'<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse">{"".join(rows)}</table>')
    total = sum(map(len, changes_by_date.values()))
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto;color:#333">'
            f'<div style="background:#eb4e62;color:#fff;padding:16px 20px;border-radius:6px 6px 0 0">'
            f'<div style="font-size:18px;font-weight:700">BookMyShow availability update</div>'
            f'<div style="font-size:13px;opacity:.9">{total} change(s) &middot; {REGION_SLUG.title()} &middot; {EVENT}</div></div>'
            f'<div style="padding:4px 20px 20px;border:1px solid #eeeeee;border-top:0">{"".join(parts)}'
            f'<p style="color:#999;font-size:12px;margin-top:24px">Sent by bms_watch.py at {time.strftime("%d %b %Y %H:%M")}</p>'
            f'</div></div>')


def email(subject, text, html_body=None):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, ", ".join(MAIL_TO)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


FAIL_AFTER = 3  # consecutive failed runs before emailing (task runs every minute -> ~3 min), so one blip doesn't alert


def alert_on_failures(state, errors):
    """Email once when fetching keeps failing, and once when it recovers. Tracks the streak in state."""
    streak = state.get("_fail_streak", 0)
    if errors:
        streak += 1
        if streak == FAIL_AFTER:
            send_status("API FAILING", "#eb4e62",
                        f"Fetching showtimes failed {streak} runs in a row. Checks continue; you'll get one email when it recovers.",
                        errors)
    else:
        if streak >= FAIL_AFTER:
            send_status("API RECOVERED", "#1ea83c", f"Fetching works again after {streak} failed runs.", [])
        streak = 0
    state["_fail_streak"] = streak


def send_status(title, colour, msg, errors):
    items = "".join(f'<li style="font-family:Consolas,monospace;font-size:12px">{html.escape(e)}</li>' for e in errors)
    body = (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto;color:#333">'
            f'<div style="background:{colour};color:#fff;padding:16px 20px;border-radius:6px 6px 0 0;font-size:18px;font-weight:700">'
            f'BMS watch: {title}</div><div style="padding:16px 20px;border:1px solid #eeeeee;border-top:0">'
            f'<p>{html.escape(msg)}</p>' + (f'<ul>{items}</ul>' if items else "")
            + f'<p style="color:#999;font-size:12px">Details in {LOG.name}. Sent {time.strftime("%d %b %Y %H:%M")}</p></div></div>')
    try:
        email(f"BMS watch: {title}", msg + "\n\n" + "\n".join(errors), body)
        log.warning("status email sent: %s", title)
    except Exception:
        log.exception("status email failed: %s", title)


def check():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    by_date, errors = {}, []
    for date in DATES:
        try:
            new = parse(fetch(date))
        except Exception as e:  # network/Cloudflare hiccup: skip, don't wipe state
            log.exception("%s: fetch failed", date)
            errors.append(f"{date}: {type(e).__name__}: {e}")
            continue
        n_shows = sum(map(len, new.values()))
        if date in state:
            d = diff(state[date], new)
            if d:
                by_date[date] = d
            log.info("%s: %d shows in %d cinemas, %d change(s)", date, n_shows, len(new), len(d))
        else:
            log.info("%s: baseline saved (%d shows in %d cinemas)", date, n_shows, len(new))
        state[date] = new
    alert_on_failures(state, errors)
    STATE.write_text(json.dumps(state, indent=1))
    for dt, cs in by_date.items():
        for c in cs:
            log.info("change%s %s", "" if is_increase(c) else " (no email)", as_text(dt, c))
    by_date = {dt: kept for dt, cs in by_date.items() if (kept := [c for c in cs if is_increase(c)])}
    if by_date:
        text = "\n".join(as_text(dt, c) for dt, cs in by_date.items() for c in cs)
        total = sum(map(len, by_date.values()))
        try:
            email(f"BMS: {total} change(s) for {EVENT}", text, as_html(by_date))
            log.info("email sent to %d recipient(s)", len(MAIL_TO))
        except Exception:
            log.exception("email failed")


def _test():
    old = {"A": {"7AM": "SOLD OUT / UNAVAILABLE (availStatus=0)"}, "B": {"1PM": "AVAILABLE (availStatus=1)"}}
    new = {"A": {"7AM": "AVAILABLE (availStatus=1)", "9AM": "AVAILABLE (availStatus=1)"},
           "C": {"5PM": "FAST FILLING (availStatus=1)"}}
    d = diff(old, new)
    assert ("changed", "A", "7AM", old["A"]["7AM"], new["A"]["7AM"]) in d
    assert ("new_show", "A", "9AM", "", new["A"]["9AM"]) in d
    assert ("cinema_gone", "B", "", "", "") in d and ("new_cinema", "C", "", "", "") in d
    assert diff(new, new) == []
    S, FF, AV = "SOLD OUT / UNAVAILABLE (availStatus=0)", "FAST FILLING (availStatus=1)", "AVAILABLE (availStatus=1)"
    assert is_increase(("changed", "A", "7AM", S, FF)) and is_increase(("changed", "A", "7AM", FF, AV))
    assert not is_increase(("changed", "A", "7AM", FF, S)) and not is_increase(("changed", "A", "7AM", AV, FF))
    assert is_increase(("changed", "A", "7AM", S, "SOLD OUT / UNAVAILABLE (availStatus=1)"))  # became bookable
    assert is_increase(("new_show", "A", "9AM", "", FF)) and not is_increase(("new_show", "A", "9AM", "", S))
    assert is_increase(("new_cinema", "C", "", "", "")) and not is_increase(("cinema_gone", "B", "", "", ""))
    assert not is_increase(("show_gone", "A", "7AM", AV, ""))
    sent = []
    globals()["send_status"] = lambda title, *a: sent.append(title)
    st = {}
    for _ in range(FAIL_AFTER + 2):
        alert_on_failures(st, ["boom"])
    alert_on_failures(st, [])
    alert_on_failures(st, [])
    assert sent == ["API FAILING", "API RECOVERED"] and st["_fail_streak"] == 0, sent
    h = as_html({"20261217": d})
    assert "#1ea83c" in h and "CINEMA GONE" in h and "&rarr;" in h
    Path(__file__).with_name("preview.html").write_text(h, encoding="utf-8")
    print("ok (email preview written to preview.html)")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
    else:
        setup_logging()
        if "--loop" in sys.argv:
            every = int(sys.argv[sys.argv.index("--loop") + 1])
            while True:
                try:
                    check()
                except Exception:
                    log.exception("check crashed")
                time.sleep(every)
        else:
            check()
