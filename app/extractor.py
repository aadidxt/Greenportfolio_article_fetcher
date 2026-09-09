from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
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
                canonical_url=normalized,
                original_url=candidate.url,
                normalized_title=" ".join(
                    self._provider_title(candidate.title, candidate.publisher).casefold().split()
                ),
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

        clean_title = _squash(title)[:1000]
        return ArticleDraft(
            publisher=_squash(publisher)[:200],
            published_at=published,
            title=clean_title,
            description=self._first_paragraph(soup, json_ld)[:5000],
            url=normalized,
            normalized_url=normalized,
            source=candidate.source,
            canonical_url=normalized,
            original_url=candidate.url,
            normalized_title=" ".join(clean_title.casefold().split()),
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

    @classmethod
    def _first_paragraph(cls, soup: BeautifulSoup, json_ld: list[dict]) -> str:
        """Return the first defensible paragraph from the actual article body."""
        explicit_selectors = (
            "[itemprop='articleBody'] p",
            "article [data-component*='body' i] p",
            "article [class*='article-body' i] p",
            "article [class*='article__body' i] p",
            "article [class*='story-body' i] p",
            "article [class*='story-content' i] p",
            "article [class*='entry-content' i] p",
            "article [class*='post-content' i] p",
            "article [class*='article-content' i] p",
            "article [id*='article-body' i] p",
            "article [id*='story-body' i] p",
            "article > p",
        )
        found = cls._first_valid_from_selectors(soup, explicit_selectors)
        if found:
            return found

        # Schema.org articleBody is body content; description/SEO fields are
        # deliberately never considered.
        for item in json_ld:
            body = item.get("articleBody")
            if not isinstance(body, str) or not body.strip():
                continue
            body_soup = BeautifulSoup(body, "html.parser")
            blocks = [
                _squash(node.get_text(" ", strip=True))
                for node in body_soup.select("p")
            ]
            if not blocks:
                blocks = [_squash(part) for part in re.split(r"[\r\n]+", body)]
            for text in blocks:
                if cls._meaningful_paragraph(text):
                    return text

        publisher_selectors = (
            "main [class*='article-body' i] p",
            "main [class*='article-content' i] p",
            "main [class*='story-body' i] p",
            "main [class*='story-content' i] p",
            "main [class*='entry-content' i] p",
            "main [class*='post-content' i] p",
            "main [class*='content-body' i] p",
            "[role='main'] [class*='article' i] p",
            "[data-testid*='article' i] p",
            "[data-testid*='story' i] p",
            "article p",
        )
        found = cls._first_valid_from_selectors(soup, publisher_selectors)
        if found:
            return found

        container = cls._best_content_container(soup)
        if container is not None:
            for paragraph in container.find_all("p"):
                text = _squash(paragraph.get_text(" ", strip=True))
                if cls._meaningful_paragraph(text, paragraph):
                    return text
        return ""

    @classmethod
    def _first_valid_from_selectors(
        cls, soup: BeautifulSoup, selectors: tuple[str, ...]
    ) -> str:
        seen: set[int] = set()
        for selector in selectors:
            for paragraph in soup.select(selector):
                marker = id(paragraph)
                if marker in seen:
                    continue
                seen.add(marker)
                text = _squash(paragraph.get_text(" ", strip=True))
                if cls._meaningful_paragraph(text, paragraph):
                    return text
        return ""

    @staticmethod
    def _meaningful_paragraph(text: str, node: Tag | None = None) -> bool:
        if len(text) < 50 or len(text.split()) < 8:
            return False
        lowered = text.casefold().strip()
        if lowered.startswith(
            (
                "advertisement", "subscribe", "sign up", "sign in", "log in",
                "read more", "also read", "related:", "recommended", "cookie",
                "privacy policy", "all rights reserved", "follow us", "share this",
                "download our app", "updated:", "published:", "written by", "edited by",
            )
        ):
            return False
        if node is not None:
            ancestry = " ".join(
                " ".join(
                    [str(parent.get("id", "")), *map(str, parent.get("class", []))]
                )
                for parent in [node, *list(node.parents)[:5]]
                if isinstance(parent, Tag)
            ).casefold()
            negative = (
                "advert", "promo", "related", "recommend", "comment", "footer",
                "caption", "newsletter", "subscribe", "author-bio", "byline",
                "social", "share", "cookie", "navigation", "breadcrumb",
            )
            if any(token in ancestry for token in negative):
                return False
            link_text = sum(
                len(_squash(link.get_text(" ", strip=True))) for link in node.find_all("a")
            )
            if link_text / max(1, len(text)) > 0.45:
                return False
        return True

    @classmethod
    def _best_content_container(cls, soup: BeautifulSoup) -> Tag | None:
        candidates: list[tuple[float, Tag]] = []
        positive = ("article", "story", "entry", "post", "content", "body", "main")
        negative = (
            "nav", "footer", "aside", "related", "recommend", "comment", "promo",
            "advert", "sidebar", "author", "share", "social", "cookie",
        )
        for node in soup.find_all(("article", "main", "section", "div")):
            valid: list[str] = []
            for paragraph in node.find_all("p", recursive=True):
                text = _squash(paragraph.get_text(" ", strip=True))
                if cls._meaningful_paragraph(text, paragraph):
                    valid.append(text)
            if not valid:
                continue
            identity = " ".join(
                [str(node.get("id", "")), *map(str, node.get("class", []))]
            ).casefold()
            score = float(sum(min(len(text), 1000) for text in valid) + len(valid) * 100)
            if node.name in {"article", "main"}:
                score += 500
            score += 250 * sum(token in identity for token in positive)
            score -= 1000 * sum(token in identity for token in negative)
            full_text = _squash(node.get_text(" ", strip=True))
            link_text = sum(
                len(_squash(link.get_text(" ", strip=True))) for link in node.find_all("a")
            )
            score *= max(0.1, 1 - (link_text / max(1, len(full_text))))
            candidates.append((score, node))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _provider_title(title: str, publisher: str) -> str:
        clean = _squash(title)
        suffix = f" - {publisher}" if publisher else ""
        if suffix and clean.casefold().endswith(suffix.casefold()):
            clean = clean[: -len(suffix)]
        return clean
