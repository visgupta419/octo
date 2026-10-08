"""SQLite store. One file, no server, idempotent schema setup."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "2"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SchemaMismatch(Exception):
    """The index on disk was built by a different schema version."""

    def __init__(self, found: str | None, expected: str):
        self.found, self.expected = found, expected
        super().__init__(
            f"index schema version {found or 'unknown'} != {expected}; "
            "run `ctxgraph ingest` to rebuild it"
        )


@dataclass(frozen=True)
class ChunkRecord:
    id: str
    source_id: str
    path: str
    bucket: str
    text: str
    start_line: int
    end_line: int
    commit_sha: str | None
    file_mtime: float | None
    token_count: int
    hash: str
    meta: dict[str, str]
    terms: str = ""
    repo: str = ""


@dataclass(frozen=True)
class Entity:
    id: str
    type: str
    name: str
    attrs: dict[str, Any]


@dataclass(frozen=True)
class Edge:
    src_id: str
    dst_id: str
    kind: str
    attrs: dict[str, Any]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str, *, on_mismatch: str = "error"):
        """Open (and create) the index.

        ``on_mismatch`` is "error" (raise SchemaMismatch) or "rebuild" (delete
        the stale index file and start fresh; the index is a derived cache).
        """
        self.path = Path(path)
        self.rebuilt = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = self._connect()
        found = self._read_version()
        if found is not None and found != SCHEMA_VERSION:
            if on_mismatch != "rebuild" or str(self.path) == ":memory:":
                self.conn.close()
                raise SchemaMismatch(found, SCHEMA_VERSION)
            self.conn.close()
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    p.unlink()
            self.conn = self._connect()
            self.rebuilt = True
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _read_version(self) -> str | None:
        """Schema version of an existing index, or None for an empty database."""
        tables = {
            r["name"]
            for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not tables:
            return None
        if "meta" not in tables:
            return "0"
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return row["value"] if row else "0"

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    # -- sources -----------------------------------------------------------

    def upsert_source(
        self,
        source_id: str,
        type_: str,
        bucket: str,
        config: dict[str, Any],
        ingested_commit: str | None,
        manual: bool,
        stale_after_days: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sources(id, type, bucket, config_json, ingested_commit, ingested_at,
                                manual, stale_after_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                bucket = excluded.bucket,
                config_json = excluded.config_json,
                ingested_commit = excluded.ingested_commit,
                ingested_at = excluded.ingested_at,
                manual = excluded.manual,
                stale_after_days = excluded.stale_after_days
            """,
            (
                source_id,
                type_,
                bucket,
                json.dumps(config, sort_keys=True),
                ingested_commit,
                utcnow_iso(),
                int(manual),
                stale_after_days,
            ),
        )

    def ensure_source(self, source_id: str, type_: str, bucket: str, config: dict[str, Any]) -> None:
        """Register a source row (without an ingest stamp) so chunks can reference it."""
        self.conn.execute(
            """
            INSERT INTO sources(id, type, bucket, config_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type, bucket = excluded.bucket, config_json = excluded.config_json
            """,
            (source_id, type_, bucket, json.dumps(config, sort_keys=True)),
        )

    def list_sources(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM sources ORDER BY id"))

    def delete_source(self, source_id: str) -> int:
        removed = self.delete_chunks_for_source(source_id)
        self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return removed

    # -- chunks ------------------------------------------------------------

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        rows = self.conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,))
        return {r["id"] for r in rows}

    def insert_chunk(self, rec: ChunkRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO chunks(id, source_id, repo, path, bucket, text, start_line, end_line,
                               commit_sha, file_mtime, token_count, hash, terms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.source_id,
                rec.repo,
                rec.path,
                rec.bucket,
                rec.text,
                rec.start_line,
                rec.end_line,
                rec.commit_sha,
                rec.file_mtime,
                rec.token_count,
                rec.hash,
                rec.terms,
            ),
        )
        if rec.meta:
            self.conn.executemany(
                "INSERT OR REPLACE INTO chunk_meta(chunk_id, key, value) VALUES (?, ?, ?)",
                [(rec.id, k, v) for k, v in rec.meta.items()],
            )

    def delete_chunks(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        self._vec_cache = None
        total = 0
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            marks = ",".join("?" * len(batch))
            # chunk_meta rows go with the chunk via ON DELETE CASCADE.
            cur = self.conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", batch)
            total += cur.rowcount
        return total

    def delete_chunks_for_source(self, source_id: str) -> int:
        self._vec_cache = None
        cur = self.conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
        return cur.rowcount

    def count_chunks(self, source_id: str | None = None) -> int:
        if source_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (source_id,)
            ).fetchone()
        return int(row["n"])

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        row = self.conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            return None
        meta = {
            r["key"]: r["value"]
            for r in self.conn.execute(
                "SELECT key, value FROM chunk_meta WHERE chunk_id = ?", (chunk_id,)
            )
        }
        return ChunkRecord(
            id=row["id"],
            source_id=row["source_id"],
            path=row["path"],
            bucket=row["bucket"],
            text=row["text"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            commit_sha=row["commit_sha"],
            file_mtime=row["file_mtime"],
            token_count=row["token_count"],
            hash=row["hash"],
            meta=meta,
            terms=row["terms"],
            repo=row["repo"],
        )

    def chunk_paths(self) -> dict[str, str]:
        """Every indexed path -> the source id that owns it."""
        return {
            r["path"]: r["source_id"]
            for r in self.conn.execute("SELECT DISTINCT path, source_id FROM chunks")
        }

    def iter_chunks(self, source_types: Iterable[str] | None = None):
        """Yield (chunk_id, path, text, meta) for mention scanning."""
        sql = """
            SELECT c.id, c.path, c.text, s.type AS source_type FROM chunks c
            JOIN sources s ON s.id = c.source_id
        """
        params: list[Any] = []
        if source_types:
            types = list(source_types)
            sql += f" WHERE s.type IN ({','.join('?' * len(types))})"
            params = types
        for r in self.conn.execute(sql, params):
            yield r["id"], r["path"], r["text"], r["source_type"]

    # -- graph -------------------------------------------------------------

    def clear_graph(self) -> None:
        self._importance_cache = None
        self.conn.execute("DELETE FROM mentions")
        self.conn.execute("DELETE FROM edges")
        self.conn.execute("DELETE FROM entities")

    def upsert_entity(self, ent: Entity, repo: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO entities(id, type, name, lname, repo, attrs_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                attrs_json = excluded.attrs_json, name = excluded.name, lname = excluded.lname
            """,
            (ent.id, ent.type, ent.name, ent.name.lower(), repo, json.dumps(ent.attrs, sort_keys=True)),
        )

    def add_edge(self, edge: Edge) -> None:
        self.conn.execute(
            """
            INSERT INTO edges(src_id, dst_id, kind, attrs_json) VALUES (?, ?, ?, ?)
            ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET attrs_json = excluded.attrs_json
            """,
            (edge.src_id, edge.dst_id, edge.kind, json.dumps(edge.attrs, sort_keys=True)),
        )

    def add_mentions(self, rows: Iterable[tuple[str, str]]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO mentions(chunk_id, entity_id) VALUES (?, ?)", list(rows)
        )

    @staticmethod
    def _row_entity(r: sqlite3.Row) -> Entity:
        return Entity(id=r["id"], type=r["type"], name=r["name"], attrs=json.loads(r["attrs_json"]))

    def get_entity(self, entity_id: str) -> Entity | None:
        r = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._row_entity(r) if r else None

    def find_entities(self, name: str, type_: str | None = None, limit: int = 20) -> list[Entity]:
        """Case-insensitive lookup by exact name, then by name suffix (Class.method)."""
        lname = name.lower()
        type_sql = " AND type = ?" if type_ else ""
        params: list[Any] = [lname] + ([type_] if type_ else [])
        rows = self.conn.execute(
            f"SELECT * FROM entities WHERE lname = ?{type_sql} ORDER BY type, name LIMIT ?",
            [*params, limit],
        ).fetchall()
        if not rows:
            rows = self.conn.execute(
                f"SELECT * FROM entities WHERE (lname LIKE ? OR lname LIKE ?){type_sql} "
                "ORDER BY length(name), type, name LIMIT ?",
                ["%." + lname, "%/" + lname, *([type_] if type_ else []), limit],
            ).fetchall()
        return [self._row_entity(r) for r in rows]

    def entity_names(self, types: Iterable[str]) -> dict[str, list[Entity]]:
        """lower(name) -> entities, for the given types."""
        types = list(types)
        out: dict[str, list[Entity]] = {}
        rows = self.conn.execute(
            f"SELECT * FROM entities WHERE type IN ({','.join('?' * len(types))})", types
        )
        for r in rows:
            out.setdefault(r["lname"], []).append(self._row_entity(r))
        return out

    def edges_from(self, entity_id: str) -> list[tuple[Edge, Entity]]:
        rows = self.conn.execute(
            """
            SELECT e.src_id, e.dst_id, e.kind, e.attrs_json AS eattrs, n.*
            FROM edges e JOIN entities n ON n.id = e.dst_id WHERE e.src_id = ?
            ORDER BY e.kind, n.name
            """,
            (entity_id,),
        )
        return [
            (Edge(r["src_id"], r["dst_id"], r["kind"], json.loads(r["eattrs"])), self._row_entity(r))
            for r in rows
        ]

    def edges_to(self, entity_id: str) -> list[tuple[Edge, Entity]]:
        rows = self.conn.execute(
            """
            SELECT e.src_id, e.dst_id, e.kind, e.attrs_json AS eattrs, n.*
            FROM edges e JOIN entities n ON n.id = e.src_id WHERE e.dst_id = ?
            ORDER BY e.kind, n.name
            """,
            (entity_id,),
        )
        return [
            (Edge(r["src_id"], r["dst_id"], r["kind"], json.loads(r["eattrs"])), self._row_entity(r))
            for r in rows
        ]

    def in_degree(self, entity_id: str, kinds: tuple[str, ...] = ("references", "calls")) -> int:
        marks = ",".join("?" * len(kinds))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM edges WHERE dst_id = ? AND kind IN ({marks})",
            (entity_id, *kinds),
        ).fetchone()
        return int(row["n"])

    def mentions_of(self, entity_id: str, limit: int = 10) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.path FROM mentions m JOIN chunks c ON c.id = m.chunk_id
            WHERE m.entity_id = ? ORDER BY c.path LIMIT ?
            """,
            (entity_id, limit),
        )
        return [r["path"] for r in rows]

    def graph_counts(self) -> dict[str, int]:
        ents = {
            r["type"]: r["n"]
            for r in self.conn.execute("SELECT type, COUNT(*) AS n FROM entities GROUP BY type")
        }
        edges = int(self.conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"])
        mentions = int(self.conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"])
        return {"entities": sum(ents.values()), "edges": edges, "mentions": mentions, **{f"entity:{k}": v for k, v in ents.items()}}

    # -- embeddings --------------------------------------------------------

    def chunks_missing_embeddings(self, model: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT c.id, c.text FROM chunks c
            LEFT JOIN embeddings e ON e.chunk_id = c.id AND e.model = ?
            WHERE e.chunk_id IS NULL ORDER BY c.path, c.start_line
            """,
            (model,),
        )
        return [(r["id"], r["text"]) for r in rows]

    def upsert_embeddings(self, rows: Iterable[tuple[str, bytes, str]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO embeddings(chunk_id, vector, model) VALUES (?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET vector = excluded.vector, model = excluded.model
            """,
            list(rows),
        )
        self._vec_cache = None

    def delete_embeddings_except(self, model: str) -> int:
        cur = self.conn.execute("DELETE FROM embeddings WHERE model != ?", (model,))
        self._vec_cache = None
        return cur.rowcount

    def embedding_count(self, model: str | None = None) -> int:
        if model is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM embeddings WHERE model = ?", (model,)).fetchone()
        return int(row["n"])

    def load_vectors(self, model: str):
        """(chunk ids, float32 matrix with unit rows) for ``model``; cached."""
        import numpy as np

        cached = getattr(self, "_vec_cache", None)
        if cached is not None and cached[0] == model:
            return cached[1], cached[2]
        ids: list[str] = []
        blobs: list[bytes] = []
        for r in self.conn.execute("SELECT chunk_id, vector FROM embeddings WHERE model = ? ORDER BY chunk_id", (model,)):
            ids.append(r["chunk_id"])
            blobs.append(r["vector"])
        if not ids:
            matrix = np.zeros((0, 0), dtype=np.float32)
        else:
            matrix = np.vstack([np.frombuffer(b, dtype=np.float32) for b in blobs])
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        self._vec_cache = (model, ids, matrix)
        return ids, matrix

    # -- query log ---------------------------------------------------------

    def log_query(
        self,
        text: str,
        filters: dict[str, Any],
        mode: str,
        candidates: int,
        pack_chunks: int,
        used_tokens: int,
        budget: int,
        over_budget: int,
        sources: Iterable[str],
        duration_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO query_log(ts, text, filters_json, mode, candidates, pack_chunks,
                                  used_tokens, budget, over_budget, sources_json, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utcnow_iso(),
                text,
                json.dumps(filters, sort_keys=True),
                mode,
                candidates,
                pack_chunks,
                used_tokens,
                budget,
                over_budget,
                json.dumps(sorted(set(sources))),
                duration_ms,
            ),
        )
        self.conn.commit()
