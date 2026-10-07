from __future__ import annotations

import hmac
import logging
import math
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from dateutil.parser import parse as parse_date

from .config import Settings
from .database import Database, utc_now
from .emailer import WeeklyEmailSender
from .exporter import export_csv, export_xlsx
from .extractor import ArticleExtractor
from .models import ArticleDraft, SearchCandidate
from .pipeline import FetchAlreadyRunning, FetchPipeline
from .scheduler import (
    JOB_ID,
    create_scheduler,
    format_next_run,
    reschedule_job,
    weekly_trigger,
)
from .search import InternetSearchService
from .sheets import GoogleSheetsStore
from .url_utils import normalize_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

settings = Settings.from_env()
database = Database(settings.database_path)
database.seed_recipients_if_empty(settings.email_recipients)
search_service = InternetSearchService(settings)
extractor = ArticleExtractor(settings)
sheets = GoogleSheetsStore(settings)
emailer = WeeklyEmailSender(settings, database=database)
pipeline = FetchPipeline(
    settings=settings,
    database=database,
    search_service=search_service,
    extractor=extractor,
    sheets=sheets,
    emailer=emailer,
)
scheduler = create_scheduler(settings, pipeline, database=database)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-fetch")
submission_lock = threading.Lock()
active_future: Future | None = None


class RecipientCreate(BaseModel):
    email: str = Field(..., max_length=255)
    active: bool = True


class RecipientUpdate(BaseModel):
    email: str | None = Field(None, max_length=255)
    active: bool | None = None


class AutomationScheduleUpdate(BaseModel):
    enabled: bool = True
    frequency: str = "weekly"
    day_of_week: str = "mon"
    hour: int = Field(9, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)
    timezone: str = "Asia/Kolkata"
    cron: str | None = None


class SyncReviewResolution(BaseModel):
    action: Literal[
        "restore_to_sheet",
        "confirm_deletion",
        "import_to_database",
        "consolidate_duplicates",
    ]


class SlidingWindowRateLimiter:
    def __init__(self, maximum: int, window_seconds: int):
        self.maximum = maximum
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._events.get(key, []) if now - stamp < self.window_seconds]
            if len(recent) >= self.maximum:
                self._events[key] = recent
                return False
            recent.append(now)
            self._events[key] = recent
        return True


fetch_limiter = SlidingWindowRateLimiter(maximum=5, window_seconds=60)


def _log_future(future: Future) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("Background fetch finished with an error")


def _submit_fetch(run_type: str) -> str:
    global active_future
    with submission_lock:
        if pipeline._lock.locked() or (active_future is not None and not active_future.done()):
            raise FetchAlreadyRunning("A fetch is already running")
        fetch_id = uuid.uuid4().hex
        active_future = executor.submit(pipeline.run, run_type=run_type, fetch_id=fetch_id)
        active_future.add_done_callback(_log_future)
        return fetch_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    schedule = database.get_automation_schedule(settings.timezone)
    if settings.scheduler_enabled and schedule.get("enabled", True):
        scheduler.start()
        logger.info(
            "Automation scheduler started (%s) in %s",
            schedule.get("frequency"),
            schedule.get("timezone"),
        )
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        executor.shutdown(wait=False, cancel_futures=False)


app = FastAPI(
    title="Green Portfolio Media Monitor",
    version="1.0.0",
    docs_url="/api/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/":
        # The HTML carries versioned asset URLs; revalidate it so deployments
        # cannot pair a new navigation link with an older cached router.
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    if not settings.admin_api_key:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ADMIN_API_KEY must be configured in production",
            )
        return
    if not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid admin key is required",
        )


@app.get("/health")
def health(response: Response) -> dict[str, str]:
    # Heartbeat monitors must reach the application rather than reuse a cached
    # response, otherwise Render can still consider the service idle.
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


@app.get("/api/overview")
def overview() -> dict:
    now = utc_now()
    data = database.overview(now)
    schedule = database.get_automation_schedule(settings.timezone)
    job = scheduler.get_job(JOB_ID) if scheduler else None
    now_tz = now.astimezone(ZoneInfo(schedule.get("timezone", settings.timezone)))
    next_run = (
        job.trigger.get_next_fire_time(None, now_tz)
        if (job and schedule.get("enabled", True))
        else None
    )
    next_formatted = format_next_run(
        next_run,
        schedule.get("timezone", settings.timezone),
        schedule.get("frequency", "weekly"),
    )
    active_recipients = database.get_active_recipient_emails()
    all_recipients = database.list_recipients()

    data.update(
        {
            "next_scheduled_fetch": next_run.astimezone(UTC).isoformat() if next_run else None,
            "next_scheduled_fetch_formatted": next_formatted,
            "timezone": schedule.get("timezone", settings.timezone),
            "scheduler_enabled": bool(settings.scheduler_enabled and schedule.get("enabled", True)),
            "automation_schedule": schedule,
            "active_recipients_count": len(active_recipients),
            "total_recipients_count": len(all_recipients),
            "search_providers": settings.search_provider_names,
            "data_sync": database.data_sync_status(),
            "integrations": {
                "google_sheets": sheets.configuration_status(),
                "email": emailer.configuration_status(),
            },
        }
    )
    return data


@app.get("/api/articles")
def articles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=5, le=100),
    search: str = Query("", max_length=200),
    publisher: str = Query("", max_length=200),
    keyword: str = Query("", max_length=200),
    date_from: str = Query("", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query("", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
    sort: Literal["publisher", "published_at", "title"] = "published_at",
    direction: Literal["asc", "desc"] = "desc",
) -> dict:
    items, total = database.query_articles(
        page=page,
        page_size=page_size,
        search=search.strip(),
        publisher=publisher.strip(),
        keyword=keyword.strip(),
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        direction=direction,
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, math.ceil(total / page_size)),
    }


@app.get("/api/publishers")
def publishers() -> dict[str, list[str]]:
    return {"items": database.publishers()}


@app.get("/api/fetches")
def fetch_history(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"items": database.list_fetches(limit)}


@app.get("/api/fetches/{fetch_id}")
def fetch_status(fetch_id: str) -> dict:
    fetch = database.get_fetch(fetch_id)
    if not fetch:
        raise HTTPException(status_code=404, detail="Fetch not found")
    return fetch


@app.post("/api/fetch", status_code=status.HTTP_202_ACCEPTED)
def start_fetch(request: Request, _: None = Depends(require_admin_key)) -> dict[str, str]:
    client = request.client.host if request.client else "unknown"
    if not fetch_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many fetch requests; try again shortly")
    if not settings.search_provider_names:
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure a direct-URL search provider first: enable BING_NEWS_RSS_ENABLED, "
                "or configure NEWS_API_KEY, GNEWS_API_KEY, SERPER_API_KEY, or GOOGLE_CSE_API_KEY plus GOOGLE_CSE_ID"
            ),
        )
    try:
        fetch_id = _submit_fetch("manual")
    except FetchAlreadyRunning:
        raise HTTPException(status_code=409, detail="A fetch is already running")
    return {"fetch_id": fetch_id, "status": "accepted"}


@app.post("/api/jobs/weekly", status_code=status.HTTP_202_ACCEPTED)
def external_weekly_job(request: Request, _: None = Depends(require_admin_key)) -> dict[str, str]:
    client = request.client.host if request.client else "external-scheduler"
    if not fetch_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many fetch requests")
    if not settings.search_provider_names:
        raise HTTPException(status_code=503, detail="No direct-URL search provider is configured")
    try:
        fetch_id = _submit_fetch("scheduled")
    except FetchAlreadyRunning:
        raise HTTPException(status_code=409, detail="A fetch is already running")
    return {"fetch_id": fetch_id, "status": "accepted"}


# --- CMS Admin Endpoints ---


@app.get("/api/admin/recipients")
def list_recipients(_: None = Depends(require_admin_key)) -> dict:
    return {"items": database.list_recipients()}


@app.post("/api/admin/recipients", status_code=status.HTTP_201_CREATED)
def add_recipient(payload: RecipientCreate, _: None = Depends(require_admin_key)) -> dict:
    try:
        item = database.add_recipient(payload.email, payload.active)
        return item
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/admin/recipients/{recipient_id}")
def update_recipient(
    recipient_id: str,
    payload: RecipientUpdate,
    _: None = Depends(require_admin_key),
) -> dict:
    try:
        item = database.update_recipient(recipient_id, email=payload.email, active=payload.active)
        return item
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/admin/recipients/{recipient_id}")
def delete_recipient(recipient_id: str, _: None = Depends(require_admin_key)) -> dict:
    deleted = database.delete_recipient(recipient_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {"status": "deleted"}


@app.get("/api/admin/automation")
def get_automation(_: None = Depends(require_admin_key)) -> dict:
    schedule = database.get_automation_schedule(settings.timezone)
    job = scheduler.get_job(JOB_ID) if scheduler else None
    now_tz = datetime.now(ZoneInfo(schedule.get("timezone", settings.timezone)))
    next_run = (
        job.trigger.get_next_fire_time(None, now_tz)
        if (job and schedule.get("enabled", True))
        else None
    )
    return {
        "schedule": schedule,
        "next_scheduled_fetch": next_run.astimezone(UTC).isoformat() if next_run else None,
        "next_scheduled_fetch_formatted": format_next_run(
            next_run,
            schedule.get("timezone", settings.timezone),
            schedule.get("frequency", "weekly"),
        ),
    }


@app.post("/api/admin/automation")
def save_automation(payload: AutomationScheduleUpdate, _: None = Depends(require_admin_key)) -> dict:
    try:
        saved_schedule = database.save_automation_schedule(payload.model_dump())
        next_fire = reschedule_job(scheduler, saved_schedule, pipeline)
        if settings.scheduler_enabled and saved_schedule.get("enabled", True) and not scheduler.running:
            scheduler.start()
        return {
            "schedule": saved_schedule,
            "next_scheduled_fetch": next_fire.astimezone(UTC).isoformat() if next_fire else None,
            "next_scheduled_fetch_formatted": format_next_run(
                next_fire,
                saved_schedule.get("timezone", settings.timezone),
                saved_schedule.get("frequency", "weekly"),
            ),
            "status": "saved",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/sync-sheets")
def sync_sheets(_: None = Depends(require_admin_key)) -> dict:
    if pipeline._lock.locked():
        raise HTTPException(status_code=409, detail="A fetch is already running")
    if not sheets.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Sheets sync is not enabled in settings.",
        )
    return sheets.sync_database(database)


@app.get("/api/admin/data-sync")
def data_sync(_: None = Depends(require_admin_key)) -> dict:
    return {
        "summary": database.data_sync_status(),
        "items": database.list_sync_reviews(),
    }


@app.post("/api/admin/data-sync/{review_id}/resolve")
def resolve_data_sync(
    review_id: str,
    payload: SyncReviewResolution,
    _: None = Depends(require_admin_key),
) -> dict:
    if pipeline._lock.locked():
        raise HTTPException(status_code=409, detail="A fetch is already running")
    if not sheets.enabled:
        raise HTTPException(status_code=400, detail="Google Sheets sync is disabled")
    try:
        if payload.action == "restore_to_sheet":
            summary = sheets.restore_review(database, review_id)
        elif payload.action == "confirm_deletion":
            summary = sheets.confirm_removal(database, review_id)
        elif payload.action == "consolidate_duplicates":
            summary = sheets.consolidate_duplicate_review(database, review_id)
        else:
            review = database.get_sync_review(review_id)
            if not review or review["status"] != "pending" or review["review_type"] != "sheet_only":
                raise KeyError("Pending sheet-only review not found")
            row = review["sheet_data"]
            raw_url = str(row.get("url", "")).strip()
            normalized = normalize_url(raw_url)
            published = parse_date(str(row.get("published_date", "")))
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            candidate = SearchCandidate(
                url=raw_url,
                title=str(row.get("title", "")).strip(),
                published_at=published,
                publisher=str(row.get("publisher", "")).strip(),
                source="google-sheet-review",
            )
            extracted = extractor.extract(candidate)
            if extracted is not None:
                draft = extracted
            else:
                title = candidate.title
                publisher = candidate.publisher
                if not title or not publisher:
                    raise ValueError("Sheet row needs publisher, published date, title, and URL")
                draft = ArticleDraft(
                    publisher=publisher,
                    published_at=published,
                    title=title,
                    # A Sheet cell is not proof that text came from the article
                    # body. Keep it blank when live extraction cannot verify it.
                    description="",
                    url=normalized,
                    normalized_url=normalized,
                    source="google-sheet-review",
                    canonical_url=normalized,
                    original_url=raw_url,
                    normalized_title=" ".join(title.casefold().split()),
                )
            summary = sheets.import_review(database, review_id, draft)
        return {"status": "resolved", "summary": summary}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/backfill-descriptions")
def backfill_descriptions(
    limit: int | None = Query(None, ge=1, le=1000),
    _: None = Depends(require_admin_key),
) -> dict:
    if pipeline._lock.locked():
        raise HTTPException(status_code=409, detail="A fetch is already running")
    return pipeline.backfill_descriptions(limit=limit)


@app.get("/api/exports/{scope}.{file_format}")
def export_articles(
    scope: Literal["new", "all"], file_format: Literal["xlsx", "csv"]
) -> Response:
    if scope == "all":
        records = database.all_articles()
        basename = "green_portfolio_all_articles"
    else:
        latest_fetch_id = database.latest_successful_fetch_id()
        records = database.articles_for_fetch(latest_fetch_id) if latest_fetch_id else []
        basename = "new_articles"
    local_date = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    payload = export_xlsx(records) if file_format == "xlsx" else export_csv(records)
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if file_format == "xlsx"
        else "text/csv; charset=utf-8"
    )
    return Response(
        payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{basename}_{local_date}.{file_format}"'
        },
    )


static_dir = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(static_dir / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
