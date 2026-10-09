"""JSON views over the index for the UI (and anything else that wants them)."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..compile import compile_pack, render_json, render_markdown
from ..config import Config
from ..graph.facts import describe, describe_json, facts_for_query, is_test
from ..retrieve import QueryFilters, SearchOptions, search_detailed
from ..retrieve.bm25 import file_importance, is_test_path
from ..stats import compute_stats
from ..store.db import Database, Entity

ENTITY_TYPES = ["symbol", "file", "object", "field", "flow", "component", "service", "team", "person", "rule", "recordtype", "permissionset", "layout"]


class Api:
    """Holds one shared connection plus lazily built embedder/reranker."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.lock = threading.Lock()
        self._embedder = None
        self._embedder_tried = False
        self._reranker = None
        self._reranker_tried = False

    # -- model helpers -----------------------------------------------------

    def embedder(self):
        if not self._embedder_tried:
            self._embedder_tried = True
            if self.cfg.embedding.provider != "none":
                from ..retrieve.embed import make_embedder

                try:
                    self._embedder = make_embedder(self.cfg.embedding)
                except Exception:  # missing package or key: degrade to BM25
                    self._embedder = None
        return self._embedder

    def reranker(self):
        if not self._reranker_tried:
            self._reranker_tried = True
            from ..retrieve.rerank import make_reranker

            try:
                self._reranker = make_reranker(self.cfg.rerank, True)
            except Exception:
                self._reranker = None
        return self._reranker

    # -- overview ------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        db = self.db
        sources = []
        chunk_rows = {
            r["source_id"]: r
            for r in db.conn.execute(
                "SELECT source_id, COUNT(*) AS chunks, COUNT(DISTINCT path) AS files, SUM(token_count) AS tokens FROM chunks GROUP BY source_id"
            )
        }
        for row in db.list_sources():
            c = chunk_rows.get(row["id"])
            cfg_src = next((s for s in self.cfg.sources if s.id == row["id"]), None)
            sources.append({
                "id": row["id"], "type": row["type"], "bucket": row["bucket"],
                "paths": cfg_src.paths if cfg_src else json.loads(row["config_json"]).get("paths", []),
                "files": int(c["files"]) if c else 0, "chunks": int(c["chunks"]) if c else 0,
                "tokens": int(c["tokens"]) if c and c["tokens"] else 0,
                "ingested_commit": row["ingested_commit"], "ingested_at": row["ingested_at"],
                "manual": bool(row["manual"]), "stale_after_days": row["stale_after_days"],
            })
        counts = db.graph_counts()
        edges = {r["kind"]: r["n"] for r in db.conn.execute("SELECT kind, COUNT(*) AS n FROM edges GROUP BY kind ORDER BY n DESC")}
        langs = {r["value"]: r["n"] for r in db.conn.execute("SELECT value, COUNT(*) AS n FROM chunk_meta WHERE key = 'language' GROUP BY value ORDER BY n DESC")}
        kinds = {r["value"]: r["n"] for r in db.conn.execute("SELECT value, COUNT(*) AS n FROM chunk_meta WHERE key = 'kind' GROUP BY value ORDER BY n DESC")}
        tok = [r["token_count"] for r in db.conn.execute("SELECT token_count FROM chunks ORDER BY token_count")]
        emb_model = db.conn.execute("SELECT model, COUNT(*) AS n FROM embeddings GROUP BY model ORDER BY n DESC LIMIT 1").fetchone()
        head = db.conn.execute("SELECT ingested_commit FROM sources WHERE ingested_commit IS NOT NULL LIMIT 1").fetchone()
        queries = int(db.conn.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()["n"])
        return {
            "repo": self.cfg.repo_name,
            "repo_root": str(self.cfg.repo_root),
            "head_commit": head["ingested_commit"] if head else None,
            "db_path": str(self.cfg.db_path),
            "db_bytes": self.cfg.db_path.stat().st_size if self.cfg.db_path.exists() else 0,
            "sources": sources,
            "chunks": {
                "total": len(tok), "tokens": sum(tok),
                "p50": tok[len(tok) // 2] if tok else 0, "p90": tok[int(len(tok) * 0.9)] if tok else 0, "max": tok[-1] if tok else 0,
                "by_language": langs, "by_kind": kinds,
            },
            "entities": {k[7:]: v for k, v in counts.items() if k.startswith("entity:")},
            "entities_total": counts.get("entities", 0),
            "edges": edges,
            "edges_total": counts.get("edges", 0),
            "mentions": counts.get("mentions", 0),
            "embeddings": {"model": emb_model["model"] if emb_model else None, "count": int(emb_model["n"]) if emb_model else 0,
                            "provider": self.cfg.embedding.provider},
            "queries_logged": queries,
            "config": {
                "parser": self.cfg.chunking.parser,
                "chunking": {"target_tokens": self.cfg.chunking.target_tokens, "max_tokens": self.cfg.chunking.max_tokens, "min_tokens": self.cfg.chunking.min_tokens},
                "retrieval": self.cfg.retrieval.__dict__,
                "rerank_enabled": self.cfg.rerank.enabled,
                "budget_tokens_default": self.cfg.budget_tokens_default,
            },
            "findings": [f.__dict__ for f in compute_stats(self.cfg, db).findings],
        }

    # -- entities ------------------------------------------------------------

    def entities(self, type_: str | None, q: str | None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        where = []
        params: list[Any] = []
        if type_:
            where.append("e.type = ?")
            params.append(type_)
        if q:
            where.append("e.lname LIKE ? ESCAPE '\\'")
            params.append("%" + q.lower().replace("%", "\\%").replace("_", "\\_") + "%")
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self.db.conn.execute(f"SELECT COUNT(*) AS n FROM entities e{sql_where}", params).fetchone()["n"])
        rows = self.db.conn.execute(
            f"""
            SELECT e.id, e.type, e.name, e.attrs_json,
                   (SELECT COUNT(*) FROM edges x WHERE x.dst_id = e.id) AS indeg,
                   (SELECT COUNT(*) FROM edges x WHERE x.src_id = e.id) AS outdeg
            FROM entities e{sql_where}
            ORDER BY indeg DESC, e.type, e.name LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        items = []
        for r in rows:
            a = json.loads(r["attrs_json"])
            items.append({
                "id": r["id"], "type": r["type"], "name": r["name"], "kind": a.get("kind"), "path": a.get("path") or (r["name"] if r["type"] == "file" else None),
                "in": r["indeg"], "out": r["outdeg"], "test": bool(_TEST_RE.search(a.get("path") or "")) if r["type"] == "symbol" else (is_test_path(r["name"]) if r["type"] == "file" else False),
                "language": a.get("language"), "label": a.get("label"),
            })
        types = {r["type"]: r["n"] for r in self.db.conn.execute("SELECT type, COUNT(*) AS n FROM entities GROUP BY type")}
        return {"total": total, "items": items, "types": types}

    def entity(self, entity_id: str) -> dict[str, Any] | None:
        ent = self.db.get_entity(entity_id)
        if ent is None:
            return None
        d = describe_json(self.db, ent)
        path = ent.attrs.get("path") or (ent.name if ent.type == "file" else None)
        chunks = []
        if path:
            start = ent.attrs.get("start_line")
            end = ent.attrs.get("end_line")
            rows = self.db.conn.execute(
                "SELECT id, path, start_line, end_line, token_count, bucket, source_id FROM chunks WHERE path = ? ORDER BY start_line", (path,)
            )
            for r in rows:
                if start and end and (r["end_line"] < start or r["start_line"] > end):
                    continue
                chunks.append({"id": r["id"], "path": r["path"], "start_line": r["start_line"], "end_line": r["end_line"], "tokens": r["token_count"], "bucket": r["bucket"], "source": r["source_id"], "heading": self._heading(r["id"])})
        mention_chunks = [
            {"id": r["id"], "path": r["path"], "start_line": r["start_line"], "end_line": r["end_line"], "tokens": r["token_count"], "bucket": r["bucket"], "source": r["source_id"], "heading": self._heading(r["id"])}
            for r in self.db.conn.execute(
                "SELECT c.id, c.path, c.start_line, c.end_line, c.token_count, c.bucket, c.source_id FROM mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.entity_id = ? ORDER BY c.path, c.start_line LIMIT 50",
                (entity_id,),
            )
        ]
        d["path"] = path
        d["chunks"] = chunks
        d["mention_chunks"] = mention_chunks
        d["is_test"] = is_test(ent)
        return d

    def _heading(self, chunk_id: str) -> str:
        r = self.db.conn.execute("SELECT value FROM chunk_meta WHERE chunk_id = ? AND key = 'heading'", (chunk_id,)).fetchone()
        return r["value"] if r else ""

    # Structural edges (a class contains its methods, a file declares its
    # classes, who authored what) swamp the picture; the graph view follows
    # the semantic ones and lists the structural ones below it.
    STRUCTURAL = ("contains", "declares", "authored", "part_of", "member_of", "shows")

    def neighborhood(self, entity_id: str, depth: int = 2, max_nodes: int = 40, first: int = 18, second: int = 4) -> dict[str, Any]:
        root = self.db.get_entity(entity_id)
        if root is None:
            return {"nodes": [], "edges": []}
        conn = self.db.conn

        skip = ",".join("?" * len(self.STRUCTURAL))

        def neighbours(eid: str, limit: int) -> list[tuple[str, int]]:
            rows = conn.execute(
                f"""
                SELECT n.id, (SELECT COUNT(*) FROM edges x WHERE x.dst_id = n.id) AS indeg FROM (
                    SELECT dst_id AS id FROM edges WHERE src_id = ? AND kind NOT IN ({skip})
                    UNION SELECT src_id AS id FROM edges WHERE dst_id = ? AND kind NOT IN ({skip})
                ) m JOIN entities n ON n.id = m.id ORDER BY indeg DESC LIMIT ?
                """,
                (eid, *self.STRUCTURAL, eid, *self.STRUCTURAL, limit),
            )
            return [(r["id"], r["indeg"]) for r in rows]

        chosen: dict[str, int] = {entity_id: 0}
        parent: dict[str, str] = {}
        frontier = deque([(entity_id, 0)])
        while frontier and len(chosen) < max_nodes:
            eid, d = frontier.popleft()
            if d >= depth:
                continue
            for nid, _ in neighbours(eid, first if d == 0 else second):
                if nid not in chosen:
                    chosen[nid] = d + 1
                    parent.setdefault(nid, eid)
                    frontier.append((nid, d + 1))
                    if len(chosen) >= max_nodes:
                        break
        ids = list(chosen)
        marks = ",".join("?" * len(ids))
        nodes = [
            {"id": r["id"], "name": r["name"], "type": r["type"], "depth": chosen[r["id"]], "parent": parent.get(r["id"]),
             "kind": json.loads(r["attrs_json"]).get("kind"), "indeg": r["indeg"]}
            for r in conn.execute(
                f"SELECT e.id, e.name, e.type, e.attrs_json, (SELECT COUNT(*) FROM edges x WHERE x.dst_id = e.id) AS indeg FROM entities e WHERE e.id IN ({marks})", ids
            )
        ]
        edges = [
            {"src": r["src_id"], "dst": r["dst_id"], "kind": r["kind"]}
            for r in conn.execute(
                f"SELECT src_id, dst_id, kind FROM edges WHERE src_id IN ({marks}) AND dst_id IN ({marks}) AND kind NOT IN ({skip})",
                ids + ids + list(self.STRUCTURAL),
            )
        ]
        return {"root": entity_id, "nodes": nodes, "edges": edges, "truncated": len(chosen) >= max_nodes}

    # -- files ---------------------------------------------------------------

    def files(self, q: str | None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if q:
            where = " WHERE c.path LIKE ? ESCAPE '\\'"
            params.append("%" + q.replace("%", "\\%").replace("_", "\\_") + "%")
        rows = self.db.conn.execute(
            f"""
            SELECT c.path, c.source_id, COUNT(*) AS chunks, SUM(c.token_count) AS tokens, MIN(c.commit_sha) AS commit_sha
            FROM chunks c{where} GROUP BY c.path ORDER BY c.path LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        total = int(self.db.conn.execute(f"SELECT COUNT(DISTINCT c.path) AS n FROM chunks c{where}", params).fetchone()["n"])
        items = []
        for r in rows:
            f = self.db.get_entity("file:" + r["path"])
            a = f.attrs if f else {}
            items.append({
                "path": r["path"], "source": r["source_id"], "chunks": r["chunks"], "tokens": r["tokens"],
                "language": a.get("language"), "kind": a.get("kind"), "owner": a.get("owner"), "service": a.get("service"),
                "last_changed": a.get("last_changed"), "last_author": a.get("last_author"), "commits": a.get("commits"),
                "test": is_test_path(r["path"]),
            })
        return {"total": total, "items": items}

    def file(self, path: str) -> dict[str, Any] | None:
        rows = self.db.conn.execute(
            "SELECT id, start_line, end_line, token_count, bucket, source_id, hash FROM chunks WHERE path = ? ORDER BY start_line", (path,)
        ).fetchall()
        if not rows:
            return None
        try:
            text = (self.cfg.repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        embedded = {r["chunk_id"] for r in self.db.conn.execute("SELECT chunk_id FROM embeddings WHERE chunk_id IN (%s)" % ",".join("?" * len(rows)), [r["id"] for r in rows])}
        chunks = [
            {"id": r["id"], "start_line": r["start_line"], "end_line": r["end_line"], "tokens": r["token_count"], "bucket": r["bucket"],
             "source": r["source_id"], "heading": self._heading(r["id"]), "embedded": r["id"] in embedded}
            for r in rows
        ]
        symbols = [
            {"id": r["id"], "name": r["name"], "kind": json.loads(r["attrs_json"]).get("kind"),
             "start_line": json.loads(r["attrs_json"]).get("start_line"), "end_line": json.loads(r["attrs_json"]).get("end_line")}
            for r in self.db.conn.execute(
                "SELECT id, name, attrs_json FROM entities WHERE type = 'symbol' AND json_extract(attrs_json, '$.path') = ? ORDER BY json_extract(attrs_json, '$.start_line')", (path,)
            )
        ]
        f = self.db.get_entity("file:" + path)
        return {
            "path": path, "text": text, "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "chunks": chunks, "symbols": symbols,
            "entity": describe_json(self.db, f) if f else None,
            "test": is_test_path(path),
        }

    def chunk(self, chunk_id: str) -> dict[str, Any] | None:
        rec = self.db.get_chunk(chunk_id)
        if rec is None:
            return None
        return {"id": rec.id, "path": rec.path, "start_line": rec.start_line, "end_line": rec.end_line, "tokens": rec.token_count,
                "bucket": rec.bucket, "source": rec.source_id, "text": rec.text, "meta": rec.meta, "terms": rec.terms, "commit": rec.commit_sha}

    # -- query ---------------------------------------------------------------

    def query(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "").strip()
        if not text:
            return {"error": "empty query"}
        budget = int(body.get("budget") or self.cfg.budget_tokens_default)
        retriever = str(body.get("retriever") or "auto")
        want_rerank = bool(body.get("rerank")) if body.get("rerank") is not None else self.cfg.rerank.enabled
        filters = QueryFilters(buckets=list(body.get("buckets") or []), sources=list(body.get("sources") or []), path_prefixes=list(body.get("paths") or []))
        opts = SearchOptions(
            limit=int(body.get("limit") or self.cfg.retrieval.candidates),
            test_penalty=self.cfg.retrieval.test_path_penalty,
            importance_boost=self.cfg.retrieval.importance_boost,
            embedder=self.embedder() if retriever != "bm25" else None,
            reranker=self.reranker() if want_rerank else None,
            rerank_top_n=self.cfg.rerank.top_n,
            near_duplicate_cosine=self.cfg.retrieval.near_duplicate_cosine,
            rrf_k=self.cfg.retrieval.rrf_k,
            retriever=retriever,
        )
        started = time.monotonic()
        result = search_detailed(self.db, text, filters, opts)
        search_ms = int((time.monotonic() - started) * 1000)
        cap = self.cfg.retrieval.max_chunks_per_file
        draft = compile_pack(text, result.hits, budget, max_chunks_per_file=cap)
        facts = facts_for_query(self.db, text, draft.chunks) if body.get("facts", True) else []
        trace: list[tuple[str, str]] = []
        pack = compile_pack(text, result.hits, budget, facts=facts, max_chunks_per_file=cap, trace=trace)
        total_ms = int((time.monotonic() - started) * 1000)
        decisions = dict(trace)
        importance = file_importance(self.db)
        candidates = [
            {
                "id": h.id, "rank": h.rank, "score": round(h.score, 4), "path": h.path, "start_line": h.start_line, "end_line": h.end_line,
                "tokens": h.token_count, "bucket": h.bucket, "source": h.source_id, "heading": h.heading,
                "decision": decisions.get(h.id, "not_reached"), "test": is_test_path(h.path), "importance": importance.get(h.path, 0),
            }
            for h in result.hits
        ]
        if not body.get("no_log"):
            self.db.log_query(text, {"buckets": filters.buckets, "sources": filters.sources, "paths": filters.path_prefixes, "client": body.get("client", "ui")},
                              result.mode, len(result.hits), len(pack.chunks), pack.used_tokens, pack.budget_tokens, pack.dropped_over_budget,
                              [h.source_id for h in result.hits], total_ms)
        return {
            "query": text, "retriever": result.retriever, "mode": result.mode, "search_ms": search_ms, "total_ms": total_ms,
            "near_duplicates_dropped": result.dropped_near_duplicates,
            "pack": render_json(pack), "markdown": render_markdown(pack), "candidates": candidates,
            "knobs": {"budget": budget, "max_chunks_per_file": cap, "test_path_penalty": self.cfg.retrieval.test_path_penalty,
                      "importance_boost": self.cfg.retrieval.importance_boost, "reserve_ratio": 0.1},
        }

    def search(self, text: str, limit: int = 10, buckets: list[str] | None = None, paths: list[str] | None = None, retriever: str = "auto") -> list[dict[str, Any]]:
        """Ranked chunk references with the full retrieval pipeline (no pack)."""
        filters = QueryFilters(buckets=list(buckets or []), path_prefixes=list(paths or []))
        opts = SearchOptions(
            limit=max(limit, 20), test_penalty=self.cfg.retrieval.test_path_penalty, importance_boost=self.cfg.retrieval.importance_boost,
            embedder=self.embedder() if retriever != "bm25" else None, near_duplicate_cosine=self.cfg.retrieval.near_duplicate_cosine,
            rrf_k=self.cfg.retrieval.rrf_k, retriever=retriever,
        )
        result = search_detailed(self.db, text, filters, opts)
        return [{"id": h.id, "path": h.path, "start_line": h.start_line, "end_line": h.end_line, "tokens": h.token_count,
                 "heading": h.heading, "score": round(h.score, 4), "bucket": h.bucket} for h in result.hits[:limit]]

    def query_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM query_log ORDER BY id DESC LIMIT ?", (limit,))]

    def search_chunks(self, q: str, limit: int = 30) -> list[dict[str, Any]]:
        from ..retrieve.bm25 import bm25_search_mode

        hits, mode = bm25_search_mode(self.db, q, None, limit)
        return [{"id": h.id, "path": h.path, "start_line": h.start_line, "end_line": h.end_line, "tokens": h.token_count,
                 "heading": h.heading, "score": round(h.score, 3), "bucket": h.bucket, "mode": mode} for h in hits]


import re as _re

_TEST_RE = _re.compile(r"(^|/)(test|tests|spec|specs|__tests__|testing)(/|$)|(Test|Tests|Spec|IT)\.\w+$")
