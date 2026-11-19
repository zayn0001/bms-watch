"""Unified runner for BMS Watch.

Supports both local CLI mode (identical to the initial script) and Full Web Server mode
with Supabase Storage S3 mirror. On Render, checks are started by the GitHub Action calling
/api/cron, not by a loop in the web app.

Usage:
  python run.py             # Start the web app (default port 8000)
  python run.py --cli       # Run a single check from terminal (Task Scheduler / Cron)
  python run.py --loop 300  # Run terminal poller every 300s forever
  python run.py --test      # Offline self-check
"""
import argparse
import logging
import os
import sys
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
        from bms_core import calculate_diff, response_date
        print("Running wrong-date fallback test...")
        assert response_date({"data": {"additionalData": {"dateCode": "20261217"}}}) == "20261217"
        print("Running Diff test...")
        diff = calculate_diff({"A": {"7AM": {"status_label": "SOLD OUT"}}}, {"A": {"7AM": {"status_label": "AVAILABLE"}}})
        assert len(diff) == 1 and diff[0][0] == "changed", "Diff check failed"
        print("Running overlap-lock test...")
        import bms_core
        with bms_core._CHECK_LOCK:
            assert run_check_cycle()["status"] == "busy", "Concurrent check was not blocked"
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

    # 4. Default: web app + Supabase S3 mirror. Checks come from /api/cron (GitHub Action) or Look now.
    mirror = SupabaseStorageMirror(DATA_DIR)
    mirror.pull()
    mirror.start_background_sync(interval_sec=5)

    log.info("Starting BMS Watch web platform on http://localhost:%d", args.port)
    try:
        run_server(args.port)
    finally:
        mirror.push()


if __name__ == "__main__":
    main()
