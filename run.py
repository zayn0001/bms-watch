"""Unified runner for BMS Watch.

Watches live in Supabase, so the web app and a local scheduled check share one database.

Usage:
  python run.py             # Start the web app (default port 8000)
  python run.py --cli       # Run a single check (Windows Task Scheduler runs this every 5 min)
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

from bms_core import init_db, run_check_cycle
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
        log.info("Running single check...")
        res = run_check_cycle()
        print(f"BMS Check Complete: {res['watches_checked']} watches, {res['total_changes']} change(s) found.")
        return

    # 3. Terminal loop
    if args.loop:
        log.info("Starting terminal loop every %ds (Press Ctrl+C to stop)...", args.loop)
        while True:
            try:
                run_check_cycle()
            except Exception:
                log.exception("Check crashed")
            time.sleep(args.loop)

    # 4. Default: the web app. Checks come from the local scheduled task (--cli) or Look now.
    log.info("Starting BMS Watch web platform on http://localhost:%d", args.port)
    run_server(args.port)


if __name__ == "__main__":
    main()
