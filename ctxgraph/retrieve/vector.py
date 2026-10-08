"""Brute-force cosine search over stored chunk vectors, and near-dup dedupe."""

from __future__ import annotations

import numpy as np

from ..store.db import Database
from .bm25 import Hit, QueryFilters
from .embed import Embedder


def _hits_for_ids(db: Database, chosen: list[tuple[str, float]]) -> list[Hit]:
    marks = ",".join("?" * len(chosen))
    rows = {
        r["id"]: r
        for r in db.conn.execute(
            f"""
            SELECT c.id, c.source_id, c.path, c.bucket, c.text, c.start_line, c.end_line,
                   c.commit_sha, c.token_count, c.hash,
                   COALESCE((SELECT value FROM chunk_meta m WHERE m.chunk_id = c.id AND m.key = 'heading'), '') AS heading
            FROM chunks c WHERE c.id IN ({marks})
            """,
            [cid for cid, _ in chosen],
        )
    }
    hits: list[Hit] = []
    for cid, sim in chosen:
        r = rows.get(cid)
        if r is None:
            continue
        hits.append(
            Hit(
                id=r["id"], source_id=r["source_id"], path=r["path"], bucket=r["bucket"], text=r["text"],
                start_line=r["start_line"], end_line=r["end_line"], commit_sha=r["commit_sha"],
                token_count=r["token_count"], hash=r["hash"], heading=r["heading"], score=sim, rank=0,
            )
        )
    for i, h in enumerate(hits, start=1):
        h.rank = i
    return hits


def vector_search(
    db: Database,
    embedder: Embedder,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    """Top-``limit`` chunks by cosine similarity to the query (score = cosine)."""
    ids, matrix = db.load_vectors(embedder.name)
    if not ids:
        return []
    q = np.asarray(embedder.embed_query(text), dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return []
    sims = matrix @ (q / qn)
    filters = filters or QueryFilters()
    where, params = filters.sql()
    if where:
        allowed = {r["id"] for r in db.conn.execute(f"SELECT c.id FROM chunks c WHERE 1=1{where}", params)}
        mask = np.fromiter((i in allowed for i in ids), dtype=bool, count=len(ids))
        sims = np.where(mask, sims, -2.0)
    k = min(limit, len(ids))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top], kind="stable")]
    chosen = [(ids[i], float(sims[i])) for i in top if sims[i] > -2.0]
    return _hits_for_ids(db, chosen) if chosen else []


def drop_near_duplicates(db: Database, hits: list[Hit], model: str, threshold: float) -> tuple[list[Hit], int]:
    """Drop lower-ranked hits whose vector is within ``threshold`` cosine of a kept one."""
    if not hits or threshold <= 0 or threshold >= 1:
        return hits, 0
    ids, matrix = db.load_vectors(model)
    if not ids:
        return hits, 0
    index = {cid: i for i, cid in enumerate(ids)}
    kept: list[Hit] = []
    kept_rows: list[int] = []
    dropped = 0
    for h in hits:
        row = index.get(h.id)
        if row is None:
            kept.append(h)
            continue
        if kept_rows and float((matrix[kept_rows] @ matrix[row]).max()) > threshold:
            dropped += 1
            continue
        kept.append(h)
        kept_rows.append(row)
    for i, h in enumerate(kept, start=1):
        h.rank = i
    return kept, dropped
