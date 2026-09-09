from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from typing import Callable

from .config import Settings
from .database import Database, isoformat, parse_iso, utc_now
from .emailer import WeeklyEmailSender
from .exporter import export_xlsx
from .extractor import ArticleExtractor
from .models import FetchCounters, SearchCandidate
from .relevance import is_relevant
from .search import InternetSearchService
from .sheets import GoogleSheetsStore
from .url_utils import article_id, normalize_url

logger = logging.getLogger(__name__)


class FetchAlreadyRunning(RuntimeError):
    pass


class FetchPipeline:
    STAGES = ("Searching", "Collecting", "Extracting", "Deduplicating", "Saving", "Complete")

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        search_service: InternetSearchService,
        extractor: ArticleExtractor,
        sheets: GoogleSheetsStore,
        emailer: WeeklyEmailSender,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.settings = settings
        self.database = database
        self.search_service = search_service
        self.extractor = extractor
        self.sheets = sheets
        self.emailer = emailer
        self.clock = clock
        self._lock = threading.Lock()

    def run(self, *, run_type: str = "manual", fetch_id: str | None = None) -> dict:
        if not self._lock.acquire(blocking=False):
            raise FetchAlreadyRunning("Another fetch is already running")
        fetch_id = fetch_id or uuid.uuid4().hex
        started_at = self.clock().astimezone(UTC)
        counters = FetchCounters()
        fetch_created = False
        try:
            self.database.create_fetch(fetch_id, started_at, run_type)
            fetch_created = True
            last_successful = parse_iso(self.database.get_state("last_successful_fetch"))
            if last_successful:
                since = last_successful - timedelta(hours=self.settings.overlap_hours)
            else:
                since = started_at - timedelta(days=self.settings.initial_lookback_days)

            self.database.update_fetch(fetch_id, stage="Searching")
            candidates = self.search_service.search(since, started_at)
            counters.search_results = len(candidates)
            self.database.update_fetch(fetch_id, stage="Collecting", counters=counters)

            seen_this_run: set[str] = set()
            for candidate in candidates:
                self.database.update_fetch(fetch_id, stage="Extracting", counters=counters)
                try:
                    preliminary = normalize_url(candidate.url)
                    if preliminary in seen_this_run:
                        counters.duplicates += 1
                        continue
                    seen_this_run.add(preliminary)
                    known = self.database.find_by_normalized_url(preliminary)
                    if known is not None:
                        counters.duplicates += 1
                        if not known.description.strip():
                            refreshed = self.extractor.extract(candidate)
                            if refreshed and refreshed.description.strip():
                                self.database.update_description(
                                    known.id, refreshed.description
                                )
                            else:
                                self.database.record_extraction_failure(
                                    known.id,
                                    "Article body paragraph was not available during rediscovery",
                                )
                        continue
                    draft = self.extractor.extract(candidate)
                    if draft is None:
                        counters.failed_articles += 1
                        continue
                    if draft.published_at < since or draft.published_at > started_at + timedelta(days=1):
                        continue
                    self.database.update_fetch(fetch_id, stage="Deduplicating", counters=counters)
                    if draft.normalized_url in seen_this_run and draft.normalized_url != preliminary:
                        counters.duplicates += 1
                        continue
                    seen_this_run.add(draft.normalized_url)
                    if not is_relevant(draft):
                        continue
                    counters.relevant_articles += 1
                    duplicate_id = self.database.find_duplicate(draft)
                    if duplicate_id:
                        counters.duplicates += 1
                        self.database.add_url_alias(duplicate_id, draft.normalized_url)
                        if draft.description.strip():
                            self.database.update_description(
                                duplicate_id, draft.description
                            )
                        continue
                    if self.database.insert_article(article_id(draft.normalized_url), draft, started_at):
                        counters.new_articles += 1
                    else:
                        counters.duplicates += 1
                except Exception:
                    logger.exception("Article processing failed for %s", candidate.url)
                    counters.failed_articles += 1
                finally:
                    self.database.update_fetch(fetch_id, counters=counters)

            if candidates and counters.failed_articles == len(candidates):
                raise RuntimeError(
                    "Every discovered result failed article resolution/extraction; "
                    "the successful-fetch timestamp was not advanced"
                )

            self.database.update_fetch(fetch_id, stage="Saving", counters=counters)
            if hasattr(self.sheets, "reconcile"):
                sync_result = self.sheets.reconcile(self.database)
                synchronized = sync_result.get("synchronized_ids", [])
            else:
                all_articles = self.database.all_articles()
                synchronized = self.sheets.append_articles(all_articles)
                self.database.mark_synced(synchronized)
            sheets_status = (
                "disabled" if getattr(self.sheets, "enabled", True) is False else "success"
            )
            self.database.update_fetch(fetch_id, sheets_status=sheets_status)

            pending_success = self.database.unassigned_articles()
            # Recovered rows from an earlier failed delivery belong to this
            # successful run, even when their rediscovery was a duplicate today.
            counters.new_articles = len(pending_success)
            self.database.update_fetch(fetch_id, counters=counters)
            should_email = run_type == "scheduled" or self.settings.email_on_manual_fetch
            if should_email:
                new_xlsx = export_xlsx(pending_success)
                all_xlsx = export_xlsx(self.database.all_articles())
                self.emailer.send(
                    new_xlsx=new_xlsx,
                    all_xlsx=all_xlsx,
                    new_count=len(pending_success),
                    fetched_at=started_at,
                )
                self.database.update_fetch(
                    fetch_id,
                    email_status="success" if self.settings.email_enabled else "disabled",
                )
            else:
                self.database.update_fetch(fetch_id, email_status="not_required")

            self.database.assign_successful_fetch(
                [article.id for article in pending_success], fetch_id
            )
            ended_at = self.clock().astimezone(UTC)
            self.database.set_state("last_successful_fetch", isoformat(ended_at))
            self.database.set_state("last_successful_fetch_id", fetch_id)
            self.database.set_state("last_fetch_new_article_count", len(pending_success))
            self.database.finish_fetch(
                fetch_id,
                status="success",
                ended_at=ended_at,
                counters=counters,
            )
            return self.database.get_fetch(fetch_id) or {"id": fetch_id, "status": "success"}
        except Exception as exc:
            logger.exception("Fetch %s failed", fetch_id)
            ended_at = self.clock().astimezone(UTC)
            if fetch_created:
                self.database.finish_fetch(
                    fetch_id,
                    status="failed",
                    ended_at=ended_at,
                    counters=counters,
                    error=str(exc)[:2000],
                )
            raise
        finally:
            self._lock.release()

    def backfill_descriptions(self, *, limit: int | None = None) -> dict[str, int | str]:
        """Retry body extraction for historical rows without inserting articles."""
        attempted = updated = failed = 0
        for article in self.database.blank_description_articles(limit):
            attempted += 1
            candidate = SearchCandidate(
                url=article.original_url or article.url,
                title=article.title,
                published_at=article.published_at,
                publisher=article.publisher,
                source=article.source or "description-backfill",
            )
            try:
                draft = self.extractor.extract(candidate)
                if draft and draft.description.strip():
                    if self.database.update_description(article.id, draft.description):
                        updated += 1
                        continue
                failed += 1
                self.database.record_extraction_failure(
                    article.id, "No meaningful article-body paragraph could be identified"
                )
            except Exception as exc:
                failed += 1
                logger.exception("Description backfill failed for %s", article.url)
                self.database.record_extraction_failure(article.id, str(exc))

        sync_status = "disabled"
        try:
            if hasattr(self.sheets, "reconcile"):
                result = self.sheets.reconcile(self.database)
                sync_status = str(result.get("status", "unknown"))
            elif updated:
                synchronized = self.sheets.append_articles(self.database.all_articles())
                self.database.mark_synced(synchronized)
                sync_status = "success"
        except Exception:
            logger.exception("Description backfill completed but Sheet synchronization failed")
            sync_status = "failed"
        return {
            "status": "complete",
            "attempted": attempted,
            "updated": updated,
            "failed": failed,
            "sheet_sync_status": sync_status,
        }
