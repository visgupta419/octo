"""BM25 retrieval over the FTS5 index with hard SQL filters."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..store.db import Database

_TERM_RE = re.compile(r"\w+", re.UNICODE)

# Column weights for bm25(): text, path tokens, split identifiers (which now
# include the chunk's heading and file name, so "execution launcher" finds
# ExecutionLauncher.java).
_W_TEXT, _W_PATH, _W_TERMS = 1.0, 0.5, 1.0

# Question words that carry no signal; dropping them lets the all-terms
# query succeed far more often.
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "with", "by", "is", "are",
    "was", "be", "it", "its", "this", "that", "how", "what", "which", "where", "when", "who",
    "why", "do", "does", "did", "we", "our", "i", "my", "you", "can", "should", "from", "into",
    "at", "as", "if", "then", "there", "their", "them", "they", "not", "no", "any", "all",
    "one", "use", "used", "using", "via", "about", "up", "out", "so", "has", "have", "had",
    "me", "us", "want", "need", "way", "thing", "things",
})

TEST_PATH_RE = re.compile(r"(^|/)(test|tests|spec|specs|__tests__|testing)(/|$)|(Test|Tests|Spec|IT)\.\w+$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


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


def search_terms(text: str) -> list[str]:
    """Query terms minus stopwords and one/two-letter tokens (with fallback)."""
    terms = query_terms(text)
    kept = [t for t in terms if t.lower() not in STOPWORDS and len(t) > 2]
    return kept or terms


def build_fts_query(text: str, mode: str = "and") -> str | None:
    """Turn free text into a safe FTS5 MATCH expression (terms quoted)."""
    terms = search_terms(text)
    if not terms:
        return None
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(f'"{t}"' for t in terms)


def file_importance(db: Database) -> dict[str, int]:
    """path -> incoming ``references`` to the symbols the file declares.

    Only class-level references count: method-level ``calls`` edges (many
    from test methods) skewed the boost on orca and cost 8 points of recall.
    Cached on the connection; ``Database.clear_graph`` drops the cache.
    """
    cached = getattr(db, "_importance_cache", None)
    if cached is not None:
        return cached
    rows = db.conn.execute(
        """
        SELECT json_extract(s.attrs_json, '$.path') AS path, COUNT(*) AS n
        FROM edges e JOIN entities s ON s.id = e.dst_id
        WHERE e.kind = 'references' AND s.type = 'symbol'
        GROUP BY path
        """
    )
    result = {r["path"]: int(r["n"]) for r in rows if r["path"]}
    db._importance_cache = result
    return result


def _run(
    db: Database,
    fts_query: str,
    filters: QueryFilters,
    limit: int,
    test_penalty: float = 1.0,
    importance: dict[str, int] | None = None,
    boost: float = 0.0,
) -> list[Hit]:
    where, params = filters.sql()
    fetch = limit * 3 if (test_penalty < 1 or boost) else limit
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
        [_W_TEXT, _W_PATH, _W_TERMS, fts_query, *params, fetch],
    )
    hits: list[Hit] = []
    for r in rows:
        score = -float(r["score"])
        if test_penalty < 1 and is_test_path(r["path"]):
            score *= test_penalty
        if boost and importance:
            score *= 1 + boost * math.log1p(importance.get(r["path"], 0))
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
                score=score,
                rank=0,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.path, h.start_line))
    del hits[limit:]
    for i, h in enumerate(hits, start=1):
        h.rank = i
    return hits


def bm25_search_mode(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
    test_penalty: float = 1.0,
    importance_boost: float = 0.0,
) -> tuple[list[Hit], str]:
    """BM25 top-k plus how it matched: "all" terms, "any" term, or "none".

    Chunks holding every term are ranked first, then the any-term list is
    fused in by reciprocal rank so long questions still reach files that
    lack one of the words.
    """
    from .fusion import rrf_merge

    filters = filters or QueryFilters()
    terms = search_terms(text)
    if not terms:
        return [], "none"
    importance = file_importance(db) if importance_boost else None
    strict = _run(db, build_fts_query(text, "and") or "", filters, limit, test_penalty, importance, importance_boost)
    if len(terms) == 1:
        return strict, ("all" if strict else "none")
    loose = _run(db, build_fts_query(text, "or") or "", filters, limit, test_penalty, importance, importance_boost)
    if not strict:
        return loose, ("any" if loose else "none")
    if not loose:
        return strict, "all"
    return rrf_merge([strict, strict, loose], limit=limit), "all"


def bm25_search(
    db: Database,
    text: str,
    filters: QueryFilters | None = None,
    limit: int = 50,
) -> list[Hit]:
    """BM25 top-k. Requires all terms first; falls back to any-term if empty."""
    return bm25_search_mode(db, text, filters, limit)[0]
