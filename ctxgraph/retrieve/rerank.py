"""Optional cross-encoder reranking of the fused candidate list."""

from __future__ import annotations

from typing import Protocol

from ..config import ConfigError, RerankConfig
from .bm25 import Hit


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, hits: list[Hit], top_n: int) -> list[Hit]: ...


def rerank_with_scores(hits: list[Hit], top_n: int, scores: list[float]) -> list[Hit]:
    """Reorder the first ``top_n`` hits by ``scores``; the tail keeps its order."""
    head = hits[:top_n]
    order = sorted(range(len(head)), key=lambda i: (-scores[i], head[i].path, head[i].start_line))
    out = [head[i] for i in order] + hits[top_n:]
    for i, h in enumerate(out, start=1):
        h.rank = i
    return out


class FastEmbedReranker:
    def __init__(self, model: str, cache_dir: str | None = None, max_chars: int = 0):
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ConfigError("rerank.enabled needs: pip install 'ctxgraph[embed]'") from exc
        self.name = f"fastembed:{model}"
        self.max_chars = max_chars
        self._model = TextCrossEncoder(model_name=model, cache_dir=cache_dir)

    def rerank(self, query: str, hits: list[Hit], top_n: int) -> list[Hit]:
        head = hits[:top_n]
        if not head:
            return hits
        texts = [h.text[: self.max_chars] if self.max_chars else h.text for h in head]
        scores = [float(s) for s in self._model.rerank(query, texts)]
        return rerank_with_scores(hits, top_n, scores)


def make_reranker(cfg: RerankConfig, enabled: bool | None = None) -> Reranker | None:
    """Build the reranker; ``enabled`` overrides ``cfg.enabled`` (CLI --rerank)."""
    if not (cfg.enabled if enabled is None else enabled):
        return None
    return FastEmbedReranker(cfg.model, cfg.cache_dir, cfg.max_chars)
