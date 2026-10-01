"""Compile ranked hits into a bounded, deduplicated context pack."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..retrieve.bm25 import Hit

# A slice of the budget is held back for graph facts and citations (milestone
# 2 fills the facts; the header and citation lines already cost tokens).
DEFAULT_RESERVE_RATIO = 0.10


@dataclass
class PackChunk:
    id: str
    source_id: str
    path: str
    bucket: str
    start_line: int
    end_line: int
    commit_sha: str | None
    token_count: int
    score: float
    rank: int
    heading: str
    text: str

    @property
    def citation(self) -> str:
        sha = self.commit_sha[:7] if self.commit_sha else "uncommitted"
        return f"{self.path}#L{self.start_line}-{self.end_line} ({sha})"


@dataclass
class ContextPack:
    query: str
    budget_tokens: int
    chunks: list[PackChunk] = field(default_factory=list)
    used_tokens: int = 0
    candidates: int = 0
    dropped_duplicates: int = 0
    dropped_over_budget: int = 0
    facts: list[str] = field(default_factory=list)  # milestone 2
    stale_sources: list[str] = field(default_factory=list)  # milestone 5

    @property
    def buckets(self) -> list[str]:
        seen: list[str] = []
        for c in self.chunks:
            if c.bucket not in seen:
                seen.append(c.bucket)
        return seen


def compile_pack(
    query: str,
    hits: list[Hit],
    budget_tokens: int,
    reserve_ratio: float = DEFAULT_RESERVE_RATIO,
) -> ContextPack:
    pack = ContextPack(query=query, budget_tokens=budget_tokens, candidates=len(hits))
    available = max(0, budget_tokens - int(budget_tokens * reserve_ratio))
    seen_hashes: set[str] = set()
    for h in hits:
        if h.hash in seen_hashes:
            pack.dropped_duplicates += 1
            continue
        seen_hashes.add(h.hash)
        if pack.used_tokens + h.token_count > available:
            pack.dropped_over_budget += 1
            continue
        pack.chunks.append(
            PackChunk(
                id=h.id,
                source_id=h.source_id,
                path=h.path,
                bucket=h.bucket,
                start_line=h.start_line,
                end_line=h.end_line,
                commit_sha=h.commit_sha,
                token_count=h.token_count,
                score=h.score,
                rank=h.rank,
                heading=h.heading,
                text=h.text,
            )
        )
        pack.used_tokens += h.token_count
    return pack
