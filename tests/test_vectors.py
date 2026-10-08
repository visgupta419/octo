from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner
from conftest import CONFIG, write

from ctxgraph.cli import main
from ctxgraph.config import Config, ConfigError, parse_config
from ctxgraph.graph import build_graph
from ctxgraph.ingest import embed_chunks, run_ingest
from ctxgraph.retrieve import QueryFilters, SearchOptions, search_detailed
from ctxgraph.retrieve.embed import HashEmbedder, from_blob, make_embedder, to_blob
from ctxgraph.retrieve.fusion import rrf_merge
from ctxgraph.retrieve.rerank import rerank_with_scores
from ctxgraph.retrieve.vector import drop_near_duplicates, vector_search
from ctxgraph.store import Database

HASH_CONFIG = {**CONFIG, "embedding": {"provider": "hash", "dim": 128}}


def test_hash_embedder_is_deterministic_and_unit_length():
    e = HashEmbedder(128)
    a, b = e.embed(["deprecated retry helper", "deprecated retry helper"])
    assert a == b and e.name == "hash-128"
    assert abs(np.linalg.norm(a) - 1.0) < 1e-5
    c = e.embed_query("retry helper deprecated")
    assert float(np.dot(a, c)) > 0.5  # shares words
    d = e.embed_query("kubernetes ingress")
    assert float(np.dot(a, d)) < 0.3
    assert from_blob(to_blob(a)).shape == (128,)


def test_embedding_config_and_factory(repo: Path):
    cfg = parse_config(HASH_CONFIG, repo / "ctxgraph.yaml")
    assert cfg.embedding.provider == "hash" and cfg.embedding.dim == 128
    assert make_embedder(cfg.embedding).name == "hash-128"
    none = parse_config(dict(CONFIG), repo / "ctxgraph.yaml")
    assert none.embedding.provider == "none" and make_embedder(none.embedding) is None
    with pytest.raises(ConfigError):
        parse_config({**CONFIG, "embedding": {"provider": "magic"}}, repo / "ctxgraph.yaml")
    openai = parse_config({**CONFIG, "embedding": {"provider": "openai"}}, repo / "ctxgraph.yaml")
    assert openai.embedding.model == "text-embedding-3-small"
    with pytest.raises(ConfigError):  # no key in env, remote endpoint
        make_embedder(openai.embedding)


def _indexed(tmp_path: Path, repo: Path):
    cfg = parse_config(HASH_CONFIG, repo / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)
    return cfg, db, make_embedder(cfg.embedding)


def test_embed_chunks_is_incremental_and_model_switch_clears(tmp_path: Path, repo: Path):
    cfg, db, emb = _indexed(tmp_path, repo)
    first = embed_chunks(cfg, db, emb)
    assert first.embedded == db.count_chunks() == first.total and first.embedded > 0
    second = embed_chunks(cfg, db, emb)
    assert second.embedded == 0 and second.total == first.total
    other = HashEmbedder(64)
    third = embed_chunks(cfg, db, other)
    assert third.dropped_other_model == first.total and third.embedded == first.total
    ids, matrix = db.load_vectors(other.name)
    assert len(ids) == first.total and matrix.shape == (first.total, 64)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0)


def test_vector_search_and_filters(tmp_path: Path, repo: Path):
    cfg, db, emb = _indexed(tmp_path, repo)
    embed_chunks(cfg, db, emb)
    hits = vector_search(db, emb, "deprecated RetryHelper backoff jitter", limit=5)
    assert hits[0].path == "CONTRIBUTING.md" and hits[0].rank == 1 and 0 < hits[0].score <= 1.0
    only = vector_search(db, emb, "deprecated RetryHelper backoff jitter", QueryFilters(buckets=["knowledge"]), limit=5)
    assert all(h.bucket == "knowledge" for h in only)
    assert vector_search(db, emb, "", limit=5) == []


def test_hybrid_search_modes(tmp_path: Path, repo: Path):
    cfg, db, emb = _indexed(tmp_path, repo)
    # before embedding: auto falls back to bm25
    r = search_detailed(db, "deprecated retry", None, SearchOptions(embedder=emb))
    assert r.retriever == "bm25" and r.mode == "all"
    embed_chunks(cfg, db, emb)
    r = search_detailed(db, "deprecated retry", None, SearchOptions(embedder=emb, near_duplicate_cosine=0.95))
    assert r.retriever == "hybrid" and r.mode == "all+vec"
    assert r.hits[0].path == "CONTRIBUTING.md" and r.hits[0].rank == 1
    assert search_detailed(db, "deprecated retry", None, SearchOptions(embedder=emb, retriever="bm25")).retriever == "bm25"
    v = search_detailed(db, "deprecated retry", None, SearchOptions(embedder=emb, retriever="vector"))
    assert v.retriever == "vector" and v.mode == "vec"
    # a query BM25 cannot match at all still gets vector hits
    r = search_detailed(db, "zzzz qqqq retry", None, SearchOptions(embedder=emb))
    assert r.hits  # "retry" matched via BM25 any-term and vectors


def test_near_duplicate_dedupe(tmp_path: Path, repo: Path):
    write(repo, "docs/copy.md", (repo / "CONTRIBUTING.md").read_text())
    cfg, db, emb = _indexed(tmp_path, repo)
    embed_chunks(cfg, db, emb)
    hits = vector_search(db, emb, "deprecated RetryHelper backoff jitter", limit=10)
    paths = [h.path for h in hits[:2]]
    assert set(paths) == {"CONTRIBUTING.md", "docs/copy.md"}
    kept, dropped = drop_near_duplicates(db, hits, emb.name, 0.8)  # hash vectors: identical bodies, different path prefix
    assert dropped >= 1 and len({h.path for h in kept} & {"CONTRIBUTING.md", "docs/copy.md"}) == 1
    assert [h.rank for h in kept] == list(range(1, len(kept) + 1))
    same, zero = drop_near_duplicates(db, hits, emb.name, 0.0)
    assert zero == 0 and same is hits


def test_rerank_with_scores_reorders_head_only():
    from ctxgraph.retrieve.bm25 import Hit

    hits = [Hit(str(i), "s", f"p{i}", "knowledge", "t", 1, 2, None, 5, f"h{i}", "", 1.0, i + 1) for i in range(4)]
    out = rerank_with_scores(hits, 3, [0.1, 0.9, 0.5])
    assert [h.id for h in out] == ["1", "2", "0", "3"] and [h.rank for h in out] == [1, 2, 3, 4]


def test_cli_ingest_query_eval_with_hash_embeddings(repo: Path):
    import yaml

    runner = CliRunner()
    runner.invoke(main, ["init", "--path", str(repo)])
    cfg_path = repo / "ctxgraph.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["embedding"] = {"provider": "hash", "dim": 64}
    cfg_path.write_text(yaml.safe_dump(data))
    res = runner.invoke(main, ["-c", str(cfg_path), "ingest"])
    assert res.exit_code == 0, res.output
    assert "embeddings: +" in res.output and "hash-64" in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "ingest"])
    assert "embeddings: +0 " in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "query", "deprecated retry", "--json"])
    assert res.exit_code == 0 and "CONTRIBUTING.md" in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "query", "deprecated retry", "--retriever", "bm25"])
    assert res.exit_code == 0
    (repo / "evals" / "questions.yaml").write_text(yaml.safe_dump({
        "questions": [{"id": "r", "q": "deprecated retry pattern", "expect_paths": ["CONTRIBUTING.md"]}]}))
    res = runner.invoke(main, ["-c", str(cfg_path), "eval", "--no-baseline"])
    assert res.exit_code == 0, res.output
    assert "retriever hybrid" in res.output and "1/1" in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "eval", "--no-baseline", "--retriever", "bm25"])
    assert "retriever bm25" in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "stats"])
    assert "(embedded:" in res.output
    res = runner.invoke(main, ["-c", str(cfg_path), "ingest", "--no-embed"])
    assert res.exit_code == 0 and "embeddings:" not in res.output


@pytest.mark.skipif(not __import__("os").environ.get("CTXGRAPH_TEST_LOCAL_EMBED"), reason="downloads a model; set CTXGRAPH_TEST_LOCAL_EMBED=1")
def test_local_fastembed_provider_end_to_end(repo: Path):
    cfg = parse_config({**CONFIG, "embedding": {"provider": "local"}}, repo / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    emb = make_embedder(cfg.embedding)
    report = embed_chunks(cfg, db, emb)
    assert report.embedded == db.count_chunks() and emb.name == "fastembed:BAAI/bge-small-en-v1.5"
    hits = vector_search(db, emb, "which helper class must we avoid when retrying", limit=3)
    assert hits[0].path == "CONTRIBUTING.md"  # no shared keyword with "deprecated RetryHelper"


@pytest.mark.skipif(not __import__("os").environ.get("CTXGRAPH_TEST_LOCAL_EMBED"), reason="downloads a model; set CTXGRAPH_TEST_LOCAL_EMBED=1")
def test_rerank_override_builds_reranker(repo: Path):
    from ctxgraph.cli import search_options
    from ctxgraph.retrieve.rerank import make_reranker

    cfg = parse_config({**CONFIG, "embedding": {"provider": "none"}}, repo / "ctxgraph.yaml")
    assert make_reranker(cfg.rerank) is None  # disabled in config
    opts = search_options(cfg, "bm25", None, True)  # --rerank wins over the config
    assert opts.reranker is not None and opts.reranker.name.startswith("fastembed:")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    r = search_detailed(db, "deprecated retry", None, opts)
    assert r.retriever == "bm25+rerank" and r.hits[0].path == "CONTRIBUTING.md"
