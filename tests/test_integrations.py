from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.emailer import WeeklyEmailSender
from app.models import ArticleRecord
from app.sheets import GoogleSheetsStore, SHEET_HEADERS


def record(identifier: str, url: str, title: str) -> ArticleRecord:
    return ArticleRecord(
        id=identifier,
        publisher="Finance Journal",
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
        title=title,
        description="The original first paragraph.",
        url=url,
        normalized_url=url,
        source="test",
    )


class FakeWorksheet:
    def __init__(self, values):
        self.values = values
        self.appended_rows = []
        self.headers_appended = []

    def get_all_values(self):
        return self.values

    def append_row(self, row, **_):
        self.headers_appended.append(row)

    def append_rows(self, rows, **_):
        self.appended_rows.extend(rows)

    def clear(self):
        self.values = []


def test_google_sheets_sync_only_appends_missing_urls(test_settings):
    existing_url = "https://example.com/already-there"
    worksheet = FakeWorksheet(
        [SHEET_HEADERS, ["Finance Journal", "2026-09-07", "Existing", "Text", existing_url]]
    )
    store = GoogleSheetsStore(replace(test_settings, google_sheets_enabled=True))
    store._worksheet = worksheet
    existing = record("one", existing_url, "Existing")
    new = record("two", "https://example.com/new-story", "New story")

    synchronized = store.append_articles([existing, new])

    assert synchronized == ["one", "two"]
    assert len(worksheet.appended_rows) == 1
    assert worksheet.appended_rows[0][2] == "New story"
    assert worksheet.values[1][2] == "Existing"


def test_google_sheets_sync_empty_sheet_and_duplicates(test_settings):
    worksheet = FakeWorksheet([])
    store = GoogleSheetsStore(replace(test_settings, google_sheets_enabled=True))
    store._worksheet = worksheet

    a1 = record("1", "https://example.com/one", "Story One")
    a2 = record("2", "https://example.com/two", "Story Two")

    res = store.sync_articles([a1, a2])
    assert res["status"] == "success"
    assert res["is_empty"] is True
    assert res["appended"] == 2
    assert worksheet.headers_appended == [SHEET_HEADERS]
    assert len(worksheet.appended_rows) == 2

    worksheet.values = [SHEET_HEADERS] + list(worksheet.appended_rows)
    worksheet.appended_rows = []

    a3 = record("3", "https://example.com/three", "Story Three")
    res2 = store.sync_articles([a1, a2, a3])
    assert res2["status"] == "success"
    assert res2["is_empty"] is False
    assert res2["appended"] == 1
    assert len(worksheet.appended_rows) == 1
    assert worksheet.appended_rows[0][4] == "https://example.com/three"


def test_google_sheets_status_exposes_safe_browser_url(test_settings):
    store = GoogleSheetsStore(
        replace(
            test_settings,
            google_sheets_enabled=True,
            google_sheet_id="https://docs.google.com/spreadsheets/d/example-sheet_123/edit#gid=0",
            google_service_account_json="credentials-present",
        )
    )

    assert store.configuration_status()["url"] == (
        "https://docs.google.com/spreadsheets/d/example-sheet_123/edit"
    )


class FakeSMTP:
    sent_message = None

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *_):
        pass

    def send_message(self, message):
        FakeSMTP.sent_message = message


def test_weekly_email_contains_both_xlsx_attachments(test_settings, monkeypatch):
    configured = replace(
        test_settings,
        email_enabled=True,
        email_recipients=("media@example.com",),
        smtp_host="smtp.example.com",
        smtp_from="monitor@example.com",
        smtp_username="user",
        smtp_password="secret",
    )
    monkeypatch.setattr("app.emailer.smtplib.SMTP", FakeSMTP)
    WeeklyEmailSender(configured).send(
        new_xlsx=b"PK-new",
        all_xlsx=b"PK-all",
        new_count=4,
        fetched_at=datetime(2026, 9, 14, 3, 30, tzinfo=UTC),
    )

    message = FakeSMTP.sent_message
    assert message is not None
    filenames = [part.get_filename() for part in message.iter_attachments()]
    assert filenames == [
        "new_articles_2026-09-14.xlsx",
        "green_portfolio_all_articles_2026-09-14.xlsx",
    ]
    assert "New articles found: 4" in message.get_body(preferencelist=("plain",)).get_content()
