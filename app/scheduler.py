from __future__ import annotations

import logging
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .pipeline import FetchAlreadyRunning, FetchPipeline

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)

JOB_ID = "weekly-media-fetch"


def weekly_trigger(settings: Settings) -> CronTrigger:
    return CronTrigger(
        day_of_week="mon",
        hour=9,
        minute=0,
        timezone=ZoneInfo(settings.timezone),
    )


def trigger_from_schedule(schedule: dict[str, Any], default_timezone: str = "Asia/Kolkata") -> CronTrigger:
    tz_name = schedule.get("timezone") or default_timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")

    frequency = str(schedule.get("frequency", "weekly")).lower()
    hour = int(schedule.get("hour", 9))
    minute = int(schedule.get("minute", 0))

    if frequency == "daily":
        return CronTrigger(hour=hour, minute=minute, timezone=tz)
    elif frequency == "custom" and schedule.get("cron"):
        return CronTrigger.from_crontab(str(schedule["cron"]).strip(), timezone=tz)
    else:  # weekly (default)
        day_of_week = str(schedule.get("day_of_week", "mon")).lower()[:3]
        return CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=tz)


def format_next_run(
    next_fire: datetime | None,
    timezone_name: str = "Asia/Kolkata",
    frequency: str = "weekly",
) -> str | None:
    if not next_fire:
        return None
    try:
        tz = ZoneInfo(timezone_name)
        local_time = next_fire.astimezone(tz)
    except Exception:
        local_time = next_fire
    time_str = local_time.strftime("%I:%M %p")
    tz_abbr = local_time.tzname() or timezone_name
    if str(frequency).lower() in ("daily", "custom"):
        return f"{time_str} {tz_abbr}"
    weekday_name = local_time.strftime("%A")
    return f"{weekday_name}, {time_str} {tz_abbr}"


def reschedule_job(
    scheduler: BackgroundScheduler,
    schedule: dict[str, Any],
    pipeline: FetchPipeline,
) -> datetime | None:
    enabled = bool(schedule.get("enabled", True))
    if not enabled:
        if scheduler.get_job(JOB_ID):
            scheduler.remove_job(JOB_ID)
            logger.info("Automation job %s removed (automation disabled)", JOB_ID)
        return None

    trigger = trigger_from_schedule(schedule)

    def run_scheduled_fetch() -> None:
        try:
            pipeline.run(run_type="scheduled")
        except FetchAlreadyRunning:
            logger.warning("Scheduled fetch skipped because a fetch is already running")
        except Exception:
            logger.exception("Scheduled fetch failed")

    scheduler.add_job(
        run_scheduled_fetch,
        trigger=trigger,
        id=JOB_ID,
        name="Automated media fetch",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60 * 60 * 6,
    )
    now_tz = datetime.now(trigger.timezone)
    next_fire = trigger.get_next_fire_time(None, now_tz)
    logger.info(
        "Automation job %s scheduled (%s) - next fire at %s",
        JOB_ID,
        schedule.get("frequency", "weekly"),
        next_fire,
    )
    return next_fire


def create_scheduler(
    settings: Settings,
    pipeline: FetchPipeline,
    database: Database | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))
    if database is not None:
        schedule = database.get_automation_schedule(settings.timezone)
    else:
        schedule = {
            "enabled": settings.scheduler_enabled,
            "frequency": "weekly",
            "day_of_week": "mon",
            "hour": 9,
            "minute": 0,
            "timezone": settings.timezone,
        }

    reschedule_job(scheduler, schedule, pipeline)
    return scheduler


