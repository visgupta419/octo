"""Retrieval entry point.

Milestone 1 is BM25 only. Milestone 3 adds embedding search, reciprocal rank
fusion and an optional reranker behind this same function signature.
"""

from __future__ import annotations

from ..store.db import Database
from .bm25 import Hit, QueryFilters, bm25_search


def search(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    return bm25_search(db, text, filters, limit)
