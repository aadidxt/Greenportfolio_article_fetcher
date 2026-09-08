from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.search import InternetSearchService


def test_google_news_rss_legacy_flag_does_not_emit_wrapper_urls(test_settings):
    from dataclasses import replace

    service = InternetSearchService(
        replace(test_settings, google_news_rss_enabled=True, bing_news_rss_enabled=False)
    )
    with pytest.raises(RuntimeError, match="No direct-URL search provider") as error:
        service.search(
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 8, tzinfo=UTC),
        )
    assert "robots-blocked wrapper URLs" in str(error.value)


def test_bing_news_rss_provider_registered(test_settings):
    from dataclasses import replace

    settings = replace(test_settings, bing_news_rss_enabled=True)
    assert "Bing News RSS" in settings.search_provider_names


def test_bing_news_rss_parses_sample_xml(test_settings):
    from dataclasses import replace

    sample_xml = Path("bing-sample.xml").read_bytes()
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.content = sample_xml
    mock_response.raise_for_status.return_value = None
    mock_session.get.return_value = mock_response

    service = InternetSearchService(
        replace(test_settings, bing_news_rss_enabled=True),
        session=mock_session,
    )
    candidates = service.search(
        since=datetime(2020, 1, 1, tzinfo=UTC),
        until=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert len(candidates) > 0
    # First item from bing-sample.xml
    whalesbook = next(c for c in candidates if "whalesbook" in c.url)
    assert whalesbook.url.startswith("https://www.whalesbook.com/")
    assert whalesbook.publisher == "whalesbook"
    assert whalesbook.source == "Bing News RSS"

