"""Embedding providers behind one small interface.

* ``local``   fastembed (ONNX, CPU) with bge-small by default; needs the
             ``embed`` extra and a one-time model download.
* ``openai``  any OpenAI-compatible /embeddings endpoint (OpenAI, Ollama,
             LM Studio, vLLM) via ``base_url`` + ``api_key_env``.
* ``voyage``  Voyage AI (code-tuned models), ``VOYAGE_API_KEY``.
* ``hash``    deterministic feature-hashed word/bigram vectors. Not
             semantic; exists so the vector pipeline runs and is tested
             anywhere with no model, and as a floor in evals.
* ``none``    BM25 only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from typing import Protocol

import numpy as np

from ..config import ConfigError, EmbeddingConfig

_WORD_RE = re.compile(r"\w+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Feature hashing of lowercase words and bigrams into ``dim`` buckets."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim, dtype=np.float32)
        words = [w.lower() for w in _WORD_RE.findall(text)]
        feats = words + [a + " " + b for a, b in zip(words, words[1:])]
        for f in feats:
            h = hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            v[idx] += 1.0 if h[4] & 1 else -1.0
        n = float(np.linalg.norm(v))
        return (v / n).tolist() if n else v.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FastEmbedEmbedder:
    def __init__(self, model: str, cache_dir: str | None = None, batch_size: int = 64, parallel: int | None = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ConfigError("embedding.provider 'local' needs: pip install 'ctxgraph[embed]'") from exc
        self.name = f"fastembed:{model}"
        self.batch_size = batch_size
        self.parallel = parallel
        self._model = TextEmbedding(model_name=model, cache_dir=cache_dir)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts, batch_size=self.batch_size, parallel=self.parallel)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()


class HttpEmbedder:
    """OpenAI-compatible or Voyage ``/embeddings`` over plain urllib."""

    def __init__(self, provider: str, model: str, base_url: str | None, api_key_env: str, batch_size: int = 64):
        self.provider = provider
        self.model = model
        self.name = f"{provider}:{model}"
        self.batch_size = batch_size
        default_url = "https://api.voyageai.com/v1" if provider == "voyage" else "https://api.openai.com/v1"
        self.base_url = (base_url or default_url).rstrip("/")
        self.api_key = os.environ.get(api_key_env, "")
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            raise ConfigError(f"embedding: set {api_key_env} for provider {provider!r}")

    def _post(self, texts: list[str], input_type: str | None) -> list[list[float]]:
        body: dict = {"model": self.model, "input": texts}
        if self.provider == "voyage" and input_type:
            body["input_type"] = input_type
        req = urllib.request.Request(
            self.base_url + "/embeddings",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [r["embedding"] for r in rows]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i : i + self.batch_size], "document"))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._post([text], "query")[0]


def make_embedder(cfg: EmbeddingConfig) -> Embedder | None:
    p = cfg.provider
    if p == "none":
        return None
    if p == "hash":
        return HashEmbedder(cfg.dim or 256)
    if p == "local":
        return FastEmbedEmbedder(cfg.model, cfg.cache_dir, cfg.batch_size, cfg.parallel)
    if p in ("openai", "voyage"):
        key_env = cfg.api_key_env or ("VOYAGE_API_KEY" if p == "voyage" else "OPENAI_API_KEY")
        return HttpEmbedder(p, cfg.model, cfg.base_url, key_env, cfg.batch_size)
    raise ConfigError(f"unknown embedding.provider {p!r} (none, local, openai, voyage, hash)")


def to_blob(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
