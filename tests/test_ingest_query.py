from __future__ import annotations

from pathlib import Path

from conftest import git, write

from ctxgraph.compile import compile_pack, render_json, render_markdown
from ctxgraph.config import Config
from ctxgraph.ingest import run_ingest
from ctxgraph.retrieve import QueryFilters, build_fts_query, search
from ctxgraph.store import Database


def test_ingest_indexes_sources_and_skips_junk(config: Config, db: Database):
    report = run_ingest(config, db)
    by_id = {s.source_id: s for s in report.sources}
    assert by_id["norms"].files == 1 and by_id["norms"].added >= 1
    assert by_id["adrs"].files == 1
    # catch-all source does not re-claim files owned by earlier sources
    assert by_id["docs"].files == 2  # services/ad-decision.md + blob.md (skipped)
    assert by_id["docs"].skipped == [("docs/blob.md", "binary")]
    paths = {r["path"] for r in db.conn.execute("SELECT path FROM chunks")}
    assert "ignored/secret.md" not in paths
    assert "src/main.py" not in paths
    rows = {r["id"]: r for r in db.list_sources()}
    assert rows["norms"]["ingested_commit"] == report.head_commit
    assert len(report.head_commit) == 40


def test_reingest_is_idempotent(config: Config, db: Database):
    first = run_ingest(config, db)
    second = run_ingest(config, db)
    assert second.added == 0 and second.removed == 0
    assert second.unchanged == first.added
    assert db.count_chunks() == first.added


def test_changed_file_replaces_chunks_and_deleted_file_removes_them(
    config: Config, db: Database, repo: Path
):
    run_ingest(config, db)
    before = db.count_chunks()
    write(repo, "docs/services/ad-decision.md", "# ad-decision-service\n\nNow it is owned by growth-team.\n")
    (repo / "docs" / "adr" / "0001-sqlite.md").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "edit")
    report = run_ingest(config, db)
    assert report.added == 1
    assert report.removed == 2  # old ad-decision chunk + deleted ADR chunk
    assert db.count_chunks() == before - 1
    assert db.count_chunks("adrs") == 0
    hit = search(db, "growth-team")[0]
    assert hit.commit_sha == report.head_commit


def test_full_rebuild_matches_incremental(config: Config, db: Database):
    run_ingest(config, db)
    ids = set(r["id"] for r in db.conn.execute("SELECT id FROM chunks"))
    report = run_ingest(config, db, full=True)
    assert report.unchanged == 0 and report.added == len(ids)
    assert set(r["id"] for r in db.conn.execute("SELECT id FROM chunks")) == ids


def test_removed_source_is_pruned(config: Config, db: Database):
    run_ingest(config, db)
    config.sources = [s for s in config.sources if s.id != "adrs"]
    report = run_ingest(config, db)
    assert report.removed_sources == ["adrs"]
    assert db.count_chunks("adrs") == 0
    assert "adrs" not in {r["id"] for r in db.list_sources()}


def test_fts_query_quotes_terms_and_dedupes():
    assert build_fts_query('retry "pattern" retry!') == '"retry" AND "pattern"'
    assert build_fts_query("a b", mode="or") == '"a" OR "b"'
    assert build_fts_query("!!!") is None


def test_search_ranks_relevant_chunk_first(config: Config, db: Database):
    run_ingest(config, db)
    hits = search(db, "deprecated retry pattern")
    assert hits[0].path == "CONTRIBUTING.md"
    assert hits[0].bucket == "norms"
    assert hits[0].rank == 1
    assert hits[0].heading.startswith("Contributing")


def test_search_falls_back_to_any_term(config: Config, db: Database):
    run_ingest(config, db)
    hits = search(db, "inventory-service unicorns")
    assert hits and hits[0].path == "docs/services/ad-decision.md"


def test_search_handles_underscore_identifiers_and_stemming(config: Config, db: Database):
    run_ingest(config, db)
    assert search(db, "ad_decision")[0].path == "docs/services/ad-decision.md"
    assert any(h.path == "CONTRIBUTING.md" for h in search(db, "approvals"))


def test_filters(config: Config, db: Database):
    run_ingest(config, db)
    assert all(h.bucket == "knowledge" for h in search(db, "service", QueryFilters(buckets=["knowledge"])))
    assert search(db, "retry", QueryFilters(buckets=["knowledge"])) == []
    assert search(db, "SQLite", QueryFilters(sources=["adrs"]))[0].source_id == "adrs"
    assert search(db, "SQLite", QueryFilters(path_prefixes=["docs/services"])) == []
    assert search(db, "SQLite", QueryFilters(path_prefixes=["docs/adr"]))


def test_pack_respects_budget_and_dedupes(config: Config, db: Database):
    run_ingest(config, db)
    hits = search(db, "service retry sqlite")
    assert len(hits) >= 3
    pack = compile_pack("q", hits, budget_tokens=10_000)
    assert len(pack.chunks) == len(hits)
    assert pack.used_tokens == sum(h.token_count for h in hits)

    tiny = compile_pack("q", hits, budget_tokens=hits[0].token_count + 10, reserve_ratio=0.0)
    assert [c.id for c in tiny.chunks] == [hits[0].id]
    assert tiny.dropped_over_budget == len(hits) - 1

    dup = compile_pack("q", hits + [hits[0]], budget_tokens=10_000)
    assert dup.dropped_duplicates == 1


def test_render_markdown_and_json(config: Config, db: Database):
    run_ingest(config, db)
    pack = compile_pack("deprecated retry", search(db, "deprecated retry"), 4000)
    md = render_markdown(pack)
    assert md.startswith('# Context pack — "deprecated retry"')
    assert "## Norms" in md
    assert "CONTRIBUTING.md#L1-" in md
    assert "(" + pack.chunks[0].commit_sha[:7] + ")" in md
    j = render_json(pack)
    assert j["chunks"][0]["citation"].startswith("CONTRIBUTING.md#L")
    assert j["buckets"] == ["norms"]

    empty = render_markdown(compile_pack("zzz", [], 100))
    assert "_No matching context._" in empty


def test_render_escapes_backtick_fences(config: Config, db: Database):
    from ctxgraph.retrieve.bm25 import Hit

    h = Hit("1", "s", "p.md", "norms", "```\ncode\n```", 1, 3, None, 5, "h", "", 1.0, 1)
    md = render_markdown(compile_pack("q", [h], 100))
    assert "````text\n```\ncode\n```\n````" in md


def test_rrf_merge_orders_by_agreement():
    from ctxgraph.retrieve.bm25 import Hit
    from ctxgraph.retrieve.fusion import rrf_merge

    def h(i, path):
        return Hit(i, "s", path, "knowledge", "t", 1, 2, None, 5, "h" + i, "", 1.0, 0)

    a = [h("1", "a"), h("2", "b"), h("3", "c")]
    b = [h("3", "c"), h("1", "a"), h("4", "d")]
    merged = rrf_merge([a, b])
    assert [m.id for m in merged] == ["1", "3", "2", "4"]
    assert [m.rank for m in merged] == [1, 2, 3, 4]
    assert merged[0].score > merged[1].score > merged[2].score
    assert [m.id for m in rrf_merge([a, b], limit=2)] == ["1", "3"]


def test_stopwords_and_test_penalty(config, db):
    from ctxgraph.retrieve.bm25 import is_test_path, search_terms

    assert search_terms("what starts a pipeline execution when it is triggered") == ["starts", "pipeline", "execution", "triggered"]
    assert search_terms("the of a") == ["the", "of", "a"]  # fallback keeps something
    assert is_test_path("orca-core/src/test/java/X.java")
    assert is_test_path("a/FooSpec.groovy") and is_test_path("a/FooTest.kt")
    assert not is_test_path("a/src/main/java/Foo.java")


def test_retrieval_config_parsing(repo):
    from ctxgraph.config import ConfigError, parse_config
    import pytest

    base = {"version": 1, "sources": [{"id": "d", "type": "markdown", "bucket": "knowledge", "paths": ["*.md"]}]}
    cfg = parse_config({**base, "retrieval": {"candidates": 80, "test_path_penalty": 0.3, "max_chunks_per_file": 1}}, repo / "ctxgraph.yaml")
    assert (cfg.retrieval.candidates, cfg.retrieval.test_path_penalty, cfg.retrieval.max_chunks_per_file) == (80, 0.3, 1)
    with pytest.raises(ConfigError):
        parse_config({**base, "retrieval": {"test_path_penalty": 0}}, repo / "ctxgraph.yaml")


def test_pack_per_file_cap(config, db):
    run_ingest(config, db)
    hits = search(db, "service retry sqlite")
    from ctxgraph.retrieve.bm25 import Hit

    dup = [Hit(**{**hits[0].__dict__, "id": f"x{i}", "hash": f"h{i}"}) for i in range(3)] + hits
    pack = compile_pack("q", dup, 10_000, max_chunks_per_file=2)
    assert sum(1 for c in pack.chunks if c.path == hits[0].path) == 2
    assert pack.dropped_file_cap >= 1


def test_oversized_files_are_skipped_and_reported(repo, config, db):
    from conftest import git, write

    write(repo, "docs/huge.md", "# Huge\n\n" + ("x" * 1024 + "\n") * 600)  # ~600 KB
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "huge")
    stages = []
    report = run_ingest(config, db, progress=lambda label, done, total: stages.append(label))
    docs = next(s for s in report.sources if s.source_id == "docs")
    assert any(p == "docs/huge.md" and r.startswith("too large") for p, r in docs.skipped)
    assert "docs/huge.md" not in {r["path"] for r in db.conn.execute("SELECT path FROM chunks")}
    assert "listing files" in stages and any(s.endswith(": chunking") for s in stages)
    config.max_file_kb = 2048
    report = run_ingest(config, db)
    assert "docs/huge.md" in {r["path"] for r in db.conn.execute("SELECT path FROM chunks")}
