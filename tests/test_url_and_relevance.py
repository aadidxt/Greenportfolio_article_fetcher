from datetime import UTC, datetime

import pytest

from app.models import ArticleDraft
from app.relevance import is_relevant
from app.url_utils import normalize_url


def article(title: str, description: str = "") -> ArticleDraft:
    return ArticleDraft(
        publisher="Business Daily",
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
        title=title,
        description=description,
        url="https://example.com/story",
        normalized_url="https://example.com/story",
    )


def test_url_normalization_removes_tracking_fragments_and_variants():
    assert normalize_url(
        "http://WWW.Example.com/story/?utm_source=x&b=2&a=1#section"
    ) == "https://example.com/story?a=1&b=2"


def test_invalid_urls_are_rejected():
    with pytest.raises(ValueError):
        normalize_url("javascript:alert(1)")


def test_relevance_requires_context_for_common_name():
    assert is_relevant(article("Divam Sharma discusses equity markets"))
    assert is_relevant(article("Green Portfolio expands its investment team"))
    assert not is_relevant(article("Anuj Jain wins a neighborhood tennis match"))
    assert not is_relevant(article("A guide to balanced portfolios"))

