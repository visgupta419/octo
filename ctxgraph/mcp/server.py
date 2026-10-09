"""`ctxgraph serve`: expose the index to agents over the Model Context Protocol.

Four tools, kept terse on purpose (every schema token is re-sent on every
turn): get_context (a budgeted pack), search (refs only), get_entity (facts
and edges for a class, object, service, file ...), get_chunk (one chunk's
text). Stdout is the protocol channel, so nothing here prints to it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from ..config import Config
from ..store.db import Database
from ..ui.api import Api

INSTRUCTIONS = """\
ctxgraph serves curated, budgeted context for this repository: code split by
declaration, docs, a graph of classes, objects, services, owners and git
history. Before grepping or reading files, call get_context with the task in
plain words; it returns graph facts first, then the most relevant chunks with
path#line citations. Use search when you only need references, get_entity to
learn what a class/object/file is, who owns it and what calls it, and
get_chunk to read a specific chunk by id. Cite the path#line it gives you."""

AGENT_SNIPPET = """\
## Context (ctxgraph)
Before searching the codebase, call the `get_context` tool from the ctxgraph
MCP server with the task in plain words. It returns graph facts (owners,
callers, references) and the most relevant chunks with path#line citations
within a token budget. Use `get_entity` for "what is X / who owns X / what
calls X", `search` for references only, and `get_chunk` to read a chunk by
id. Prefer these over grep for orientation; grep to confirm details.
"""

MAX_BUDGET = 16000


def _trim(items: list[dict[str, Any]], n: int = 15) -> str:
    names = [i["name"] for i in items[:n]]
    return ", ".join(names) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def build_server(cfg: Config):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError("the MCP server needs: pip install 'ctxgraph[mcp]'") from exc

    db = Database(cfg.db_path, check_same_thread=False)
    api = Api(cfg, db)
    mcp = FastMCP("ctxgraph", instructions=INSTRUCTIONS)

    @mcp.tool()
    def get_context(
        query: str,
        budget_tokens: int | None = None,
        buckets: list[str] | None = None,
        paths: list[str] | None = None,
        retriever: str = "auto",
    ) -> str:
        """Budgeted context pack for a task: graph facts, then relevant chunks with path#line citations.
        buckets: any of knowledge, expertise, norms. paths: path prefixes to restrict to. retriever: auto|bm25|hybrid."""
        budget = min(int(budget_tokens or cfg.budget_tokens_default), MAX_BUDGET)
        with api.lock:
            r = api.query({"text": query, "budget": budget, "buckets": buckets or [], "paths": paths or [],
                           "retriever": retriever, "facts": True, "client": "mcp"})
        if "error" in r:
            return f"error: {r['error']}"
        return r["markdown"]

    @mcp.tool()
    def search(query: str, limit: int = 10, buckets: list[str] | None = None, paths: list[str] | None = None) -> str:
        """Ranked chunk references only (no text): one line per hit as `chunk_id | path#Lstart-end | heading | tokens`.
        Follow up with get_chunk on an id, or get_context for a compiled pack."""
        with api.lock:
            hits = api.search(query, limit=min(int(limit), 50), buckets=buckets or [], paths=paths or [])
        if not hits:
            return "no matches"
        return "\n".join(f"{h['id']} | {h['path']}#L{h['start_line']}-{h['end_line']} | {h['heading'] or '-'} | {h['tokens']} tok" for h in hits)

    @mcp.tool()
    def get_entity(name: str, type: str | None = None) -> str:
        """What a thing is and how it connects: a class/method (Foo or Foo.bar), object, field, flow, component, service, team, person, or a file path.
        Returns its fact line, incoming and outgoing edges, and the chunks that carry it. type narrows: symbol, object, field, flow, component, service, team, person, file, rule, permissionset, layout."""
        with api.lock:
            ents = db.find_entities(name, type)
            if not ents and ("/" in name or "." in name.rsplit("/", 1)[-1]):
                f = db.get_entity("file:" + name)
                ents = [f] if f else []
            if not ents:
                return f"no entity named {name!r}" + (f" of type {type}" if type else "") + "; try search"
            out: list[str] = []
            for e in ents[:3]:
                d = api.entity(e.id)
                if d is None:
                    continue
                out.append(f"[{d['type']}] {d['summary']}")
                for kind, items in d["outgoing"].items():
                    out.append(f"  -> {kind}: {_trim(items)}")
                for kind, items in d["incoming"].items():
                    out.append(f"  <- {kind}: {_trim(items)}")
                if d["chunks"]:
                    out.append("  chunks: " + "; ".join(f"{c['id']} {c['path']}#L{c['start_line']}-{c['end_line']}" for c in d["chunks"][:8]))
                if d["mentioned_in"]:
                    out.append("  mentioned in: " + ", ".join(d["mentioned_in"][:5]))
            if len(ents) > 3:
                out.append(f"({len(ents) - 3} more entities match; pass type to narrow)")
        return "\n".join(out)

    @mcp.tool()
    def get_chunk(chunk_id: str) -> str:
        """The text of one chunk by id (ids come from search, get_entity and pack citations)."""
        with api.lock:
            c = api.chunk(chunk_id)
        if c is None:
            return f"no chunk {chunk_id!r}"
        sha = (c["commit"] or "uncommitted")[:7]
        return f"{c['path']}#L{c['start_line']}-{c['end_line']} ({sha}) · {c['tokens']} tokens · {c['bucket']}\n{c['text']}"

    return mcp


def client_config(cfg: Config, flavour: str) -> str:
    """A ready-to-paste client configuration using absolute paths."""
    exe = shutil.which("ctxgraph") or sys.argv[0]
    conf = str(cfg.config_path or (cfg.repo_root / "ctxgraph.yaml"))
    if flavour == "claude":
        return f"claude mcp add ctxgraph -- {exe} -c {conf} serve"
    return json.dumps({"mcpServers": {"ctxgraph": {"command": exe, "args": ["-c", conf, "serve"]}}}, indent=2)
