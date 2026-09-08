from datetime import UTC, datetime

from app.extractor import ArticleExtractor
from app.models import SearchCandidate


def test_extracts_original_metadata_and_first_article_paragraph(test_settings):
    html = b"""
    <html><head>
      <link rel="canonical" href="https://publisher.example/story?utm_source=news">
      <meta property="og:site_name" content="Finance Journal">
      <meta property="og:title" content="Green Portfolio speaks on small caps">
      <meta property="article:published_time" content="2026-09-07T09:30:00+05:30">
    </head><body><nav><p>Navigation copy that must not be selected.</p></nav>
      <article><h1>Fallback heading</h1>
      <p>Green Portfolio said disciplined research remains essential as Indian small-cap valuations move through a volatile phase.</p>
      <p>This is the second paragraph and must not be exported.</p></article>
    </body></html>
    """
    extractor = ArticleExtractor(test_settings)
    extractor._safe_get = lambda _: (html, "https://publisher.example/story")
    result = extractor.extract(
        SearchCandidate(
            url="https://publisher.example/story",
            title="Search title",
            published_at=datetime(2026, 9, 7, tzinfo=UTC),
            publisher="Publisher",
        )
    )

    assert result is not None
    assert result.title == "Green Portfolio speaks on small caps"
    assert result.publisher == "Finance Journal"
    assert result.description.startswith("Green Portfolio said disciplined research")
    assert "second paragraph" not in result.description
    assert result.normalized_url == "https://publisher.example/story"
    assert result.published_at.isoformat() == "2026-09-07T04:00:00+00:00"

