from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner
from conftest import git

from ctxgraph.cli import main


def test_init_ingest_query_roundtrip(repo: Path):
    runner = CliRunner()
    cfg = repo / "ctxgraph.yaml"

    res = runner.invoke(main, ["init", "--path", str(repo)])
    assert res.exit_code == 0, res.output
    assert cfg.is_file()
    assert (repo / ".ctxgraph" / ".gitignore").read_text() == "*\n"
    data = yaml.safe_load(cfg.read_text())
    assert data["version"] == 1 and data["embedding"]["provider"] == "local"
    data["embedding"] = {"provider": "none"}  # keep the test offline and BM25-only
    cfg.write_text(yaml.safe_dump(data))

    res = runner.invoke(main, ["init", "--path", str(repo)])
    assert res.exit_code != 0 and "already exists" in res.output

    res = runner.invoke(main, ["-c", str(cfg), "ingest"])
    assert res.exit_code == 0, res.output
    assert "agent-rules" in res.output and "CONTRIBUTING" not in res.output
    assert (repo / ".ctxgraph" / "index.db").is_file()
    assert "skipped docs/blob.md: binary" in res.output

    res = runner.invoke(main, ["-c", str(cfg), "query", "deprecated retry pattern"])
    assert res.exit_code == 0, res.output
    assert "## Norms" in res.output and "CONTRIBUTING.md#L" in res.output

    res = runner.invoke(main, ["-c", str(cfg), "query", "sqlite", "--json", "-b", "expertise"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["chunks"][0]["path"] == "docs/adr/0001-sqlite.md"

    res = runner.invoke(main, ["-c", str(cfg), "query", "sqlite", "-b", "norms"])
    assert res.exit_code == 0 and "_No matching context._" in res.output

    # the index directory never shows up as something to commit
    assert ".ctxgraph" not in git(repo, "status", "--short")


def test_query_without_index_explains(repo: Path):
    runner = CliRunner()
    runner.invoke(main, ["init", "--path", str(repo)])
    res = runner.invoke(main, ["-c", str(repo / "ctxgraph.yaml"), "query", "x"])
    assert res.exit_code != 0 and "ctxgraph ingest" in res.output


def test_missing_config_explains(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["ingest"])
    assert res.exit_code != 0 and "ctxgraph init" in res.output


def test_bad_config_reports_error(repo: Path):
    cfg = repo / "ctxgraph.yaml"
    cfg.write_text("version: 1\nsources:\n  - id: x\n    type: wat\n    bucket: norms\n    paths: ['*.md']\n")
    res = CliRunner().invoke(main, ["-c", str(cfg), "ingest"])
    assert res.exit_code != 0
    assert "type 'wat' is not supported" in res.output
