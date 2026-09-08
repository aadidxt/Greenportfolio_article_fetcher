from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings
from .models import ArticleRecord
from .url_utils import normalize_url

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
        
        potential_path = Path(raw)
        if not raw.startswith("{") and potential_path.is_file():
            raw = potential_path.read_text(encoding="utf-8").strip()
        elif not raw.startswith("{"):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except Exception as exc:
                raise RuntimeError("Google service-account JSON is not a valid file path, JSON string, or base64") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Google service-account credentials are invalid JSON") from exc
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
        """Check if the Google Sheet is empty; if so, write headers and all articles.
        Otherwise, append only new articles from the database without adding duplicate rows.
        """
        articles = database.all_articles()
        return self.sync_articles(articles, database=database)

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
