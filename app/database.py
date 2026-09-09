from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from .models import ArticleDraft, ArticleRecord, FetchCounters


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fetches (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    search_results INTEGER NOT NULL DEFAULT 0,
                    relevant_articles INTEGER NOT NULL DEFAULT 0,
                    new_articles INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    failed_articles INTEGER NOT NULL DEFAULT 0,
                    sheets_status TEXT NOT NULL DEFAULT 'pending',
                    email_status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    publisher TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    synced_to_sheet INTEGER NOT NULL DEFAULT 0,
                    successful_fetch_id TEXT,
                    canonical_url TEXT NOT NULL DEFAULT '',
                    original_url TEXT NOT NULL DEFAULT '',
                    normalized_title TEXT NOT NULL DEFAULT '',
                    sheet_status TEXT NOT NULL DEFAULT 'pending',
                    extraction_status TEXT NOT NULL DEFAULT 'pending',
                    extraction_error TEXT,
                    description_updated_at TEXT,
                    FOREIGN KEY(successful_fetch_id) REFERENCES fetches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_published
                    ON articles(published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_articles_publisher
                    ON articles(publisher);
                CREATE INDEX IF NOT EXISTS idx_articles_successful_fetch
                    ON articles(successful_fetch_id);
                CREATE INDEX IF NOT EXISTS idx_articles_sheet_sync
                    ON articles(synced_to_sheet);

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS email_recipients (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_reviews (
                    id TEXT PRIMARY KEY,
                    review_key TEXT NOT NULL UNIQUE,
                    review_type TEXT NOT NULL,
                    article_id TEXT,
                    normalized_url TEXT NOT NULL DEFAULT '',
                    sheet_rows TEXT NOT NULL DEFAULT '[]',
                    sheet_data TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(article_id) REFERENCES articles(id)
                );

                CREATE TABLE IF NOT EXISTS article_url_aliases (
                    normalized_url TEXT PRIMARY KEY,
                    article_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(article_id) REFERENCES articles(id)
                );
                """
            )
            self._migrate_schema(connection)
            self._repair_false_successes(connection)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """Apply additive migrations without replacing existing user data."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(articles)").fetchall()
        }
        additions = {
            "canonical_url": "TEXT NOT NULL DEFAULT ''",
            "original_url": "TEXT NOT NULL DEFAULT ''",
            "normalized_title": "TEXT NOT NULL DEFAULT ''",
            "sheet_status": "TEXT NOT NULL DEFAULT 'pending'",
            "extraction_status": "TEXT NOT NULL DEFAULT 'pending'",
            "extraction_error": "TEXT",
            "description_updated_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")

        connection.execute(
            "UPDATE articles SET canonical_url = url WHERE canonical_url = ''"
        )
        connection.execute(
            "UPDATE articles SET original_url = url WHERE original_url = ''"
        )
        rows = connection.execute(
            "SELECT id, title FROM articles WHERE normalized_title = ''"
        ).fetchall()
        for row in rows:
            normalized_title = " ".join(str(row["title"]).casefold().split())
            connection.execute(
                "UPDATE articles SET normalized_title = ? WHERE id = ?",
                (normalized_title, row["id"]),
            )
        connection.execute(
            """UPDATE articles SET sheet_status = CASE
               WHEN synced_to_sheet = 1 THEN 'active' ELSE 'pending' END
               WHERE sheet_status = 'pending'"""
        )
        connection.execute(
            """UPDATE articles SET extraction_status = CASE
               WHEN length(trim(description)) > 0 THEN 'success' ELSE 'pending' END
               WHERE extraction_status = 'pending'"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_canonical_url ON articles(canonical_url)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_sheet_status ON articles(sheet_status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_reviews_status ON sync_reviews(status, review_type)"
        )
        connection.execute(
            """INSERT OR IGNORE INTO article_url_aliases
            (normalized_url, article_id, created_at)
            SELECT normalized_url, id, discovered_at FROM articles"""
        )

    @staticmethod
    def _repair_false_successes(connection: sqlite3.Connection) -> None:
        """Repair runs created by the former Google News wrapper behavior.

        Earlier builds could mark a run successful when every search candidate
        failed extraction. That timestamp would incorrectly shrink the next
        incremental window, so correct the history and rebuild success state.
        """
        invalid_rows = connection.execute(
            """SELECT id FROM fetches
            WHERE status = 'success'
              AND search_results > 0
              AND failed_articles >= search_results
              AND relevant_articles = 0
              AND new_articles = 0"""
        ).fetchall()
        if not invalid_rows:
            return

        invalid_ids = [str(row["id"]) for row in invalid_rows]
        placeholders = ",".join("?" for _ in invalid_ids)
        connection.execute(
            f"""UPDATE fetches
            SET status = 'failed', stage = 'Failed',
                sheets_status = 'not_attempted',
                error = COALESCE(error, 'All search candidates failed extraction; repaired during upgrade')
            WHERE id IN ({placeholders})""",
            invalid_ids,
        )

        state_row = connection.execute(
            "SELECT value FROM state WHERE key = 'last_successful_fetch_id'"
        ).fetchone()
        last_success_id = json.loads(state_row["value"]) if state_row else None
        if last_success_id not in invalid_ids:
            return

        latest_valid = connection.execute(
            """SELECT id, ended_at, new_articles FROM fetches
            WHERE status = 'success' ORDER BY ended_at DESC LIMIT 1"""
        ).fetchone()
        updated_at = isoformat()
        if latest_valid:
            state_values = {
                "last_successful_fetch_id": latest_valid["id"],
                "last_successful_fetch": latest_valid["ended_at"],
                "last_fetch_new_article_count": latest_valid["new_articles"],
            }
            for key, value in state_values.items():
                connection.execute(
                    """INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at""",
                    (key, json.dumps(value), updated_at),
                )
        else:
            connection.execute(
                """DELETE FROM state WHERE key IN
                ('last_successful_fetch_id', 'last_successful_fetch')"""
            )
            connection.execute(
                """INSERT INTO state(key, value, updated_at) VALUES
                ('last_fetch_new_article_count', '0', ?)
                ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at""",
                (updated_at,),
            )

        newest = connection.execute(
            "SELECT status FROM fetches ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if newest:
            connection.execute(
                """INSERT INTO state(key, value, updated_at) VALUES
                ('last_fetch_status', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = excluded.updated_at""",
                (json.dumps(newest["status"]), updated_at),
            )

    def create_fetch(self, fetch_id: str, started_at: datetime, run_type: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO fetches
                (id, started_at, status, stage, run_type, email_status)
                VALUES (?, ?, 'running', 'Searching', ?, ?)""",
                (
                    fetch_id,
                    isoformat(started_at),
                    run_type,
                    "pending" if run_type == "scheduled" else "not_required",
                ),
            )
        self.set_state("last_fetch_status", "running")

    def update_fetch(
        self,
        fetch_id: str,
        *,
        stage: str | None = None,
        counters: FetchCounters | None = None,
        sheets_status: str | None = None,
        email_status: str | None = None,
    ) -> None:
        changes: list[str] = []
        values: list[Any] = []
        if stage is not None:
            changes.append("stage = ?")
            values.append(stage)
        if counters is not None:
            for field in (
                "search_results",
                "relevant_articles",
                "new_articles",
                "duplicates",
                "failed_articles",
            ):
                changes.append(f"{field} = ?")
                values.append(getattr(counters, field))
        if sheets_status is not None:
            changes.append("sheets_status = ?")
            values.append(sheets_status)
        if email_status is not None:
            changes.append("email_status = ?")
            values.append(email_status)
        if not changes:
            return
        values.append(fetch_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE fetches SET {', '.join(changes)} WHERE id = ?", values
            )

    def finish_fetch(
        self,
        fetch_id: str,
        *,
        status: str,
        ended_at: datetime,
        counters: FetchCounters,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE fetches SET ended_at = ?, status = ?, stage = ?,
                search_results = ?, relevant_articles = ?, new_articles = ?,
                duplicates = ?, failed_articles = ?, error = ? WHERE id = ?""",
                (
                    isoformat(ended_at),
                    status,
                    "Complete" if status == "success" else "Failed",
                    counters.search_results,
                    counters.relevant_articles,
                    counters.new_articles,
                    counters.duplicates,
                    counters.failed_articles,
                    error,
                    fetch_id,
                ),
            )
        self.set_state("last_fetch_status", status)

    def get_fetch(self, fetch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM fetches WHERE id = ?", (fetch_id,)).fetchone()
        return dict(row) if row else None

    def list_fetches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fetches ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = excluded.updated_at""",
                (key, encoded, isoformat()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def insert_article(self, article_id: str, draft: ArticleDraft, discovered_at: datetime) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO articles
                    (id, publisher, published_at, title, description, url,
                     normalized_url, source, discovered_at, canonical_url,
                     original_url, normalized_title, sheet_status,
                     extraction_status, description_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        article_id,
                        draft.publisher,
                        isoformat(draft.published_at),
                        draft.title,
                        draft.description,
                        draft.url,
                        draft.normalized_url,
                        draft.source,
                        isoformat(discovered_at),
                        draft.canonical_url or draft.url,
                        draft.original_url or draft.url,
                        draft.normalized_title or " ".join(draft.title.casefold().split()),
                        "success" if draft.description.strip() else "pending",
                        isoformat(discovered_at) if draft.description.strip() else None,
                    ),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO article_url_aliases
                    (normalized_url, article_id, created_at) VALUES (?, ?, ?)""",
                    (draft.normalized_url, article_id, isoformat(discovered_at)),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def find_duplicate(self, draft: ArticleDraft) -> str | None:
        with self.connect() as connection:
            exact = connection.execute(
                """SELECT a.id FROM articles a
                LEFT JOIN article_url_aliases u ON u.article_id = a.id
                WHERE a.normalized_url = ? OR a.canonical_url = ?
                   OR u.normalized_url = ? LIMIT 1""",
                (
                    draft.normalized_url,
                    draft.canonical_url or draft.normalized_url,
                    draft.normalized_url,
                ),
            ).fetchone()
            if exact:
                return str(exact["id"])

            date = draft.published_at.date().isoformat()
            rows = connection.execute(
                """SELECT id, publisher, title, published_at FROM articles
                WHERE substr(published_at, 1, 10) BETWEEN date(?, '-1 day') AND date(?, '+1 day')""",
                (date, date),
            ).fetchall()

        wanted_title = " ".join(draft.title.casefold().split())
        wanted_publisher = " ".join(draft.publisher.casefold().split())
        for row in rows:
            existing_title = " ".join(str(row["title"]).casefold().split())
            existing_publisher = " ".join(str(row["publisher"]).casefold().split())
            ratio = SequenceMatcher(None, wanted_title, existing_title).ratio()
            if wanted_title == existing_title and wanted_publisher == existing_publisher:
                return str(row["id"])
            if ratio >= 0.94 and wanted_publisher == existing_publisher:
                return str(row["id"])
            if ratio >= 0.985:
                return str(row["id"])
        return None

    def get_article(self, article_id: str) -> ArticleRecord | None:
        items = self._article_query("WHERE id = ?", (article_id,))
        return items[0] if items else None

    def find_by_normalized_url(self, normalized_url: str) -> ArticleRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.* FROM articles a
                LEFT JOIN article_url_aliases u ON u.article_id = a.id
                WHERE a.normalized_url = ? OR a.canonical_url = ?
                   OR u.normalized_url = ? LIMIT 1""",
                (normalized_url, normalized_url, normalized_url),
            ).fetchone()
        return self._row_to_article(row) if row else None

    def add_url_alias(self, article_id: str, normalized_url: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO article_url_aliases
                (normalized_url, article_id, created_at) VALUES (?, ?, ?)""",
                (normalized_url, article_id, isoformat()),
            )

    def url_aliases(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT normalized_url, article_id FROM article_url_aliases"
            ).fetchall()
        return {str(row["normalized_url"]): str(row["article_id"]) for row in rows}

    def blank_description_articles(self, limit: int | None = None) -> list[ArticleRecord]:
        suffix = "WHERE description IS NULL OR trim(description) = '' ORDER BY discovered_at"
        params: tuple[Any, ...] = ()
        if limit is not None:
            suffix += " LIMIT ?"
            params = (max(1, int(limit)),)
        return self._article_query(suffix, params)

    def update_description(self, article_id: str, description: str) -> bool:
        description = " ".join(description.split()).strip()[:5000]
        if not description:
            return False
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE articles SET description = ?, extraction_status = 'success',
                extraction_error = NULL, description_updated_at = ?
                WHERE id = ? AND (description IS NULL OR trim(description) = '')""",
                (description, isoformat(), article_id),
            )
        return cursor.rowcount > 0

    def record_extraction_failure(self, article_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE articles SET extraction_status = 'extraction_failed',
                extraction_error = ? WHERE id = ?
                AND (description IS NULL OR trim(description) = '')""",
                (error.strip()[:2000], article_id),
            )

    def unsynced_articles(self) -> list[ArticleRecord]:
        return self._article_query("WHERE synced_to_sheet = 0 ORDER BY discovered_at")

    def unassigned_articles(self) -> list[ArticleRecord]:
        return self._article_query("WHERE successful_fetch_id IS NULL ORDER BY published_at DESC")

    def articles_for_fetch(self, fetch_id: str) -> list[ArticleRecord]:
        return self._article_query(
            "WHERE successful_fetch_id = ? ORDER BY published_at DESC", (fetch_id,)
        )

    def all_articles(self) -> list[ArticleRecord]:
        return self._article_query("ORDER BY published_at DESC")

    def _article_query(self, suffix: str, params: tuple[Any, ...] = ()) -> list[ArticleRecord]:
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM articles {suffix}", params).fetchall()
        return [self._row_to_article(row) for row in rows]

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> ArticleRecord:
        keys = set(row.keys())
        return ArticleRecord(
            id=row["id"],
            publisher=row["publisher"],
            published_at=parse_iso(row["published_at"]) or utc_now(),
            title=row["title"],
            description=row["description"],
            url=row["url"],
            normalized_url=row["normalized_url"],
            source=row["source"],
            canonical_url=row["canonical_url"] if "canonical_url" in keys else row["url"],
            original_url=row["original_url"] if "original_url" in keys else row["url"],
            normalized_title=(
                row["normalized_title"]
                if "normalized_title" in keys
                else " ".join(str(row["title"]).casefold().split())
            ),
            discovered_at=row["discovered_at"],
            synced_to_sheet=bool(row["synced_to_sheet"]),
            successful_fetch_id=row["successful_fetch_id"],
            sheet_status=row["sheet_status"] if "sheet_status" in keys else "pending",
            extraction_status=(
                row["extraction_status"] if "extraction_status" in keys else "pending"
            ),
            extraction_error=row["extraction_error"] if "extraction_error" in keys else None,
        )

    def mark_synced(self, article_ids: list[str]) -> None:
        if not article_ids:
            return
        placeholders = ",".join("?" for _ in article_ids)
        with self.connect() as connection:
            connection.execute(
                f"""UPDATE articles SET synced_to_sheet = 1, sheet_status = 'active'
                WHERE id IN ({placeholders})""",
                article_ids,
            )

    def set_sheet_status(self, article_id: str, sheet_status: str) -> None:
        allowed = {"pending", "active", "missing_from_sheet", "intentionally_removed"}
        if sheet_status not in allowed:
            raise ValueError(f"Invalid sheet status: {sheet_status}")
        with self.connect() as connection:
            connection.execute(
                "UPDATE articles SET sheet_status = ? WHERE id = ?",
                (sheet_status, article_id),
            )

    def create_sync_review(
        self,
        *,
        review_key: str,
        review_type: str,
        article_id: str | None = None,
        normalized_url: str = "",
        sheet_rows: list[int] | None = None,
        sheet_data: dict[str, Any] | None = None,
    ) -> str:
        allowed = {"database_only", "sheet_only", "duplicate_sheet_rows"}
        if review_type not in allowed:
            raise ValueError(f"Invalid sync review type: {review_type}")
        now = isoformat()
        review_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sync_reviews
                (id, review_key, review_type, article_id, normalized_url,
                 sheet_rows, sheet_data, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(review_key) DO UPDATE SET
                    article_id = excluded.article_id,
                    normalized_url = excluded.normalized_url,
                    sheet_rows = excluded.sheet_rows,
                    sheet_data = excluded.sheet_data,
                    status = 'pending', resolution = NULL,
                    updated_at = excluded.updated_at""",
                (
                    review_id,
                    review_key,
                    review_type,
                    article_id,
                    normalized_url,
                    json.dumps(sheet_rows or []),
                    json.dumps(sheet_data or {}),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM sync_reviews WHERE review_key = ?", (review_key,)
            ).fetchone()
        return str(row["id"])

    def get_sync_review(self, review_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_reviews WHERE id = ?", (review_id,)
            ).fetchone()
        return self._review_dict(row) if row else None

    def list_sync_reviews(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE r.status = 'pending'" if pending_only else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT r.*, a.title AS article_title, a.publisher AS article_publisher,
                a.url AS article_url, a.sheet_status AS article_sheet_status
                FROM sync_reviews r LEFT JOIN articles a ON a.id = r.article_id
                {where} ORDER BY r.created_at, r.id"""
            ).fetchall()
        return [self._review_dict(row) for row in rows]

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["sheet_rows"] = json.loads(result.get("sheet_rows") or "[]")
        result["sheet_data"] = json.loads(result.get("sheet_data") or "{}")
        return result

    def resolve_sync_review(self, review_id: str, resolution: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE sync_reviews SET status = 'resolved', resolution = ?,
                updated_at = ? WHERE id = ? AND status = 'pending'""",
                (resolution, isoformat(), review_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Pending sync review '{review_id}' not found")

    def resolve_review_key(self, review_key: str, resolution: str = "synchronized") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE sync_reviews SET status = 'resolved', resolution = ?,
                updated_at = ? WHERE review_key = ? AND status = 'pending'""",
                (resolution, isoformat(), review_key),
            )

    def data_sync_status(self) -> dict[str, Any]:
        summary = self.get_state("sheet_sync_summary", {})
        if not isinstance(summary, dict):
            summary = {}
        with self.connect() as connection:
            database_articles = int(
                connection.execute("SELECT COUNT(1) FROM articles").fetchone()[0]
            )
            pending = int(
                connection.execute(
                    "SELECT COUNT(1) FROM sync_reviews WHERE status = 'pending'"
                ).fetchone()[0]
            )
            counts = {
                str(row["review_type"]): int(row["count"])
                for row in connection.execute(
                    """SELECT review_type, COUNT(1) AS count FROM sync_reviews
                    WHERE status = 'pending' GROUP BY review_type"""
                ).fetchall()
            }
        summary.update(
            {
                "database_articles": database_articles,
                "pending_reviews": pending,
                "database_only": counts.get("database_only", 0),
                "sheet_only": counts.get("sheet_only", 0),
                "duplicates": counts.get("duplicate_sheet_rows", 0),
            }
        )
        return summary

    def assign_successful_fetch(self, article_ids: list[str], fetch_id: str) -> None:
        if not article_ids:
            return
        placeholders = ",".join("?" for _ in article_ids)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE articles SET successful_fetch_id = ? WHERE id IN ({placeholders})",
                [fetch_id, *article_ids],
            )

    def latest_successful_fetch_id(self) -> str | None:
        return self.get_state("last_successful_fetch_id")

    def publishers(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT publisher FROM articles ORDER BY publisher COLLATE NOCASE"
            ).fetchall()
        return [str(row["publisher"]) for row in rows]

    def query_articles(
        self,
        *,
        page: int,
        page_size: int,
        search: str = "",
        publisher: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        sort: str = "published_at",
        direction: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        if search:
            token = f"%{search}%"
            clauses.append("(title LIKE ? OR description LIKE ? OR publisher LIKE ?)")
            values.extend((token, token, token))
        if publisher:
            clauses.append("publisher = ?")
            values.append(publisher)
        if keyword:
            clauses.append("(title LIKE ? OR description LIKE ?)")
            values.extend((f"%{keyword}%", f"%{keyword}%"))
        if date_from:
            clauses.append("date(published_at) >= date(?)")
            values.append(date_from)
        if date_to:
            clauses.append("date(published_at) <= date(?)")
            values.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sort_column = {
            "publisher": "publisher",
            "published_at": "published_at",
            "title": "title",
        }.get(sort, "published_at")
        order = "ASC" if direction.lower() == "asc" else "DESC"
        offset = (page - 1) * page_size
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM articles {where}", values
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT * FROM articles {where} ORDER BY {sort_column} {order} LIMIT ? OFFSET ?",
                [*values, page_size, offset],
            ).fetchall()
        return [self._row_to_article(row).public_dict() for row in rows], int(total)

    def overview(self, now: datetime) -> dict[str, Any]:
        week_start = now.astimezone(UTC) - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS count FROM articles").fetchone()["count"]
            this_week = connection.execute(
                "SELECT COUNT(*) AS count FROM articles WHERE published_at >= ?",
                (isoformat(week_start),),
            ).fetchone()["count"]
        return {
            "total_articles": int(total),
            "articles_this_week": int(this_week),
            "last_successful_fetch": self.get_state("last_successful_fetch"),
            "last_fetch_status": self.get_state("last_fetch_status", "never_run"),
            "last_fetch_new_article_count": self.get_state(
                "last_fetch_new_article_count", 0
            ),
        }

    def list_recipients(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, email, active, created_at, updated_at FROM email_recipients ORDER BY created_at ASC"
            ).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "email": str(row["email"]),
                    "active": bool(row["active"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in rows
            ]

    def add_recipient(self, email: str, active: bool = True) -> dict[str, Any]:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("A valid email address is required")
        now = isoformat()
        rec_id = uuid.uuid4().hex
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO email_recipients (id, email, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (rec_id, email, 1 if active else 0, now, now),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Recipient with email '{email}' already exists")
        return {
            "id": rec_id,
            "email": email,
            "active": active,
            "created_at": now,
            "updated_at": now,
        }

    def update_recipient(
        self,
        recipient_id: str,
        email: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, active, created_at, updated_at FROM email_recipients WHERE id = ?",
                (recipient_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Recipient '{recipient_id}' not found")

            new_email = email.strip().lower() if email is not None else str(row["email"])
            if not new_email or "@" not in new_email:
                raise ValueError("A valid email address is required")

            new_active = (1 if active else 0) if active is not None else int(row["active"])
            now = isoformat()

            try:
                connection.execute(
                    """UPDATE email_recipients
                    SET email = ?, active = ?, updated_at = ?
                    WHERE id = ?""",
                    (new_email, new_active, now, recipient_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Recipient with email '{new_email}' already exists")

        return {
            "id": recipient_id,
            "email": new_email,
            "active": bool(new_active),
            "created_at": str(row["created_at"]),
            "updated_at": now,
        }

    def delete_recipient(self, recipient_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM email_recipients WHERE id = ?",
                (recipient_id,),
            )
            return cursor.rowcount > 0

    def get_active_recipient_emails(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT email FROM email_recipients WHERE active = 1 ORDER BY email ASC"
            ).fetchall()
            return [str(row["email"]) for row in rows]

    def seed_recipients_if_empty(self, default_recipients: tuple[str, ...] | list[str]) -> None:
        if not default_recipients:
            return
        with self.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM email_recipients").fetchone()["count"]
            if count == 0:
                now = isoformat()
                for rec in default_recipients:
                    email = rec.strip().lower()
                    if email and "@" in email:
                        connection.execute(
                            """INSERT OR IGNORE INTO email_recipients
                            (id, email, active, created_at, updated_at)
                            VALUES (?, ?, 1, ?, ?)""",
                            (uuid.uuid4().hex, email, now, now),
                        )

    def get_automation_schedule(self, default_timezone: str = "Asia/Kolkata") -> dict[str, Any]:
        default_schedule = {
            "enabled": True,
            "frequency": "weekly",
            "day_of_week": "mon",
            "hour": 9,
            "minute": 0,
            "timezone": default_timezone,
        }
        raw = self.get_state("automation_schedule")
        if raw and isinstance(raw, dict):
            default_schedule.update(raw)
        return default_schedule

    def save_automation_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(schedule.get("enabled", True))
        frequency = str(schedule.get("frequency", "weekly")).lower()
        if frequency not in {"daily", "weekly", "custom"}:
            raise ValueError(f"Invalid frequency '{frequency}'. Must be 'daily', 'weekly', or 'custom'.")

        day_of_week = str(schedule.get("day_of_week", "mon")).lower()[:3]
        valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if frequency == "weekly" and day_of_week not in valid_days:
            raise ValueError(f"Invalid day_of_week '{day_of_week}'. Must be one of {sorted(valid_days)}.")

        try:
            hour = int(schedule.get("hour", 9))
            if not (0 <= hour <= 23):
                raise ValueError("Hour must be between 0 and 23")
        except (ValueError, TypeError):
            raise ValueError("Hour must be an integer between 0 and 23")

        try:
            minute = int(schedule.get("minute", 0))
            if not (0 <= minute <= 59):
                raise ValueError("Minute must be between 0 and 59")
        except (ValueError, TypeError):
            raise ValueError("Minute must be an integer between 0 and 59")

        timezone = str(schedule.get("timezone", "Asia/Kolkata")).strip()
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(timezone)
        except Exception:
            raise ValueError(f"Invalid timezone '{timezone}'. Must be a valid IANA timezone like 'Asia/Kolkata'.")

        validated = {
            "enabled": enabled,
            "frequency": frequency,
            "day_of_week": day_of_week,
            "hour": hour,
            "minute": minute,
            "timezone": timezone,
        }
        if "cron" in schedule and schedule["cron"]:
            validated["cron"] = str(schedule["cron"]).strip()

        self.set_state("automation_schedule", validated)
        return validated
