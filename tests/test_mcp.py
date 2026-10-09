from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ctxgraph.cli import main
from ctxgraph.config import parse_config
from ctxgraph.graph import build_graph
from ctxgraph.ingest import run_ingest
from ctxgraph.store import Database
from test_graph import CONFIG as SF_CONFIG, _sf_repo

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


@pytest.fixture
def indexed_repo(tmp_path: Path) -> Path:
    root = _sf_repo(tmp_path)
    import yaml

    (root / "ctxgraph.yaml").write_text(yaml.safe_dump({**SF_CONFIG, "embedding": {"provider": "none"}}))
    cfg = parse_config({**SF_CONFIG, "embedding": {"provider": "none"}}, root / "ctxgraph.yaml")
    with Database(cfg.db_path) as db:
        run_ingest(cfg, db)
        build_graph(cfg, db)
    return root


def _call(root: Path, fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "ctxgraph.cli", "-c", str(root / "ctxgraph.yaml"), "serve"])

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(run())


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def test_tools_are_listed_with_terse_schemas(indexed_repo: Path):
    async def fn(session):
        return await session.list_tools()

    tools = _call(indexed_repo, fn).tools
    names = {t.name for t in tools}
    assert names == {"get_context", "search", "get_entity", "get_chunk"}
    for t in tools:
        assert t.description and len(t.description) < 600


def test_get_context_search_entity_chunk(indexed_repo: Path):
    async def fn(session):
        ctx = await session.call_tool("get_context", {"query": "how are accounts synced to billing", "budget_tokens": 1500})
        refs = await session.call_tool("search", {"query": "BillingClient push", "limit": 3})
        ent = await session.call_tool("get_entity", {"name": "AccountService"})
        obj = await session.call_tool("get_entity", {"name": "Account", "type": "object"})
        file_ = await session.call_tool("get_entity", {"name": "force-app/main/default/classes/AccountService.cls"})
        missing = await session.call_tool("get_entity", {"name": "NoSuchThing"})
        first_id = _text(refs).splitlines()[0].split(" | ")[0]
        chunk = await session.call_tool("get_chunk", {"chunk_id": first_id})
        bad = await session.call_tool("get_chunk", {"chunk_id": "nope"})
        return ctx, refs, ent, obj, file_, missing, chunk, bad

    ctx, refs, ent, obj, file_, missing, chunk, bad = _call(indexed_repo, fn)
    pack = _text(ctx)
    assert pack.startswith("# Context pack") and "## Facts" in pack and "AccountService" in pack
    lines = _text(refs).splitlines()
    assert 1 <= len(lines) <= 3 and all(" | " in ln and "#L" in ln for ln in lines)
    e = _text(ent)
    assert e.startswith("[symbol] AccountService —") and "-> contains:" in e and "chunks:" in e
    o = _text(obj)
    assert "[object] Account — standard sObject" in o and "<- triggers_on: AccountTrigger" in o
    assert "[file]" in _text(file_) and "declares" in _text(file_)
    assert _text(missing).startswith("no entity named 'NoSuchThing'")
    c = _text(chunk)
    assert c.startswith("force-app/") and "#L" in c and "class" in c
    assert _text(bad).startswith("no chunk")


def test_print_config_and_instructions(indexed_repo: Path):
    runner = CliRunner()
    cfg = str(indexed_repo / "ctxgraph.yaml")
    res = runner.invoke(main, ["-c", cfg, "serve", "--print-config", "claude"])
    assert res.exit_code == 0 and res.output.startswith("claude mcp add ctxgraph -- ") and cfg in res.output
    res = runner.invoke(main, ["-c", cfg, "serve", "--print-config", "cursor"])
    assert res.exit_code == 0 and '"mcpServers"' in res.output and '"serve"' in res.output
    res = runner.invoke(main, ["-c", cfg, "serve", "--print-instructions"])
    assert res.exit_code == 0 and "get_context" in res.output


def test_query_log_marks_mcp_client(indexed_repo: Path):
    async def fn(session):
        await session.call_tool("get_context", {"query": "account trigger"})

    _call(indexed_repo, fn)
    cfg = parse_config({**SF_CONFIG, "embedding": {"provider": "none"}}, indexed_repo / "ctxgraph.yaml")
    with Database(cfg.db_path) as db:
        rows = db.conn.execute("SELECT filters_json FROM query_log").fetchall()
    assert rows and '"client": "mcp"' in rows[-1]["filters_json"]
