# Green Portfolio Media Monitor

A production-oriented, append-only media monitoring application for **Green Portfolio**, **Divam Sharma**, and **Anuj Jain**. It discovers fresh coverage, extracts original article metadata and the first body paragraph, filters relevance, prevents duplicates, appends new rows to Google Sheets, exports CSV/XLSX files, and sends the weekly report every Monday at 09:00 Asia/Kolkata.

The interface is an original internal-dashboard design informed by Green Portfolio's public brand language: deep teal, lime accents, Manrope UI type, and serif editorial headings. No reference-site code or content is used.

## What is included

- FastAPI backend and responsive HTML/CSS/JavaScript dashboard
- NewsAPI, GNews, Serper, and legacy Google Programmable Search provider support
- Incremental search window based on `last_successful_fetch`, with a configurable overlap
- Robots-aware, SSRF-protected article retrieval and canonical URL handling
- Original publication date, title, publisher, canonical URL, and actual first article paragraph extraction
- Canonical URL, normalized URL, title/publisher/date, and near-title deduplication
- SQLite state/history with a durable Docker volume
- Append-only Google Sheets integration for the supplied sheet ID
- Latest-successful-fetch and complete-dataset exports in XLSX and CSV
- Protected manual-fetch and scheduler endpoints with rate limiting
- APScheduler Monday 09:00 job plus an optional GitHub Actions wake-up trigger
- SMTP weekly email with both XLSX attachments
- Failure-safe retry behavior and per-run history

## Quick start

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Edit `.env`; it is loaded automatically. For a local UI-only evaluation without Google credentials, set `DATABASE_PATH=data/monitor.db`, `GOOGLE_SHEETS_ENABLED=false`, `EMAIL_ENABLED=false`, and keep `APP_ENV=development`.

```powershell
uvicorn app.main:app --reload
```

Open <http://localhost:8000>.

## Google Sheets setup

The default `GOOGLE_SHEET_ID` is already set to:

`11KZA69WxbxLeDP0O_Fg94CnooIWpzVnvJyatRuSP0rw`

1. In Google Cloud, enable the Google Sheets API.
2. Create a service account and a JSON key.
3. Share the existing sheet with the service account's `client_email` as an editor.
4. Put the complete JSON in the `GOOGLE_SERVICE_ACCOUNT_JSON` secret. Raw JSON and base64-encoded JSON are both accepted.
5. Leave `GOOGLE_WORKSHEET_NAME` blank to use the first worksheet (`gid=0`), or set its exact name.

The application never clears, deletes, or rewrites article rows. It creates the five required headers only if the worksheet is empty, validates existing headers, reads existing URLs before append, and uses `append_rows`. The internal article ID/hash and sync flags remain in SQLite.

Required sheet headers, in order:

| Publisher Website | Published Date | Title | Description | URL |
|---|---|---|---|---|

## Search configuration

Configure one or more direct-URL search providers:

- `NEWS_API_KEY`
- `GNEWS_API_KEY`
- `SERPER_API_KEY`
- `GOOGLE_CSE_API_KEY` together with `GOOGLE_CSE_ID`

NewsAPI is the simplest starting point and offers development keys. Google News RSS is intentionally disabled: its feed currently returns Google wrapper URLs blocked by `robots.txt`, and the feed terms restrict non-personal use. The monitor will never bypass that restriction or store a Google redirect as an article URL. Google Custom Search is available only for existing customers and is scheduled for discontinuation on 1 January 2027, so prefer NewsAPI, GNews, or Serper for new deployments.

All configured providers are queried and merged. A single provider failure is logged without aborting successful results from other providers. Search coverage is bounded by `MAX_ARTICLES_PER_FETCH`, and individual publisher-page failures do not fail the whole run. If every candidate fails resolution/extraction, the run fails and the successful-fetch timestamp remains unchanged.

## Incremental and failure semantics

On the first successful run, the search begins at `now - INITIAL_LOOKBACK_DAYS`. Later runs begin at:

```text
last_successful_fetch - OVERLAP_HOURS
```

The overlap protects against indexing delays and timezone differences; persistent URL hashes and secondary duplicate checks prevent re-insertion.

If Sheets or a required scheduled email fails, the run is marked failed and `last_successful_fetch` is not advanced. Locally discovered rows remain durable and unsynced rows are retried. Before retrying an append, the app checks URLs already present in the sheet, so a crash between a Sheets append and a local acknowledgement does not create duplicate rows. Historical rows are never deleted.

## Weekly email

Set:

```dotenv
EMAIL_ENABLED=true
EMAIL_RECIPIENTS=recipient@example.com,second@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=monitor@example.com
SMTP_USE_TLS=true
```

Scheduled runs send `new_articles_YYYY-MM-DD.xlsx` and `green_portfolio_all_articles_YYYY-MM-DD.xlsx`. Manual runs do not email unless `EMAIL_ON_MANUAL_FETCH=true`. A scheduled run is not marked successful if its required email fails.

## Scheduler and deployment

The container runs one application worker because the embedded APScheduler must have a single owner. `APP_TIMEZONE=Asia/Kolkata` and the cron trigger is Monday at 09:00 in that zone. The schedule does not depend on a browser session.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

The named `monitor-data` volume keeps SQLite state across restarts and redeploys. Back up this volume as part of normal operations. Use HTTPS at the reverse proxy/load balancer and store all `.env` values in the platform's secret manager.

For hosts that sleep, the optional GitHub Actions workflow calls `/api/jobs/weekly` at 03:30 UTC (09:00 IST). Add repository secrets `MEDIA_MONITOR_URL` and `MEDIA_MONITOR_ADMIN_KEY`, and set `SCHEDULER_ENABLED=false` on the deployed app so only one scheduler owns the run. The database lock also rejects overlapping fetch requests.

## Security notes

- Set a long random `ADMIN_API_KEY`; it protects both manual and external scheduled fetch endpoints.
- The dashboard asks for this key only when necessary and keeps it in browser `sessionStorage`, never in shipped JavaScript.
- Google/search/SMTP credentials remain server-side environment secrets.
- Article URLs are limited to public HTTP(S) destinations; private, loopback, link-local, reserved, and redirect targets are rejected.
- Article text is inserted into the UI through `textContent`, not HTML.
- Production should use HTTPS, one scheduler owner, a persistent volume, routine backups, and centralized logs.

## API surface

- `GET /api/overview`
- `GET /api/articles` — pagination, search, publisher, keyword, date, and sort filters
- `GET /api/publishers`
- `GET /api/fetches`
- `POST /api/fetch` — requires `X-Admin-Key` in production
- `POST /api/jobs/weekly` — same protection, sends scheduled email
- `GET /api/exports/new.xlsx` / `.csv`
- `GET /api/exports/all.xlsx` / `.csv`
- `GET /health`

## Verification

```powershell
pytest
```

The suite verifies:

1. First-run discovery, extraction fields, insertion, Sheet append behavior, timestamp state, and latest/full exports.
2. Second-run incremental windows, duplicate rejection, and preservation of existing rows.
3. Recovery behavior when a Sheet append fails.
4. Scheduled email generation with both valid XLSX payloads.
5. Monday 09:00 Asia/Kolkata scheduling (03:30 UTC).

Live Google, search-provider, and SMTP calls require your credentials and are intentionally not performed by the automated test suite.
