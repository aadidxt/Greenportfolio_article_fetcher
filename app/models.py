from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SearchCandidate:
    url: str
    title: str
    published_at: datetime | None = None
    publisher: str = ""
    source: str = ""


@dataclass(slots=True)
class ArticleDraft:
    publisher: str
    published_at: datetime
    title: str
    description: str
    url: str
    normalized_url: str
    source: str = ""


@dataclass(slots=True)
class ArticleRecord(ArticleDraft):
    id: str = ""
    discovered_at: str = ""
    synced_to_sheet: bool = False
    successful_fetch_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat()
        return data


@dataclass(slots=True)
class FetchCounters:
    search_results: int = 0
    relevant_articles: int = 0
    new_articles: int = 0
    duplicates: int = 0
    failed_articles: int = 0

