from __future__ import annotations

import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import requests
from dateutil.parser import parse as parse_date

from .config import Settings
from .models import SearchCandidate
from .url_utils import normalize_url

logger = logging.getLogger(__name__)

SEARCH_QUERIES = (
    '"Green Portfolio"',
    '"Green Portfolio" "Divam Sharma"',
    '"Green Portfolio" "Anuj Jain"',
    '"Divam Sharma" investment',
    '"Anuj Jain" "Green Portfolio" OR investment',
)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parse_date(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


class InternetSearchService:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "GreenPortfolioMediaMonitor/1.0 (+media-monitor)"}
        )

    def search(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        providers = []
        if self.settings.bing_news_rss_enabled:
            providers.append(self._bing_news_rss)
        if self.settings.news_api_key:
            providers.append(self._news_api)
        if self.settings.gnews_api_key:
            providers.append(self._gnews_api)
        if self.settings.serper_api_key:
            providers.append(self._serper)
        if self.settings.google_cse_api_key and self.settings.google_cse_id:
            providers.append(self._google_cse)
        if not providers:
            legacy_note = (
                " GOOGLE_NEWS_RSS_ENABLED is ignored because Google News supplies "
                "robots-blocked wrapper URLs rather than canonical publisher URLs."
                if self.settings.google_news_rss_enabled
                else ""
            )
            raise RuntimeError(
                "No direct-URL search provider is configured. Enable BING_NEWS_RSS_ENABLED "
                "or set NEWS_API_KEY, GNEWS_API_KEY, SERPER_API_KEY, or Google CSE credentials."
                + legacy_note
            )

        candidates: list[SearchCandidate] = []
        provider_errors: list[str] = []
        for provider in providers:
            try:
                candidates.extend(provider(since, until))
            except Exception as exc:  # a provider outage should not stop other sources
                logger.exception("Search provider %s failed", provider.__name__)
                provider_errors.append(f"{provider.__name__}: {exc}")
        if not candidates and provider_errors:
            raise RuntimeError("All configured search providers failed: " + "; ".join(provider_errors))
        return self._deduplicate_candidates(candidates)[: self.settings.max_articles_per_fetch]

    def _bing_news_rss(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        results: list[SearchCandidate] = []
        for query in SEARCH_QUERIES:
            try:
                response = self.session.get(
                    "https://www.bing.com/news/search",
                    params={"q": query, "format": "rss"},
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
                root = ET.fromstring(response.content)
            except Exception as exc:
                logger.warning("Bing News RSS query %r failed: %s", query, exc)
                continue

            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not title or not link:
                    continue

                # Bing RSS links wrap the direct canonical URL in a 'url' query parameter
                try:
                    parsed_link = urllib.parse.urlparse(link)
                    qs = urllib.parse.parse_qs(parsed_link.query)
                    target_url = qs.get("url", [None])[0]
                    canonical_url = urllib.parse.unquote(target_url) if target_url else link
                except Exception:
                    canonical_url = link

                published = _parse_date(item.findtext("pubDate"))
                if published and published < since:
                    continue

                source_elem = item.find("{*}Source")
                if source_elem is None:
                    source_elem = item.find("source")
                publisher = (source_elem.text if source_elem is not None and source_elem.text else "").strip()
                if not publisher:
                    try:
                        publisher = urllib.parse.urlparse(canonical_url).netloc.replace("www.", "")
                    except Exception:
                        publisher = "BingNews"

                results.append(
                    SearchCandidate(
                        url=canonical_url,
                        title=title,
                        published_at=published,
                        publisher=publisher,
                        source="Bing News RSS",
                    )
                )
        return results

    def _news_api(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        response = self.session.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": '"Green Portfolio" OR "Divam Sharma" OR "Anuj Jain"',
                "from": since.isoformat(),
                "to": until.isoformat(),
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 100,
                "apiKey": self.settings.news_api_key,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return [
            SearchCandidate(
                item.get("url", ""),
                item.get("title", ""),
                _parse_date(item.get("publishedAt")),
                (item.get("source") or {}).get("name", ""),
                "NewsAPI",
            )
            for item in response.json().get("articles", [])
            if item.get("url") and item.get("title")
        ]

    def _gnews_api(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        results: list[SearchCandidate] = []
        for query in SEARCH_QUERIES[:3]:
            response = self.session.get(
                "https://gnews.io/api/v4/search",
                params={
                    "q": query,
                    "from": since.isoformat(),
                    "to": until.isoformat(),
                    "lang": "en",
                    "country": "in",
                    "max": 100,
                    "apikey": self.settings.gnews_api_key,
                },
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            for item in response.json().get("articles", []):
                source = item.get("source") or {}
                results.append(
                    SearchCandidate(
                        item.get("url", ""),
                        item.get("title", ""),
                        _parse_date(item.get("publishedAt")),
                        source.get("name", ""),
                        "GNews",
                    )
                )
        return [item for item in results if item.url and item.title]

    def _serper(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        results: list[SearchCandidate] = []
        headers = {"X-API-KEY": self.settings.serper_api_key, "Content-Type": "application/json"}
        for query in SEARCH_QUERIES:
            response = self.session.post(
                "https://google.serper.dev/news",
                headers=headers,
                json={"q": query, "gl": "in", "hl": "en", "num": 20},
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            for item in response.json().get("news", []):
                published = _parse_date(item.get("date"))
                if published and published < since:
                    continue
                results.append(
                    SearchCandidate(
                        item.get("link", ""),
                        item.get("title", ""),
                        published,
                        item.get("source", ""),
                        "Serper",
                    )
                )
        return [item for item in results if item.url and item.title]

    def _google_cse(self, since: datetime, until: datetime) -> list[SearchCandidate]:
        results: list[SearchCandidate] = []
        days = max(1, (until.date() - since.date()).days + 1)
        for query in SEARCH_QUERIES:
            response = self.session.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": self.settings.google_cse_api_key,
                    "cx": self.settings.google_cse_id,
                    "q": query,
                    "dateRestrict": f"d{days}",
                    "num": 10,
                },
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            for item in response.json().get("items", []):
                page_map = (item.get("pagemap") or {}).get("metatags") or [{}]
                meta = page_map[0] if page_map else {}
                results.append(
                    SearchCandidate(
                        item.get("link", ""),
                        item.get("title", ""),
                        _parse_date(
                            meta.get("article:published_time")
                            or meta.get("datepublished")
                            or meta.get("date")
                        ),
                        meta.get("og:site_name", ""),
                        "Google Programmable Search",
                    )
                )
        return [item for item in results if item.url and item.title]

    @staticmethod
    def _deduplicate_candidates(candidates: list[SearchCandidate]) -> list[SearchCandidate]:
        seen: set[str] = set()
        unique: list[SearchCandidate] = []
        for candidate in sorted(
            candidates,
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        ):
            try:
                key = normalize_url(candidate.url)
            except ValueError:
                key = f"{candidate.publisher.casefold()}::{candidate.title.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique
