# BMS Watch (CineWatch)

Automated movie ticket availability tracker and alert system for BookMyShow (BMS).

Designed for moviegoers and cinephiles who want first-day-first-show (FDFS) in the formats they want (IMAX / EPIQ / PXL / 4DX) for high-demand releases without having to manually refresh the site every few minutes.

---

## Architecture & How It Works

- **Zero-Dependency Local Execution:** Runs with pure Python 3.12 (standard library). No mandatory Docker, Redis, or Node.js required to run locally.
- **Render Free Tier Deployment (`render.yaml`):** Deploys as a single Docker container web service in `singapore` (`ap-southeast-1`) with health check at `/api/health`.
- **Supabase Storage S3 Mirroring (`mirror.py`):** Since Render free tier disks are ephemeral, `mirror.py` syncs the local SQLite database (`/data`) with Supabase Storage S3 (`default` bucket, folder `bms_watch/`), exactly matching the setup used in `openGym` and `food-tracker`.

```mermaid
flowchart TD
    subgraph Local ["Local Development (Zero Setup)"]
        CLI["python run.py --cli (Direct Terminal Check)"]
        WEB["python run.py (Web Dashboard http://localhost:8000)"]
        LocalDB[("Local SQLite: data/bms_watch.db")]
        CLI --> LocalDB
        WEB --> LocalDB
    end

    subgraph RenderSupabase ["Render Free Tier + Supabase Storage"]
        RenderService["Render Web Service (PORT: 10000)"]
        Mirror["mirror.py (AWS SigV4 Sync Engine)"]
        SupabaseS3[("Supabase Storage: default/bms_watch/")]
        RenderService <--> Mirror <--> SupabaseS3
    end

    WEB --> BMS["BookMyShow v5 APIs"]
    CLI --> BMS
    RenderService --> BMS
```

---

## Supabase Integration (Shared Project, Dedicated Schema)

This app integrates with your shared Supabase project (`ismyhkcsxrswqsfzfrxe`):

- **Shared Auth & User Database:** Shares `auth.users` with `evenly`. Users can log in with their existing account or sign in with a 6-digit email OTP.
- **Dedicated PostgreSQL Schema (`bms_watch`):** To avoid table name collisions with `public` or `evenly` (e.g. `evenly.groups`), all BMS Watch tables live inside their own isolated schema:
  - `bms_watch.watches` (Row-Level Security: users only see and manage their own watches)
  - `bms_watch.alert_logs`
  - `bms_watch.state_cache`
- **Migration Script:** The complete SQL schema is in [`supabase/migrations/20261119000000_bms_watch_schema.sql`](./supabase/migrations/20261119000000_bms_watch_schema.sql).
- **PostgREST Configuration:** In the Supabase Dashboard, go to **Settings > API > PostgREST > Exposed Schemas** and add `bms_watch` alongside `public` and `evenly`.

---

## Quick Start (Local)

### 1. Configure Environment

Copy `.env.example` to `.env` and configure your email credentials:

```bash
cp .env.example .env
```

`.env`:
```ini
SMTP_USER=mishal0404@gmail.com
SMTP_PASS=your_gmail_app_password
MAIL_TO=mishal0404@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
```

### 2. Run Modes

#### Mode A: Full Web Dashboard & Background Poller (Recommended)
```bash
python run.py
```
Open **`http://localhost:8000`** in your browser:
- City auto-detected from your location (or type to search any BookMyShow city).
- New-watch form: pick a film, dates from today / release day onwards (bookable dates marked), cinemas multi-select, formats.
- Active Watches Dashboard with Pause / Resume / Delete actions.
- "Check Availability Now" manual trigger.

#### Mode B: Fast Terminal CLI Check (Like the initial script)
```bash
python run.py --cli
# OR:
python bms_watch.py
```

#### Mode C: Continuous Terminal Poller
```bash
python run.py --loop 180  # Checks every 180 seconds in terminal
```

#### Mode D: Offline Self-Tests
```bash
python run.py --test
```

---

## Render + Supabase Deployment

This repository includes a [`render.yaml`](./render.yaml) file configured for Render's free tier.

### Environment Variables on Render:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `PORT` | Service port | `10000` |
| `DATA_DIR` | Local container directory | `/data` |
| `POLL_INTERVAL` | Background check frequency in seconds | `180` |
| `MIRROR_ENDPOINT` | Supabase Storage S3 endpoint | `https://<ref>.storage.supabase.co/storage/v1/s3` |
| `MIRROR_BUCKET` | Supabase bucket name | `default` |
| `MIRROR_PREFIX` | App folder prefix in bucket | `bms_watch/` |
| `MIRROR_REGION` | Supabase bucket region | `ap-southeast-1` |
| `MIRROR_KEY_ID` | Supabase S3 Access Key ID | *(Secret)* |
| `MIRROR_SECRET` | Supabase S3 Secret Key | *(Secret)* |
| `SMTP_USER` | Email sender address | *(Secret)* |
| `SMTP_PASS` | Gmail App Password | *(Secret)* |

On container start, `mirror.py` pulls existing state from Supabase. While running, it pushes file changes every 5 seconds, and handles `SIGTERM` on shutdown/deploy to guarantee no watches are lost.
