# BMS Watch (CineWatch)

Automated movie ticket availability tracker, prime seating finder, and alert system for BookMyShow (BMS).

Designed for moviegoers and cinephiles who want first-day-first-show (FDFS) and prime seats (middle center rows, IMAX / EPIQ / PXL / 4DX) for high-demand releases without having to manually refresh the site every few minutes.

---

## Key Features

- **Pre-Release / "Just About To Release" Watches:** Put watches on upcoming movies even before bookings open. Get alerted the moment the first screen/theatre goes live in your city.
- **Multi-Cinema & City Filtering:** Target specific favourite theatres (e.g., Broadway EPIQ Coimbatore, PVR Brookefields, INOX Prozone) or watch across an entire city.
- **Experience & Format Filters:** Filter alerts for IMAX 2D/3D, EPIQ, PXL, ICE, 4DX, ScreenX, Dolby Atmos, Standard 2D/3D.
- **Prime Seating Detection (Sweet Spot Engine):**
  - Analyzes cinema seat grid geometry (middle 35%–65% width and 40%–80% distance from screen).
  - Flags when $N$ contiguous seats are available in the acoustic/visual sweet spot.
  - Filters by seat categories (e.g., Recliners, Club, Elite, Executive).
- **Rich HTML Email Notifications:** Includes direct deep links to the BookMyShow checkout page for the exact venue and session ID.
- **Extensible Notifications:** Email (SMTP/Resend), Telegram Bot, WhatsApp, and Web Push notifications.

---

## Quick Start (CLI Mode)

### 1. Environment Setup

Copy `.env.example` to `.env` and fill in your SMTP details (e.g. Gmail App Password):

```bash
cp .env.example .env
```

`.env`:
```ini
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_gmail_app_password
MAIL_TO=recipient@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
```

### 2. Run Self-Test
```bash
python bms_watch.py --test
```

### 3. Check Once (e.g., for Task Scheduler or Cron)
```bash
python bms_watch.py
```

### 4. Continuous Polling
```bash
python bms_watch.py --loop 180  # Check every 180 seconds
```

---

## Project Roadmap: Becoming a Full-Fledged Web Platform

The project is evolving into a full-stack platform:
- **Backend:** FastAPI (Python 3.12) REST API for managing watches, cinemas, and users.
- **Worker:** Asynchronous deduplicated scheduler (queries BMS once per unique city/movie/date, then fans out to all subscribers).
- **Frontend:** Next.js / React web interface with a Watch Creation Wizard and interactive seating preview.
- **Database:** PostgreSQL / SQLite via SQLAlchemy with full watch audit trails.

See [`bms_watch_project_plan.md`](./docs/bms_watch_project_plan.md) for the complete architecture and implementation specifications.
