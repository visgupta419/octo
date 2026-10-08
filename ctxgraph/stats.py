"""Context anti-patterns from the query log and the index itself."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .store.db import Database


@dataclass
class Finding:
    key: str
    message: str
    remedy: str
    items: list[str] = field(default_factory=list)


@dataclass
class Stats:
    queries: int = 0
    any_term_fallbacks: int = 0
    zero_hits: int = 0
    over_budget: int = 0
    avg_used_tokens: float = 0.0
    avg_candidates: float = 0.0
    chunks: int = 0
    embedded: int = 0
    chunks_by_source: dict[str, int] = field(default_factory=dict)
    sources_never_retrieved: list[str] = field(default_factory=list)
    largest_chunks: list[tuple[str, int]] = field(default_factory=list)
    zero_hit_queries: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "findings"}
        d["findings"] = [f.__dict__ for f in self.findings]
        return d


def compute_stats(cfg: Config, db: Database) -> Stats:
    s = Stats()
    rows = db.conn.execute("SELECT * FROM query_log ORDER BY id").fetchall()
    s.queries = len(rows)
    retrieved: set[str] = set()
    used = cand = 0
    for r in rows:
        if r["mode"] == "any":
            s.any_term_fallbacks += 1
        if r["candidates"] == 0:
            s.zero_hits += 1
            if len(s.zero_hit_queries) < 10:
                s.zero_hit_queries.append(r["text"])
        if r["over_budget"]:
            s.over_budget += 1
        used += r["used_tokens"]
        cand += r["candidates"]
        retrieved.update(json.loads(r["sources_json"]))
    if rows:
        s.avg_used_tokens = round(used / len(rows), 1)
        s.avg_candidates = round(cand / len(rows), 1)

    s.chunks_by_source = {
        r["source_id"]: r["n"]
        for r in db.conn.execute("SELECT source_id, COUNT(*) AS n FROM chunks GROUP BY source_id")
    }
    s.chunks = sum(s.chunks_by_source.values())
    s.embedded = db.embedding_count()
    s.largest_chunks = [
        (f"{r['path']}#L{r['start_line']}-{r['end_line']}", r["token_count"])
        for r in db.conn.execute(
            "SELECT path, start_line, end_line, token_count FROM chunks ORDER BY token_count DESC LIMIT 5"
        )
    ]
    if rows:
        s.sources_never_retrieved = sorted(
            src.id for src in cfg.sources if src.id not in retrieved and s.chunks_by_source.get(src.id, 0) > 0
        )

    empty_sources = sorted(src.id for src in cfg.sources if s.chunks_by_source.get(src.id, 0) == 0)
    if empty_sources:
        s.findings.append(Finding(
            "empty_sources", f"{len(empty_sources)} configured source(s) matched no files",
            "fix the paths in ctxgraph.yaml or delete the source", empty_sources))
    if s.sources_never_retrieved:
        s.findings.append(Finding(
            "never_retrieved", f"{len(s.sources_never_retrieved)} source(s) indexed but never retrieved",
            "check they hold what agents ask for, or drop them to keep the index tight",
            s.sources_never_retrieved))
    if s.queries and s.any_term_fallbacks / s.queries > 0.3:
        s.findings.append(Finding(
            "fallback_rate", f"{s.any_term_fallbacks}/{s.queries} queries matched only some terms",
            "queries use words the repo does not; add docs or ownership names, or wait for embeddings (milestone 4)"))
    if s.zero_hits:
        s.findings.append(Finding(
            "zero_hits", f"{s.zero_hits}/{s.queries} queries returned nothing",
            "add a source covering these topics", s.zero_hit_queries))
    if s.queries and s.over_budget / s.queries > 0.5:
        s.findings.append(Finding(
            "budget_ceiling", f"{s.over_budget}/{s.queries} packs dropped candidates for budget",
            "raise budget_tokens_default or lower chunking.max_tokens so more, smaller chunks fit"))
    if cfg.embedding.provider != "none" and s.chunks and s.embedded < s.chunks:
        s.findings.append(Finding(
            "embedding_gap", f"{s.chunks - s.embedded} of {s.chunks} chunks have no vector",
            "run `ctxgraph ingest` to embed them (or set embedding.provider: none)"))
    big = [c for c in s.largest_chunks if c[1] > cfg.chunking.max_tokens * 1.5]
    if big:
        s.findings.append(Finding(
            "oversized_chunks", f"{len(big)} chunk(s) far above chunking.max_tokens",
            "these are single unsplittable blocks; consider excluding generated files",
            [f"{p} ({t} tokens)" for p, t in big]))
    return s


def render_stats(s: Stats) -> str:
    out = ["# ctxgraph stats", ""]
    out.append(f"queries logged: {s.queries}")
    if s.queries:
        out.append(f"  any-term fallbacks: {s.any_term_fallbacks}   zero hits: {s.zero_hits}   over budget: {s.over_budget}")
        out.append(f"  avg candidates: {s.avg_candidates}   avg pack tokens: {s.avg_used_tokens}")
    out.append(f"chunks: {s.chunks}  (embedded: {s.embedded})")
    for sid, n in sorted(s.chunks_by_source.items()):
        out.append(f"  {sid}: {n}")
    if s.largest_chunks:
        out.append("largest chunks:")
        out.extend(f"  {p}: {t} tokens" for p, t in s.largest_chunks)
    out.append("")
    if not s.findings:
        out.append("no findings")
    for f in s.findings:
        out.append(f"- [{f.key}] {f.message}")
        out.append(f"    remedy: {f.remedy}")
        for item in f.items[:10]:
            out.append(f"    - {item}")
    return "\n".join(out)
