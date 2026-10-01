"""SQLite store. One file, no server, idempotent schema setup."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


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


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------

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
            INSERT INTO chunks(id, source_id, path, bucket, text, start_line, end_line,
                               commit_sha, file_mtime, token_count, hash, terms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.source_id,
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
        total = 0
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            marks = ",".join("?" * len(batch))
            # chunk_meta rows go with the chunk via ON DELETE CASCADE.
            cur = self.conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", batch)
            total += cur.rowcount
        return total

    def delete_chunks_for_source(self, source_id: str) -> int:
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
        )
