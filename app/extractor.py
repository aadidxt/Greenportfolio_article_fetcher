from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag
from dateutil.parser import parse as parse_date

from .config import Settings
from .models import ArticleDraft, SearchCandidate
from .url_utils import normalize_url, validate_public_url

logger = logging.getLogger(__name__)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parse_date(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


class ArticleExtractor:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.user_agent = "GreenPortfolioMediaMonitor/1.0 (+respectful article indexing)"
        self.session.headers.update({"User-Agent": self.user_agent})
        self._robots: dict[str, RobotFileParser] = {}

    def extract(self, candidate: SearchCandidate) -> ArticleDraft | None:
        try:
            response, final_url = self._safe_get(candidate.url)
        except Exception as exc:
            logger.info("Could not download article %s: %s", candidate.url, exc)
            # Direct publisher URLs can still be retained with verified provider metadata.
            if "news.google." in candidate.url or not candidate.published_at:
                return None
            try:
                normalized = normalize_url(candidate.url)
            except ValueError:
                return None
            return ArticleDraft(
                publisher=candidate.publisher or urlsplit(candidate.url).hostname or "Unknown",
                published_at=candidate.published_at,
                title=self._provider_title(candidate.title, candidate.publisher),
                description="",
                url=normalized,
                normalized_url=normalized,
                source=candidate.source,
            )

        soup = BeautifulSoup(response, "html.parser")
        for node in soup(["script", "style", "noscript", "nav", "footer", "aside"]):
            if isinstance(node, Tag) and node.name != "script":
                node.decompose()

        json_ld = self._json_ld(response)
        canonical = self._canonical_url(soup, final_url, json_ld)
        if (urlsplit(canonical).hostname or "").casefold().endswith("news.google.com"):
            # Never persist an aggregator/search redirect as the article URL.
            return None
        try:
            normalized = normalize_url(canonical)
        except ValueError:
            normalized = normalize_url(final_url)

        title = self._title(soup, json_ld) or self._provider_title(
            candidate.title, candidate.publisher
        )
        publisher = self._publisher(soup, json_ld) or candidate.publisher
        if not publisher:
            publisher = (urlsplit(canonical).hostname or "Unknown").removeprefix("www.")
        published = self._published_at(soup, json_ld) or candidate.published_at
        if not title or not published:
            return None

        return ArticleDraft(
            publisher=_squash(publisher)[:200],
            published_at=published,
            title=_squash(title)[:1000],
            description=self._first_paragraph(soup)[:5000],
            url=normalized,
            normalized_url=normalized,
            source=candidate.source,
        )

    def _safe_get(self, url: str) -> tuple[bytes, str]:
        current = url
        for _ in range(6):
            validate_public_url(current)
            if not self._allowed_by_robots(current):
                raise PermissionError("Blocked by robots.txt")
            response = self.session.get(
                current,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("Redirect had no target")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.casefold() and "xhtml" not in content_type.casefold():
                response.close()
                raise ValueError("URL is not an HTML article")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 3_000_000:
                    break
                chunks.append(chunk)
            response.close()
            return b"".join(chunks), current
        raise RuntimeError("Too many redirects")

    def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                validate_public_url(parser.url)
                response = self.session.get(
                    parser.url,
                    timeout=min(self.settings.request_timeout_seconds, 8),
                    allow_redirects=False,
                )
                if response.ok:
                    parser.parse(response.text.splitlines())
                else:
                    parser.parse([])
            except requests.RequestException:
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.user_agent, url)

    @staticmethod
    def _json_ld(html: bytes) -> list[dict]:
        raw_soup = BeautifulSoup(html, "html.parser")
        documents: list[dict] = []
        for script in raw_soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(script.get_text(strip=True))
            except (json.JSONDecodeError, TypeError):
                continue
            stack = value if isinstance(value, list) else [value]
            for item in stack:
                if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                    documents.extend(x for x in item["@graph"] if isinstance(x, dict))
                elif isinstance(item, dict):
                    documents.append(item)
        article_types = {"article", "newsarticle", "reportage", "blogposting"}
        return [
            item
            for item in documents
            if str(item.get("@type", "")).casefold() in article_types
        ] or documents

    @staticmethod
    def _canonical_url(soup: BeautifulSoup, final_url: str, json_ld: list[dict]) -> str:
        link = soup.select_one('link[rel="canonical"][href]')
        if link and link.get("href"):
            return urljoin(final_url, str(link["href"]))
        for item in json_ld:
            value = item.get("url") or item.get("mainEntityOfPage")
            if isinstance(value, dict):
                value = value.get("@id")
            if isinstance(value, str):
                return urljoin(final_url, value)
        return final_url

    @staticmethod
    def _title(soup: BeautifulSoup, json_ld: list[dict]) -> str:
        for item in json_ld:
            if isinstance(item.get("headline"), str):
                return item["headline"]
        for selector, attribute in (
            ('meta[property="og:title"]', "content"),
            ('meta[name="twitter:title"]', "content"),
        ):
            node = soup.select_one(selector)
            if node and node.get(attribute):
                return str(node[attribute])
        heading = soup.select_one("article h1, main h1, h1")
        return heading.get_text(" ", strip=True) if heading else ""

    @staticmethod
    def _publisher(soup: BeautifulSoup, json_ld: list[dict]) -> str:
        for item in json_ld:
            publisher = item.get("publisher")
            if isinstance(publisher, dict) and isinstance(publisher.get("name"), str):
                return publisher["name"]
        node = soup.select_one('meta[property="og:site_name"][content]')
        return str(node["content"]) if node else ""

    @staticmethod
    def _published_at(soup: BeautifulSoup, json_ld: list[dict]) -> datetime | None:
        for item in json_ld:
            parsed = _parse_datetime(item.get("datePublished"))
            if parsed:
                return parsed
        selectors = (
            'meta[property="article:published_time"]',
            'meta[name="date"]',
            'meta[name="pubdate"]',
            'meta[name="publish-date"]',
            'meta[itemprop="datePublished"]',
            "time[datetime]",
        )
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            parsed = _parse_datetime(node.get("content") or node.get("datetime"))
            if parsed:
                return parsed
        return None

    @staticmethod
    def _first_paragraph(soup: BeautifulSoup) -> str:
        boilerplate = ("subscribe", "sign up", "advertisement", "read more", "updated:")
        for selector in (
            "article [itemprop='articleBody'] p",
            "article .article-body p",
            "article .story-content p",
            "article p",
            "main p",
        ):
            for paragraph in soup.select(selector):
                text = _squash(paragraph.get_text(" ", strip=True))
                lowered = text.casefold()
                if len(text) >= 40 and not any(text.startswith(word) for word in boilerplate):
                    return text
        return ""

    @staticmethod
    def _provider_title(title: str, publisher: str) -> str:
        clean = _squash(title)
        suffix = f" - {publisher}" if publisher else ""
        if suffix and clean.casefold().endswith(suffix.casefold()):
            clean = clean[: -len(suffix)]
        return clean
