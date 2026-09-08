from __future__ import annotations

import re

from .models import ArticleDraft


FINANCE_TERMS = {
    "aif",
    "asset management",
    "business",
    "capital",
    "equity",
    "finance",
    "financial",
    "fund",
    "investment",
    "investor",
    "market",
    "money",
    "portfolio",
    "pms",
    "sebi",
    "smallcase",
    "stock",
    "wealth",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def is_relevant(article: ArticleDraft) -> bool:
    title = _clean(article.title)
    description = _clean(article.description)
    publisher = _clean(article.publisher)
    url = _clean(article.url)
    combined = " ".join((title, description, publisher, url))

    if "green portfolio" in combined or "greenportfolio" in combined:
        return True

    has_named_person = "divam sharma" in combined or "anuj jain" in combined
    if not has_named_person:
        return False

    # Common names need a finance/investing context to avoid false positives.
    context = " ".join((title, description, url))
    finance_hits = sum(term in context for term in FINANCE_TERMS)
    name_in_title = "divam sharma" in title or "anuj jain" in title
    return finance_hits >= 1 and (name_in_title or finance_hits >= 2)
