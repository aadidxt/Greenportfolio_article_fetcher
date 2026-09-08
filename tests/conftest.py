from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.from_env(),
        app_env="test",
        database_path=tmp_path / "monitor.db",
        scheduler_enabled=False,
        bing_news_rss_enabled=False,
        google_news_rss_enabled=False,
        google_sheets_enabled=False,
        email_enabled=False,
        resend_api_key="",
        admin_api_key="test-admin-key",
    )

