"""Retrieval entry point: BM25, optionally fused with vectors, reranked, deduped."""

from __future__ import annotations

from dataclasses import dataclass

from ..store.db import Database
from .bm25 import Hit, QueryFilters, bm25_search_mode, file_importance, is_test_path
from .fusion import rrf_merge


@dataclass
class SearchOptions:
    limit: int = 50
    test_penalty: float = 1.0
    importance_boost: float = 0.0
    embedder: object | None = None  # retrieve.embed.Embedder
    reranker: object | None = None  # retrieve.rerank.Reranker
    rerank_top_n: int = 30
    near_duplicate_cosine: float = 0.0
    rrf_k: int = 60
    retriever: str = "auto"  # auto | bm25 | hybrid | vector


@dataclass
class SearchResult:
    hits: list[Hit]
    mode: str  # e.g. "all", "any+vec", "none"
    dropped_near_duplicates: int = 0
    retriever: str = "bm25"


def _adjust(db: Database, hits: list[Hit], test_penalty: float, boost: float) -> list[Hit]:
    """Apply the same test demotion and importance boost BM25 uses to vector hits."""
    if test_penalty >= 1 and not boost:
        return hits
    import math

    importance = file_importance(db) if boost else {}
    for h in hits:
        if test_penalty < 1 and is_test_path(h.path):
            h.score *= test_penalty
        if boost:
            h.score *= 1 + boost * math.log1p(importance.get(h.path, 0))
    hits.sort(key=lambda h: (-h.score, h.path, h.start_line))
    for i, h in enumerate(hits, start=1):
        h.rank = i
    return hits


def search_detailed(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    opts: SearchOptions | None = None,
) -> SearchResult:
    opts = opts or SearchOptions()
    embedder = opts.embedder
    use_vectors = (
        embedder is not None
        and opts.retriever in ("auto", "hybrid", "vector")
        and db.embedding_count(embedder.name) > 0
    )
    bm25_hits: list[Hit] = []
    mode = "none"
    if opts.retriever != "vector":
        bm25_hits, mode = bm25_search_mode(
            db, text, filters, opts.limit, opts.test_penalty, opts.importance_boost
        )
    hits = bm25_hits
    retriever = "bm25"
    if use_vectors:
        from .vector import drop_near_duplicates, vector_search

        vec = _adjust(db, vector_search(db, embedder, text, filters, opts.limit), opts.test_penalty, opts.importance_boost)
        if opts.retriever == "vector" or not bm25_hits:
            hits, retriever = vec, "vector"
            mode = "vec" if vec else "none"
        else:
            hits, retriever = rrf_merge([bm25_hits, vec], k=opts.rrf_k, limit=opts.limit), "hybrid"
            mode = f"{mode}+vec"
    dropped = 0
    if opts.reranker is not None and hits:
        hits = opts.reranker.rerank(text, hits, opts.rerank_top_n)
        retriever += "+rerank"
    if use_vectors and opts.near_duplicate_cosine:
        from .vector import drop_near_duplicates

        hits, dropped = drop_near_duplicates(db, hits, embedder.name, opts.near_duplicate_cosine)
    return SearchResult(hits=hits, mode=mode, dropped_near_duplicates=dropped, retriever=retriever)


def search_mode(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
    test_penalty: float = 1.0,
    importance_boost: float = 0.0,
) -> tuple[list[Hit], str]:
    """BM25-only ranked hits and the match mode ("all", "any", "none")."""
    r = search_detailed(
        db, text, filters,
        SearchOptions(limit=limit, test_penalty=test_penalty, importance_boost=importance_boost, retriever="bm25"),
    )
    return r.hits, r.mode


def search(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    return search_mode(db, text, filters, limit)[0]
