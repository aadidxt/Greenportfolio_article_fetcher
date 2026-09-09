from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.database import Database
from app.models import ArticleDraft
from app.pipeline import FetchPipeline
from app.sheets import GoogleSheetsStore, SHEET_HEADERS
from app.url_utils import article_id, normalize_url


class MutableWorksheet:
    def __init__(self, values=None):
        self.values = [list(row) for row in (values or [])]

    def get_all_values(self):
        return [list(row) for row in self.values]

    def append_row(self, row, **_):
        self.values.append(list(row))

    def append_rows(self, rows, **_):
        self.values.extend(list(row) for row in rows)

    def update_cell(self, row, column, value):
        while len(self.values) < row:
            self.values.append([])
        while len(self.values[row - 1]) < column:
            self.values[row - 1].append("")
        self.values[row - 1][column - 1] = value

    def delete_rows(self, row):
        self.values.pop(row - 1)


def draft(url="https://example.com/article", description="First real article paragraph with enough meaningful words for storage."):
    normalized = normalize_url(url)
    return ArticleDraft(
        publisher="Example Finance",
        published_at=datetime(2026, 9, 8, tzinfo=UTC),
        title="Green Portfolio discusses disciplined investing",
        description=description,
        url=normalized,
        normalized_url=normalized,
        source="test",
        canonical_url=normalized,
        original_url=url,
        normalized_title="green portfolio discusses disciplined investing",
    )


def configured_store(test_settings, worksheet):
    store = GoogleSheetsStore(replace(test_settings, google_sheets_enabled=True))
    store._worksheet = worksheet
    return store


def test_manual_sheet_deletion_creates_review_and_restore_is_duplicate_safe(test_settings):
    database = Database(test_settings.database_path)
    item = draft("https://example.com/article?utm_source=google")
    assert database.insert_article(article_id(item.normalized_url), item, datetime.now(UTC))
    worksheet = MutableWorksheet([])
    store = configured_store(test_settings, worksheet)

    first = store.reconcile(database)
    assert first["appended"] == 1
    assert len(worksheet.values) == 2

    second = store.reconcile(database)
    assert second["appended"] == 0
    assert len(worksheet.values) == 2

    worksheet.values.pop(1)
    missing = store.reconcile(database)
    assert missing["database_only"] == 1
    assert missing["appended"] == 0
    assert len(database.all_articles()) == 1
    review = database.list_sync_reviews()[0]
    assert review["review_type"] == "database_only"

    restored = store.restore_review(database, review["id"])
    assert restored["pending_reviews"] == 0
    assert len(worksheet.values) == 2
    assert store.reconcile(database)["appended"] == 0
    assert len(worksheet.values) == 2


def test_confirmed_sheet_removal_is_not_readded(test_settings):
    database = Database(test_settings.database_path)
    item = draft()
    database.insert_article(article_id(item.normalized_url), item, datetime.now(UTC))
    worksheet = MutableWorksheet([])
    store = configured_store(test_settings, worksheet)
    store.reconcile(database)
    worksheet.values.pop(1)
    store.reconcile(database)
    review = database.list_sync_reviews()[0]

    store.confirm_removal(database, review["id"])
    again = store.reconcile(database)
    assert again["appended"] == 0
    assert again["database_only"] == 0
    assert database.all_articles()[0].sheet_status == "intentionally_removed"
    assert len(worksheet.values) == 1


def test_sheet_only_import_and_duplicate_consolidation(test_settings):
    database = Database(test_settings.database_path)
    url = "https://example.com/sheet-only?utm_campaign=test"
    row = ["Example Finance", "2026-09-08", "Green Portfolio sheet story", "Body paragraph", url]
    worksheet = MutableWorksheet([SHEET_HEADERS, row])
    store = configured_store(test_settings, worksheet)

    summary = store.reconcile(database)
    assert summary["sheet_only"] == 1
    review = database.list_sync_reviews()[0]
    imported = draft(url)
    store.import_review(database, review["id"], imported)
    assert len(database.all_articles()) == 1
    assert database.list_sync_reviews() == []

    worksheet.values.append(list(row))
    duplicate_summary = store.reconcile(database)
    assert duplicate_summary["duplicates"] == 1
    duplicate_review = database.list_sync_reviews()[0]
    assert duplicate_review["review_type"] == "duplicate_sheet_rows"
    store.consolidate_duplicate_review(database, duplicate_review["id"])
    assert len(worksheet.values) == 2
    assert database.list_sync_reviews() == []


def test_reconciliation_fills_blank_sheet_description_from_database(test_settings):
    database = Database(test_settings.database_path)
    item = draft()
    database.insert_article(article_id(item.normalized_url), item, datetime.now(UTC))
    database.mark_synced([article_id(item.normalized_url)])
    worksheet = MutableWorksheet(
        [SHEET_HEADERS, [item.publisher, "2026-09-08", item.title, "", item.url]]
    )
    store = configured_store(test_settings, worksheet)

    store.reconcile(database)
    assert worksheet.values[1][3] == item.description


def test_title_publisher_date_fallback_records_permanent_url_alias(test_settings):
    database = Database(test_settings.database_path)
    item = draft("https://publisher.example/canonical-story")
    identifier = article_id(item.normalized_url)
    database.insert_article(identifier, item, datetime.now(UTC))
    database.mark_synced([identifier])
    alternate = "https://syndication.example/different-path"
    worksheet = MutableWorksheet(
        [SHEET_HEADERS, [item.publisher, "2026-09-08", item.title, item.description, alternate]]
    )
    store = configured_store(test_settings, worksheet)

    summary = store.reconcile(database)
    assert summary["matching_articles"] == 1
    assert summary["database_only"] == 0
    assert summary["sheet_only"] == 0
    assert database.find_by_normalized_url(normalize_url(alternate)).id == identifier


def test_backfill_updates_existing_database_and_sheet_row_without_duplicate(test_settings):
    database = Database(test_settings.database_path)
    blank = draft(description="")
    identifier = article_id(blank.normalized_url)
    database.insert_article(identifier, blank, datetime.now(UTC))
    database.mark_synced([identifier])
    worksheet = MutableWorksheet(
        [SHEET_HEADERS, [blank.publisher, "2026-09-08", blank.title, "", blank.url]]
    )
    store = configured_store(test_settings, worksheet)
    extracted = draft()

    class Extractor:
        def extract(self, _candidate):
            return extracted

    class Search:
        def search(self, _since, _until):
            return []

    class Email:
        def send(self, **_):
            return None

    pipeline = FetchPipeline(
        settings=test_settings,
        database=database,
        search_service=Search(),
        extractor=Extractor(),
        sheets=store,
        emailer=Email(),
    )
    result = pipeline.backfill_descriptions()

    assert result["updated"] == 1
    assert len(database.all_articles()) == 1
    assert database.all_articles()[0].description == extracted.description
    assert worksheet.values[1][3] == extracted.description
