from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctxgraph.config import parse_config
from ctxgraph.store import Database


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo with norms, an ADR, docs, and junk to ignore."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(
        root,
        "CONTRIBUTING.md",
        "# Contributing\n\n## Retries\n\nNever use the deprecated RetryHelper class; "
        "call backoff.retry() with jitter instead.\n\n## Reviews\n\nOne approval from "
        "the owning team is required before merge.\n",
    )
    write(
        root,
        "docs/adr/0001-sqlite.md",
        "# ADR 1: SQLite for the store\n\n## Decision\n\nWe chose SQLite with FTS5 "
        "because it needs zero infrastructure and ships with Python.\n",
    )
    write(
        root,
        "docs/services/ad-decision.md",
        "# ad-decision-service\n\nThe ad_decision service selects an ad for a request. "
        "It calls inventory-service over RPC and is owned by ads-decisioning.\n",
    )
    write(root, "src/main.py", "print('hello')\n")
    write(root, ".gitignore", "ignored/\n")
    write(root, "ignored/secret.md", "# Secret\n\nThis must never be indexed.\n")
    (root / "docs" / "blob.md").write_bytes(b"\x00\x01binary\x00")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


CONFIG = {
    "version": 1,
    "sources": [
        {"id": "norms", "type": "markdown", "bucket": "norms", "paths": ["CONTRIBUTING.md"]},
        {"id": "adrs", "type": "markdown", "bucket": "expertise", "paths": ["docs/adr/**/*.md"]},
        {"id": "docs", "type": "markdown", "bucket": "knowledge", "paths": ["**/*.md"]},
    ],
}


@pytest.fixture
def config(repo: Path):
    return parse_config(dict(CONFIG), repo / "ctxgraph.yaml")


@pytest.fixture
def db(tmp_path: Path):
    with Database(tmp_path / "index.db") as d:
        yield d
