from datetime import UTC, datetime

from app.extractor import ArticleExtractor
from app.models import SearchCandidate


def test_extracts_original_metadata_and_first_article_paragraph(test_settings):
    html = br"""
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


def test_uses_json_ld_article_body_not_seo_description(test_settings):
    html = br"""
    <html><head>
      <script type="application/ld+json">
      {"@type":"NewsArticle","headline":"Green Portfolio interview",
       "datePublished":"2026-09-07T09:30:00Z",
       "description":"SEO copy that must never be selected",
       "articleBody":"This is the first meaningful paragraph from the structured article body with original reporting and sufficient detail.\nThe second body paragraph is not needed."}
      </script>
    </head><body><main><div class="newsletter"><p>Subscribe to receive all our latest market coverage in your inbox every morning.</p></div></main></body></html>
    """
    extractor = ArticleExtractor(test_settings)
    extractor._safe_get = lambda _: (html, "https://publisher.example/structured")
    result = extractor.extract(
        SearchCandidate(
            url="https://publisher.example/structured",
            title="Provider title",
            published_at=datetime(2026, 9, 7, tzinfo=UTC),
            publisher="Publisher",
        )
    )

    assert result is not None
    assert result.description.startswith("This is the first meaningful paragraph")
    assert "SEO copy" not in result.description
    assert "second body" not in result.description


def test_readability_fallback_ignores_navigation_ads_and_related_content(test_settings):
    html = b"""
    <html><head>
      <meta property="og:title" content="Green Portfolio market outlook">
      <meta property="article:published_time" content="2026-09-07T09:30:00Z">
    </head><body>
      <div class="navigation"><p>This navigation paragraph contains many words but should never become article content for this record.</p></div>
      <main><div class="page"><div class="advertisement"><p>Advertisement content with enough words to otherwise resemble a meaningful paragraph in the page.</p></div>
      <section class="market-report-copy"><p>Green Portfolio expects disciplined stock selection to matter as valuations diverge across Indian equity market segments this year.</p>
      <p>The second genuine paragraph contains additional analysis.</p></section>
      <div class="related-articles"><p>Related coverage with enough text to look meaningful but it is not part of this article.</p></div></div></main>
    </body></html>
    """
    extractor = ArticleExtractor(test_settings)
    extractor._safe_get = lambda _: (html, "https://publisher.example/fallback")
    result = extractor.extract(
        SearchCandidate(
            url="https://publisher.example/fallback",
            title="Provider title",
            published_at=datetime(2026, 9, 7, tzinfo=UTC),
            publisher="Publisher",
        )
    )

    assert result is not None
    assert result.description.startswith("Green Portfolio expects disciplined")
