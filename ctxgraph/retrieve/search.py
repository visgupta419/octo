"""Retrieval entry point.

Milestone 1 is BM25 only. Milestone 3 adds embedding search, reciprocal rank
fusion and an optional reranker behind this same function signature.
"""

from __future__ import annotations

from ..store.db import Database
from .bm25 import Hit, QueryFilters, bm25_search_mode


def search_mode(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> tuple[list[Hit], str]:
    """Ranked hits and the match mode ("all", "any", "none")."""
    return bm25_search_mode(db, text, filters, limit)


def search(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    return search_mode(db, text, filters, limit)[0]
