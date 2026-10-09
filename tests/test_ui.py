from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from conftest import CONFIG

from ctxgraph.config import parse_config
from ctxgraph.graph import build_graph
from ctxgraph.ingest import run_ingest
from ctxgraph.store import Database
from ctxgraph.ui.server import UiServer
from test_graph import CONFIG as SF_CONFIG, _sf_repo


@pytest.fixture
def server(tmp_path: Path):
    root = _sf_repo(tmp_path)
    cfg = parse_config({**SF_CONFIG, "embedding": {"provider": "hash", "dim": 64}}, root / "ctxgraph.yaml")
    with Database(cfg.db_path) as db:
        run_ingest(cfg, db)
        build_graph(cfg, db)
        from ctxgraph.ingest import embed_chunks
        from ctxgraph.retrieve.embed import make_embedder

        embed_chunks(cfg, db, make_embedder(cfg.embedding))
    srv = UiServer(cfg, "127.0.0.1", 0)
    srv.start_background()
    yield srv
    srv.shutdown()


def get(srv, path):
    with urllib.request.urlopen(srv.url.rstrip("/") + path, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def get_json(srv, path):
    status, ctype, body = get(srv, path)
    assert "json" in ctype, ctype
    return status, json.loads(body)


def post_json(srv, path, payload):
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def test_static_and_overview(server):
    status, ctype, body = get(server, "/")
    assert status == 200 and "text/html" in ctype and b"ctxgraph" in body
    assert get(server, "/static/app.js")[0] == 200 and get(server, "/static/style.css")[0] == 200
    status, o = get_json(server, "/api/overview")
    assert status == 200
    assert o["repo"] == "sf" and o["chunks"]["total"] > 0 and o["entities_total"] > 0
    assert {s["id"] for s in o["sources"]} == {"docs", "apex", "lwc", "meta"}
    assert o["embeddings"]["count"] == o["chunks"]["total"] and o["embeddings"]["model"] == "hash-64"
    assert "symbol" in o["entities"] and "calls" in o["edges"] and o["config"]["parser"] == "auto"


def test_path_traversal_refused(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/static/../api.py")
    assert exc.value.code == 404


def test_entities_entity_and_neighborhood(server):
    _, data = get_json(server, "/api/entities?type=symbol&q=accountservice")
    names = [e["name"] for e in data["items"]]
    assert "AccountService" in names and data["types"]["symbol"] >= 5
    ent = next(e for e in data["items"] if e["name"] == "AccountService")
    assert ent["in"] >= 1 and ent["path"].endswith("AccountService.cls")
    _, d = get_json(server, f"/api/entity?id={ent['id']}")
    assert d["name"] == "AccountService" and d["summary"].startswith("AccountService —")
    assert d["chunks"] and d["chunks"][0]["path"].endswith("AccountService.cls")
    assert "contains" in d["outgoing"] and d["mention_chunks"][0]["path"] == "README.md"
    _, g = get_json(server, f"/api/neighborhood?id={ent['id']}")
    ids = {n["id"] for n in g["nodes"]}
    assert ent["id"] in ids and any(n["type"] == "object" and n["name"] == "Account" for n in g["nodes"])
    assert all(e["src"] in ids and e["dst"] in ids for e in g["edges"]) and g["edges"]
    with pytest.raises(urllib.error.HTTPError):
        get(server, "/api/entity?id=nope")


def test_files_file_and_chunk(server):
    _, data = get_json(server, "/api/files?q=AccountService")
    assert data["total"] == 1 and data["items"][0]["chunks"] >= 1 and data["items"][0]["language"] == "apex"
    path = data["items"][0]["path"]
    _, f = get_json(server, f"/api/file?path={path}")
    assert f["lines"] > 5 and f["chunks"][0]["start_line"] == 1 and f["chunks"][0]["embedded"] is True
    assert any(s["name"] == "AccountService.getAccounts" for s in f["symbols"])
    assert f["entity"]["type"] == "file"
    _, c = get_json(server, f"/api/chunk?id={f['chunks'][0]['id']}")
    assert c["path"] == path and "class AccountService" in c["text"]


def test_query_playground_and_log(server):
    status, r = post_json(server, "/api/query", {"text": "how are accounts synced to billing", "budget": 800, "retriever": "auto"})
    assert status == 200 and r["retriever"] == "hybrid" and r["pack"]["chunks"]
    decisions = {c["decision"] for c in r["candidates"]}
    assert "included" in decisions
    assert all(c["rank"] >= 1 and "score" in c for c in r["candidates"])
    assert r["pack"]["facts"] and "AccountService" in r["markdown"]
    status, r2 = post_json(server, "/api/query", {"text": "how are accounts synced to billing", "retriever": "bm25", "facts": False, "no_log": True})
    assert r2["retriever"] == "bm25" and r2["pack"]["facts"] == []
    status, r3 = post_json(server, "/api/query", {"text": ""})
    assert r3["error"] == "empty query"
    _, log = get_json(server, "/api/log")
    assert len(log) == 1 and log[0]["text"] == "how are accounts synced to billing" and log[0]["filters_json"]
    _, hits = get_json(server, "/api/search?q=BillingClient")
    assert hits and hits[0]["path"].endswith((".cls", ".md", ".trigger"))
