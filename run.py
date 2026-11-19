"""Unified runner for BMS Watch.

Supports both local CLI mode (identical to the initial script) and Full Web Server mode
with automatic background poller and Supabase Storage S3 mirror.

Usage:
  python run.py             # Start Full Web App & Background Poller (default port 8000)
  python run.py --cli       # Run a single check from terminal (Task Scheduler / Cron)
  python run.py --loop 300  # Run terminal poller every 300s forever
  python run.py --test      # Offline self-check
"""
import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

# Setup paths and environment
ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from bms_core import DATA_DIR, init_db, run_check_cycle
from mirror import SupabaseStorageMirror
from server import run_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("bms_watch.runner")


def background_poller(interval_sec: int, stop_event: threading.Event):
    log.info("Background poller worker started (interval: %ds)", interval_sec)
    # Initial check after 5 seconds
    time.sleep(5)
    while not stop_event.is_set():
        try:
            log.info("Executing scheduled BMS check cycle...")
            result = run_check_cycle()
            log.info("Check cycle completed: %s", result)
        except Exception:
            log.exception("Error in background check cycle")
        stop_event.wait(interval_sec)


def main():
    parser = argparse.ArgumentParser(description="BMS Watch Runner")
    parser.add_argument("--cli", action="store_true", help="Run a single CLI check and exit")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="Run poller loop in terminal")
    parser.add_argument("--test", action="store_true", help="Run offline tests")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")), help="Web server port")
    args = parser.parse_args()

    # Initialize local DB
    init_db()

    # 1. Offline tests
    if args.test:
        from bms_core import PrimeSeatingEngine, calculate_diff
        print("Running PrimeSeatingEngine test...")
        mock_layout = {
            "areas": [
                {
                    "areaDesc": "CLUB",
                    "rows": [
                        {"seats": [{"status": "available"}, {"status": "available"}, {"status": "sold"}]}
                    ]
                }
            ]
        }
        res = PrimeSeatingEngine.evaluate_layout(mock_layout, min_adjacent=2)
        assert res["has_prime_seats"] is True, "Prime seating check failed"
        print("Running Diff test...")
        diff = calculate_diff({"A": {"7AM": {"status_label": "SOLD OUT"}}}, {"A": {"7AM": {"status_label": "AVAILABLE"}}})
        assert len(diff) == 1 and diff[0][0] == "changed", "Diff check failed"
        print("All tests passed successfully!")
        return

    # 2. Single CLI check
    if args.cli:
        mirror = SupabaseStorageMirror(DATA_DIR)
        mirror.pull()
        try:
            log.info("Running single check...")
            res = run_check_cycle()
            print(f"BMS Check Complete: {res['watches_checked']} watches, {res['total_changes']} change(s) found.")
        finally:
            mirror.push()
        return

    # 3. Terminal loop
    if args.loop:
        mirror = SupabaseStorageMirror(DATA_DIR)
        mirror.pull()
        mirror.start_background_sync(interval_sec=5)
        every = args.loop
        log.info("Starting terminal loop every %ds (Press Ctrl+C to stop)...", every)
        try:
            while True:
                try:
                    run_check_cycle()
                except Exception:
                    log.exception("Check crashed")
                time.sleep(every)
        finally:
            mirror.push()

    # 4. Default: Full Web App + Background Worker + Supabase S3 Mirror
    mirror = SupabaseStorageMirror(DATA_DIR)
    mirror.pull()
    mirror.start_background_sync(interval_sec=5)

    poll_interval = int(os.getenv("POLL_INTERVAL", "180"))
    stop_event = threading.Event()
    poller_thread = threading.Thread(
        target=background_poller,
        args=(poll_interval, stop_event),
        daemon=True,
        name="bms-poller"
    )
    poller_thread.start()

    log.info("Starting BMS Watch web platform on http://localhost:%d", args.port)
    try:
        run_server(args.port)
    finally:
        stop_event.set()
        mirror.push()


if __name__ == "__main__":
    main()
