"""Applies supabase/migrations to the shared Supabase project (ismyhkcsxrswqsfzfrxe).

Supports two methods:
1. Supabase Access Token (SUPABASE_ACCESS_TOKEN): via Supabase Management API.
2. PostgreSQL Session Pooler (POSTGRES_PASSWORD or DATABASE_URL): via psql CLI.

Usage:
  python scripts/db_push.py
"""
import getpass
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MIGRATION_FILE = ROOT_DIR / "supabase" / "migrations" / "20261119000000_bms_watch_schema.sql"
ENV_FILE = ROOT_DIR / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

PROJECT_REF = os.getenv("SUPABASE_PROJECT_REF", "ismyhkcsxrswqsfzfrxe")
REGION = os.getenv("SUPABASE_REGION", "ap-southeast-1")


def run_via_api(token: str, sql: str) -> bool:
    print(f"Applying migration via Supabase Management API to project {PROJECT_REF}...")
    url = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": sql}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
            print("Migration applied successfully!")
            print(json.dumps(res, indent=2))
            return True
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"API Error ({e.code}): {err_msg}")
        return False
    except Exception as e:
        print(f"Failed to execute API request: {e}")
        return False


def run_via_psql(db_password: str, sql_file: Path) -> bool:
    host = f"aws-0-{REGION}.pooler.supabase.com"
    user = f"postgres.{PROJECT_REF}"
    port = "5432"
    db = "postgres"
    conn_str = f"postgresql://{user}:{db_password}@{host}:{port}/{db}?sslmode=require"
    
    print(f"Applying migration via psql to {user}@{host}:{port}/{db}...")
    try:
        res = subprocess.run(
            ["psql", conn_str, "-f", str(sql_file)],
            capture_output=True,
            text=True,
            check=True
        )
        print("Migration applied successfully!")
        print(res.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"psql Error (exit {e.returncode}):\n{e.stderr}")
        return False
    except FileNotFoundError:
        print("psql command not found in PATH.")
        return False


def main():
    if not MIGRATION_FILE.exists():
        print(f"Migration file not found: {MIGRATION_FILE}")
        sys.exit(1)

    sql = MIGRATION_FILE.read_text(encoding="utf-8")

    token = os.getenv("SUPABASE_ACCESS_TOKEN")
    db_pass = os.getenv("POSTGRES_PASSWORD")

    # If neither is set in .env, prompt the user
    if not token and not db_pass:
        print("=" * 60)
        print(f"Supabase Project: {PROJECT_REF}")
        print("Choose authentication method to apply migration:")
        print("  1. Supabase Access Token (sbp_...) [https://supabase.com/dashboard/account/tokens]")
        print("  2. Database Password (from Supabase Project Settings > Database)")
        print("=" * 60)
        choice = input("Enter choice (1 or 2): ").strip()
        if choice == "1":
            token = getpass.getpass("Enter Supabase Access Token (sbp_...): ").strip()
        else:
            db_pass = getpass.getpass("Enter Database Password: ").strip()

    if token:
        success = run_via_api(token, sql)
    elif db_pass:
        success = run_via_psql(db_pass, MIGRATION_FILE)
    else:
        print("No valid credentials provided.")
        sys.exit(1)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
