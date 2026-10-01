"""BM25 retrieval over the FTS5 index with hard SQL filters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..store.db import Database

_TERM_RE = re.compile(r"\w+", re.UNICODE)

# Column weights for bm25(): text, path tokens, split identifiers.
_W_TEXT, _W_PATH, _W_TERMS = 1.0, 0.5, 0.6


@dataclass
class QueryFilters:
    buckets: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    path_prefixes: list[str] = field(default_factory=list)

    def sql(self) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []
        if self.buckets:
            clauses.append(f"c.bucket IN ({','.join('?' * len(self.buckets))})")
            params.extend(self.buckets)
        if self.sources:
            clauses.append(f"c.source_id IN ({','.join('?' * len(self.sources))})")
            params.extend(self.sources)
        if self.path_prefixes:
            ors = " OR ".join("c.path LIKE ? ESCAPE '\\'" for _ in self.path_prefixes)
            clauses.append(f"({ors})")
            for p in self.path_prefixes:
                esc = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(esc + "%")
        return (" AND " + " AND ".join(clauses)) if clauses else "", params


@dataclass
class Hit:
    id: str
    source_id: str
    path: str
    bucket: str
    text: str
    start_line: int
    end_line: int
    commit_sha: str | None
    token_count: int
    hash: str
    heading: str
    score: float  # higher is better
    rank: int


def query_terms(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in _TERM_RE.findall(text):
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def build_fts_query(text: str, mode: str = "and") -> str | None:
    """Turn free text into a safe FTS5 MATCH expression (terms quoted)."""
    terms = query_terms(text)
    if not terms:
        return None
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(f'"{t}"' for t in terms)


def _run(db: Database, fts_query: str, filters: QueryFilters, limit: int) -> list[Hit]:
    where, params = filters.sql()
    rows = db.conn.execute(
        f"""
        SELECT c.id, c.source_id, c.path, c.bucket, c.text, c.start_line, c.end_line,
               c.commit_sha, c.token_count, c.hash,
               COALESCE((SELECT value FROM chunk_meta m
                         WHERE m.chunk_id = c.id AND m.key = 'heading'), '') AS heading,
               bm25(chunks_fts, ?, ?, ?) AS score
        FROM chunks_fts f
        JOIN chunks c ON c.rid = f.rowid
        WHERE chunks_fts MATCH ?{where}
        ORDER BY score, c.path, c.start_line
        LIMIT ?
        """,
        [_W_TEXT, _W_PATH, _W_TERMS, fts_query, *params, limit],
    )
    hits: list[Hit] = []
    for i, r in enumerate(rows, start=1):
        hits.append(
            Hit(
                id=r["id"],
                source_id=r["source_id"],
                path=r["path"],
                bucket=r["bucket"],
                text=r["text"],
                start_line=r["start_line"],
                end_line=r["end_line"],
                commit_sha=r["commit_sha"],
                token_count=r["token_count"],
                hash=r["hash"],
                heading=r["heading"],
                score=-float(r["score"]),
                rank=i,
            )
        )
    return hits


def bm25_search(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    """BM25 top-k. Requires all terms first; falls back to any-term if empty."""
    filters = filters or QueryFilters()
    terms = query_terms(text)
    if not terms:
        return []
    hits = _run(db, build_fts_query(text, "and") or "", filters, limit)
    if hits or len(terms) == 1:
        return hits
    return _run(db, build_fts_query(text, "or") or "", filters, limit)
