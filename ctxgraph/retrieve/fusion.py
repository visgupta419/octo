"""Reciprocal rank fusion, used to merge ranked lists from different retrievers."""

from __future__ import annotations

from typing import Iterable

from .bm25 import Hit


def rrf_merge(lists: Iterable[list[Hit]], k: int = 60, limit: int | None = None) -> list[Hit]:
    """Merge ranked hit lists; a hit's score is the sum of 1/(k + rank) over lists."""
    scores: dict[str, float] = {}
    first: dict[str, Hit] = {}
    for hits in lists:
        for rank, h in enumerate(hits, start=1):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank)
            first.setdefault(h.id, h)
    merged = sorted(first.values(), key=lambda h: (-scores[h.id], h.path, h.start_line))
    if limit is not None:
        del merged[limit:]
    out: list[Hit] = []
    for i, h in enumerate(merged, start=1):
        out.append(Hit(**{**h.__dict__, "score": scores[h.id], "rank": i}))
    return out
