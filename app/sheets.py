from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings
from .models import ArticleDraft, ArticleRecord
from .url_utils import article_id, normalize_url

logger = logging.getLogger(__name__)

SHEET_HEADERS = ["Publisher Website", "Published Date", "Title", "Description", "URL"]


class GoogleSheetsStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._worksheet: Any = None

    @property
    def enabled(self) -> bool:
        return self.settings.google_sheets_enabled

    def _credentials(self) -> dict[str, Any]:
        raw = self.settings.google_service_account_json.strip()
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")

        # 1. If it starts with '{', it is raw JSON
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Google service-account credentials are invalid JSON") from exc
            if not isinstance(value, dict) or value.get("type") != "service_account":
                raise RuntimeError("A Google service-account credential is required")
            return value

        # 2. Check if it is a file path (only short strings without newlines)
        if len(raw) < 500 and "\n" not in raw:
            try:
                potential_path = Path(raw)
                render_secret_path = Path("/etc/secrets") / potential_path.name
                if potential_path.is_file():
                    raw = potential_path.read_text(encoding="utf-8").strip()
                elif render_secret_path.is_file():
                    raw = render_secret_path.read_text(encoding="utf-8").strip()
                elif raw.endswith(".json") or "/" in raw or "\\" in raw:
                    raise RuntimeError(
                        f"Google service-account file '{raw}' was not found on the server filesystem. "
                        "Please paste the base64 string directly into GOOGLE_SERVICE_ACCOUNT_JSON."
                    )
            except OSError:
                pass

        # If a file was read and is now raw JSON
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Google service-account credentials are invalid JSON") from exc
            if not isinstance(value, dict) or value.get("type") != "service_account":
                raise RuntimeError("A Google service-account credential is required")
            return value

        # 3. Otherwise, it must be base64 encoded
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
            value = json.loads(decoded)
        except Exception as exc:
            raise RuntimeError(
                "Google service-account JSON is not a valid file path, JSON string, or base64"
            ) from exc

        if not isinstance(value, dict) or value.get("type") != "service_account":
            raise RuntimeError("A Google service-account credential is required")
        return value

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _get_worksheet(self):
        if self._worksheet is not None:
            return self._worksheet
        if not self.enabled:
            return None
        import gspread

        client = gspread.service_account_from_dict(self._credentials())
        sheet_ref = self.settings.google_sheet_id.strip()
        if sheet_ref.startswith("http://") or sheet_ref.startswith("https://"):
            spreadsheet = client.open_by_url(sheet_ref)
        else:
            spreadsheet = client.open_by_key(sheet_ref)
        if self.settings.google_worksheet_name:
            self._worksheet = spreadsheet.worksheet(self.settings.google_worksheet_name)
        else:
            self._worksheet = spreadsheet.get_worksheet(0)
        if self._worksheet is None:
            raise RuntimeError("The Google Sheet has no first worksheet")
        return self._worksheet

    def sync_database(self, database: Database) -> dict[str, Any]:
        return self.reconcile(database)

    def _read_values(self) -> tuple[Any, list[list[str]], bool]:
        worksheet = self._get_worksheet()
        values = worksheet.get_all_values()
        is_empty = not values or all(
            not any(str(cell).strip() for cell in row) for row in values
        )
        if is_empty:
            worksheet.append_row(SHEET_HEADERS, value_input_option="RAW")
            values = [SHEET_HEADERS]
        else:
            actual_headers = [str(c).strip() for c in values[0][: len(SHEET_HEADERS)]]
            if actual_headers != SHEET_HEADERS:
                if not any(actual_headers):
                    worksheet.update(values=[SHEET_HEADERS], range_name="A1:E1")
                    values[0] = SHEET_HEADERS
                else:
                    raise RuntimeError(
                        "Google Sheet headers do not match the required schema"
                    )
        return worksheet, values, is_empty

    @staticmethod
    def _row_data(row: list[str], row_number: int) -> dict[str, Any]:
        padded = [str(value).strip() for value in row[:5]] + [""] * max(0, 5 - len(row))
        raw_url = padded[4]
        try:
            normalized = normalize_url(raw_url) if raw_url else ""
        except ValueError:
            normalized = ""
        return {
            "publisher": padded[0],
            "published_date": padded[1],
            "title": padded[2],
            "description": padded[3],
            "url": raw_url,
            "normalized_url": normalized,
            "row": row_number,
        }

    @staticmethod
    def _review_token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _fallback_key(publisher: str, title: str, published_date: str) -> str:
        return "|".join(
            (
                " ".join(title.casefold().split()),
                " ".join(publisher.casefold().split()),
                published_date[:10],
            )
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def reconcile(self, database: Database) -> dict[str, Any]:
        """Compare both stores, append only never-synced rows, and flag drift."""
        if not self.enabled:
            result = {
                "status": "disabled",
                "database_articles": len(database.all_articles()),
                "sheet_articles": 0,
                "matching_articles": 0,
                "database_only": 0,
                "sheet_only": 0,
                "duplicates": 0,
                "pending_reviews": len(database.list_sync_reviews()),
                "appended": 0,
            }
            database.set_state("sheet_sync_summary", result)
            return result

        worksheet, values, is_empty = self._read_values()
        rows = [
            self._row_data(list(row), number)
            for number, row in enumerate(values[1:], start=2)
            if any(str(cell).strip() for cell in row)
        ]
        sheet_by_url: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = row["normalized_url"]
            if key:
                sheet_by_url.setdefault(key, []).append(row)

        duplicate_groups = 0
        for normalized, matches in sheet_by_url.items():
            if len(matches) <= 1:
                database.resolve_review_key(f"duplicate_sheet:{normalized}")
                continue
            duplicate_groups += 1
            database.create_sync_review(
                review_key=f"duplicate_sheet:{normalized}",
                review_type="duplicate_sheet_rows",
                normalized_url=normalized,
                sheet_rows=[int(item["row"]) for item in matches],
                sheet_data={"rows": matches},
            )

        articles = database.all_articles()
        db_by_url = {article.normalized_url: article for article in articles}
        articles_by_id = {article.id: article for article in articles}
        aliases = database.url_aliases()
        for alias, existing_id in aliases.items():
            if existing_id in articles_by_id:
                db_by_url[alias] = articles_by_id[existing_id]
        urls_by_article: dict[str, set[str]] = {
            article.id: {article.normalized_url} for article in articles
        }
        for alias, existing_id in aliases.items():
            urls_by_article.setdefault(existing_id, set()).add(alias)
        sheet_by_fallback: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["publisher"] and row["title"] and row["published_date"]:
                key = self._fallback_key(
                    row["publisher"], row["title"], row["published_date"]
                )
                sheet_by_fallback.setdefault(key, []).append(row)
        db_by_fallback: dict[str, list[ArticleRecord]] = {}
        for article in articles:
            key = self._fallback_key(
                article.publisher,
                article.title,
                article.published_at.date().isoformat(),
            )
            db_by_fallback.setdefault(key, []).append(article)
        matching_ids: list[str] = []
        matched_sheet_rows: set[int] = set()
        appended_ids: list[str] = []
        rows_to_append: list[list[str]] = []
        database_only = 0

        for article in articles:
            matches_by_row: dict[int, dict[str, Any]] = {}
            for known_url in urls_by_article.get(article.id, {article.normalized_url}):
                for match in sheet_by_url.get(known_url, []):
                    matches_by_row[int(match["row"])] = match
            if not matches_by_row:
                fallback = self._fallback_key(
                    article.publisher,
                    article.title,
                    article.published_at.date().isoformat(),
                )
                if (
                    len(db_by_fallback.get(fallback, [])) == 1
                    and len(sheet_by_fallback.get(fallback, [])) == 1
                ):
                    match = sheet_by_fallback[fallback][0]
                    matches_by_row[int(match["row"])] = match
                    if match["normalized_url"]:
                        database.add_url_alias(article.id, match["normalized_url"])
            matches = list(matches_by_row.values())
            if matches:
                matching_ids.append(article.id)
                matched_sheet_rows.update(int(match["row"]) for match in matches)
                database.resolve_review_key(f"database_only:{article.id}")
                # The database is authoritative for verified article-body text;
                # keep the corresponding Sheet description identical.
                first_row = matches[0]
                if article.description and first_row["description"] != article.description:
                    self._update_cell(worksheet, int(first_row["row"]), 4, article.description)
                continue

            if article.sheet_status == "intentionally_removed":
                database.resolve_review_key(
                    f"database_only:{article.id}", "intentionally_removed"
                )
                continue
            if not article.synced_to_sheet or article.sheet_status == "pending":
                rows_to_append.append(self._article_row(article))
                appended_ids.append(article.id)
                continue

            database_only += 1
            database.set_sheet_status(article.id, "missing_from_sheet")
            database.create_sync_review(
                review_key=f"database_only:{article.id}",
                review_type="database_only",
                article_id=article.id,
                normalized_url=article.normalized_url,
                sheet_data={
                    "publisher": article.publisher,
                    "published_date": article.published_at.date().isoformat(),
                    "title": article.title,
                    "description": article.description,
                    "url": article.url,
                },
            )

        if rows_to_append:
            worksheet.append_rows(
                rows_to_append, value_input_option="RAW", insert_data_option="INSERT_ROWS"
            )
            database.mark_synced(appended_ids)

        if matching_ids:
            database.mark_synced(matching_ids)

        sheet_only = 0
        for row in rows:
            normalized = row["normalized_url"]
            if int(row["row"]) in matched_sheet_rows or (normalized and normalized in db_by_url):
                database.resolve_review_key(f"sheet_only:{normalized}")
                continue
            sheet_only += 1
            identity = normalized or self._review_token(
                f"{row['row']}|{row['url']}|{row['title']}"
            )
            database.create_sync_review(
                review_key=f"sheet_only:{identity}",
                review_type="sheet_only",
                normalized_url=normalized,
                sheet_rows=[int(row["row"])],
                sheet_data=row,
            )

        pending_reviews = len(database.list_sync_reviews())
        result = {
            "status": "success",
            "is_empty": is_empty,
            "database_articles": len(articles),
            "sheet_articles": len(rows) + len(rows_to_append),
            "matching_articles": len(matching_ids) + len(appended_ids),
            "database_only": database_only,
            "sheet_only": sheet_only,
            "duplicates": duplicate_groups,
            "pending_reviews": pending_reviews,
            "appended": len(rows_to_append),
            "synchronized_ids": matching_ids + appended_ids,
        }
        database.set_state("sheet_sync_summary", result)
        logger.info(
            "Sheet reconciliation: %d matching, %d database-only, %d sheet-only, %d duplicate groups",
            result["matching_articles"], database_only, sheet_only, duplicate_groups,
        )
        return result

    @staticmethod
    def _article_row(article: ArticleRecord) -> list[str]:
        return [
            article.publisher,
            article.published_at.date().isoformat(),
            article.title,
            article.description,
            article.url,
        ]

    @staticmethod
    def _update_cell(worksheet: Any, row: int, column: int, value: str) -> None:
        if hasattr(worksheet, "update_cell"):
            worksheet.update_cell(row, column, value)
        else:
            letter = "ABCDE"[column - 1]
            worksheet.update(values=[[value]], range_name=f"{letter}{row}")

    def restore_review(self, database: Database, review_id: str) -> dict[str, Any]:
        review = database.get_sync_review(review_id)
        if not review or review["status"] != "pending" or review["review_type"] != "database_only":
            raise KeyError("Pending database-only review not found")
        article = database.get_article(str(review["article_id"]))
        if article is None:
            raise KeyError("Article no longer exists in the database")
        worksheet, values, _ = self._read_values()
        existing = {
            self._row_data(list(row), number)["normalized_url"]
            for number, row in enumerate(values[1:], start=2)
        }
        if article.normalized_url not in existing:
            worksheet.append_rows(
                [self._article_row(article)],
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
            )
        database.mark_synced([article.id])
        database.resolve_sync_review(review_id, "restored_to_sheet")
        return self.reconcile(database)

    def confirm_removal(self, database: Database, review_id: str) -> dict[str, Any]:
        review = database.get_sync_review(review_id)
        if not review or review["status"] != "pending" or review["review_type"] != "database_only":
            raise KeyError("Pending database-only review not found")
        database.set_sheet_status(str(review["article_id"]), "intentionally_removed")
        database.resolve_sync_review(review_id, "intentionally_removed")
        return database.data_sync_status()

    def import_review(
        self, database: Database, review_id: str, draft: ArticleDraft
    ) -> dict[str, Any]:
        review = database.get_sync_review(review_id)
        if not review or review["status"] != "pending" or review["review_type"] != "sheet_only":
            raise KeyError("Pending sheet-only review not found")
        duplicate_id = database.find_duplicate(draft)
        if duplicate_id is None:
            inserted = database.insert_article(
                article_id(draft.normalized_url), draft, datetime.now().astimezone()
            )
            if not inserted:
                duplicate_id = article_id(draft.normalized_url)
        else:
            database.add_url_alias(duplicate_id, draft.normalized_url)
        article = database.find_by_normalized_url(draft.normalized_url)
        if article:
            database.mark_synced([article.id])
            if not article.description.strip():
                database.record_extraction_failure(
                    article.id,
                    "Imported after review, but no article-body paragraph could be verified",
                )
        database.resolve_sync_review(review_id, "imported_to_database")
        return self.reconcile(database)

    def consolidate_duplicate_review(
        self, database: Database, review_id: str
    ) -> dict[str, Any]:
        review = database.get_sync_review(review_id)
        if not review or review["status"] != "pending" or review["review_type"] != "duplicate_sheet_rows":
            raise KeyError("Pending duplicate review not found")
        row_numbers = sorted({int(value) for value in review["sheet_rows"]})
        if len(row_numbers) < 2:
            database.resolve_sync_review(review_id, "already_consolidated")
            return self.reconcile(database)
        worksheet, values, _ = self._read_values()
        keeper = row_numbers[0]
        source_rows = [values[number - 1] for number in row_numbers if number - 1 < len(values)]
        for column in range(1, 6):
            current = str(values[keeper - 1][column - 1]).strip() if len(values[keeper - 1]) >= column else ""
            if current:
                continue
            replacement = next(
                (str(row[column - 1]).strip() for row in source_rows if len(row) >= column and str(row[column - 1]).strip()),
                "",
            )
            if replacement:
                self._update_cell(worksheet, keeper, column, replacement)
        for row_number in reversed(row_numbers[1:]):
            worksheet.delete_rows(row_number)
        database.resolve_sync_review(review_id, "duplicate_rows_consolidated")
        return self.reconcile(database)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def sync_articles(
        self, articles: list[ArticleRecord], database: Database | None = None
    ) -> dict[str, Any]:
        if not self.enabled:
            logger.warning("Google Sheets is disabled; retaining articles in local persistent storage")
            return {"status": "disabled", "appended": 0, "total": len(articles), "synchronized_ids": []}

        worksheet = self._get_worksheet()
        values = worksheet.get_all_values()

        # Check if the Google Sheet is completely empty or only contains whitespace
        is_empty = (
            not values
            or all(not any(str(cell).strip() for cell in row) for row in values)
        )

        if is_empty:
            logger.info("Google Sheet is empty. Adding headers and initializing.")
            worksheet.clear()
            worksheet.append_row(SHEET_HEADERS, value_input_option="RAW")
            existing_urls: set[str] = set()
        else:
            actual_headers = [str(c).strip() for c in values[0][: len(SHEET_HEADERS)]]
            if actual_headers != SHEET_HEADERS:
                if not any(actual_headers):
                    worksheet.update(values=[SHEET_HEADERS], range_name="A1:E1")
                else:
                    raise RuntimeError(
                        "Google Sheet headers do not match the required append-only schema"
                    )

            existing_urls = set()
            for row in values[1:]:
                if len(row) > 4 and str(row[4]).strip():
                    try:
                        existing_urls.add(normalize_url(str(row[4])))
                    except ValueError:
                        continue

        # Opening the worksheet and validating its schema are required even for
        # a zero-result fetch; otherwise a misconfigured Sheet could be reported
        # as successfully synchronized.
        if not articles:
            return {
                "status": "success",
                "is_empty": is_empty,
                "appended": 0,
                "total": 0,
                "synchronized_ids": [],
            }

        rows_to_append: list[list[str]] = []
        synchronized_ids: list[str] = []

        for article in articles:
            synchronized_ids.append(article.id)
            if article.normalized_url in existing_urls:
                continue
            rows_to_append.append(
                [
                    article.publisher,
                    article.published_at.date().isoformat(),
                    article.title,
                    article.description,
                    article.url,
                ]
            )
            existing_urls.add(article.normalized_url)

        if rows_to_append:
            worksheet.append_rows(
                rows_to_append, value_input_option="RAW", insert_data_option="INSERT_ROWS"
            )
            logger.info("Appended %d article(s) to Google Sheet", len(rows_to_append))
        else:
            logger.info("Google Sheet is already up to date. No new rows added.")

        if database is not None and synchronized_ids:
            database.mark_synced(synchronized_ids)

        return {
            "status": "success",
            "is_empty": is_empty,
            "appended": len(rows_to_append),
            "total": len(articles),
            "synchronized_ids": synchronized_ids,
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def append_articles(self, articles: list[ArticleRecord]) -> list[str]:
        result = self.sync_articles(articles)
        return result.get("synchronized_ids", [])

    def configuration_status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "configured": True, "message": "Local-only mode"}
        configured = bool(
            self.settings.google_sheet_id and self.settings.google_service_account_json
        )
        return {
            "enabled": True,
            "configured": configured,
            "message": "Ready" if configured else "Service-account credentials required",
        }
