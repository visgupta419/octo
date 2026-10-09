"""Ingestion: resolve sources, chunk files, write the store incrementally.

Chunk ids are content hashes of (source, path, text), so re-running ingest
on an unchanged tree is a no-op and only changed chunks are rewritten.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..config import Config
from ..gitutil import head_commit
from ..paths import list_repo_files, resolve_files
from ..store.db import ChunkRecord, Database
from ..tokens import count_tokens
from .chunk import RawChunk
from .loaders import loader_for, source_file_filter

BINARY_SNIFF_BYTES = 8192


def chunk_id(source_id: str, path: str, text: str) -> str:
    h = hashlib.sha256()
    for part in (source_id, path, text):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


@dataclass
class SourceReport:
    source_id: str
    type: str
    bucket: str
    files: int = 0
    added: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def chunks(self) -> int:
        return self.added + self.unchanged


@dataclass
class IngestReport:
    head_commit: str | None
    full: bool
    sources: list[SourceReport] = field(default_factory=list)
    removed_sources: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(s.added for s in self.sources)

    @property
    def removed(self) -> int:
        return sum(s.removed for s in self.sources)

    @property
    def unchanged(self) -> int:
        return sum(s.unchanged for s in self.sources)

    @property
    def chunks(self) -> int:
        return sum(s.chunks for s in self.sources)


def _read_text(path) -> tuple[str | None, str | None]:
    """Return (text, skip_reason)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {exc.strerror or exc}"
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None, "binary"
    return data.decode("utf-8", errors="replace"), None


@dataclass
class EmbedReport:
    model: str
    embedded: int
    total: int
    seconds: float
    dropped_other_model: int = 0


def embed_chunks(
    cfg: Config, db: Database, embedder, *, batch_size: int | None = None, progress=None
) -> EmbedReport:
    """Embed chunks that have no vector for ``embedder.name`` (incremental).

    ``progress(done, total, seconds)`` is called after every batch, so a CLI
    can show that a long run is alive. Vectors are committed per batch, so
    an interrupted run resumes where it stopped.
    """
    import time

    from ..retrieve.embed import to_blob

    started = time.monotonic()
    dropped = db.delete_embeddings_except(embedder.name)
    pending = db.chunks_missing_embeddings(embedder.name)
    size = batch_size or cfg.embedding.batch_size
    done = 0
    for i in range(0, len(pending), size):
        batch = pending[i : i + size]
        vectors = embedder.embed([text[: cfg.embedding.max_chars] for _, text in batch])
        db.upsert_embeddings((cid, to_blob(v), embedder.name) for (cid, _), v in zip(batch, vectors))
        db.commit()
        done += len(batch)
        if progress is not None:
            progress(done, len(pending), time.monotonic() - started)
    return EmbedReport(
        model=embedder.name, embedded=done, total=db.embedding_count(embedder.name),
        seconds=round(time.monotonic() - started, 1), dropped_other_model=dropped,
    )


def run_ingest(cfg: Config, db: Database, *, full: bool = False, progress=None) -> IngestReport:
    """Index every source. ``progress(stage, done, total)`` reports activity
    (stage is a short label such as "apex: chunking"); done/total may be 0."""
    head = head_commit(cfg.repo_root)
    report = IngestReport(head_commit=head, full=full)
    if progress:
        progress("listing files", 0, 0)
    all_files = list_repo_files(cfg.repo_root)
    claimed: set[str] = set()

    for source in cfg.sources:
        loader = loader_for(source.type)
        keep = source_file_filter(source)
        sr = SourceReport(source_id=source.id, type=source.type, bucket=source.bucket)
        files = [
            f
            for f in resolve_files(all_files, source.paths, cfg.exclude_for(source))
            if f not in claimed and keep(f)
        ]
        claimed.update(files)
        sr.files = len(files)
        max_bytes = cfg.max_file_kb_for(source) * 1024
        db.ensure_source(source.id, source.type, source.bucket, source.to_config_dict())

        if full:
            db.delete_chunks_for_source(source.id)
            existing: set[str] = set()
        else:
            existing = db.chunk_ids_for_source(source.id)

        seen: set[str] = set()
        for n, rel in enumerate(files, start=1):
            fp = cfg.repo_root / rel
            if progress and (n % 50 == 0 or n == len(files)):
                progress(f"{source.id}: chunking", n, len(files))
            try:
                st = fp.stat()
            except OSError as exc:
                sr.skipped.append((rel, f"unreadable: {exc.strerror or exc}"))
                continue
            if max_bytes and st.st_size > max_bytes:
                sr.skipped.append((rel, f"too large ({st.st_size // 1024} KB > max_file_kb {max_bytes // 1024})"))
                continue
            text, reason = _read_text(fp)
            if text is None:
                sr.skipped.append((rel, reason or "unknown"))
                continue
            mtime = st.st_mtime
            raw: RawChunk
            for raw in loader(source, rel, text, cfg.chunking):
                cid = chunk_id(source.id, rel, raw.text)
                if cid in seen:
                    continue
                seen.add(cid)
                if cid in existing:
                    sr.unchanged += 1
                    continue
                meta = {"doc_type": source.type}
                if raw.heading:
                    meta["heading"] = raw.heading
                meta.update(raw.meta)
                db.insert_chunk(
                    ChunkRecord(
                        id=cid,
                        source_id=source.id,
                        path=rel,
                        bucket=source.bucket,
                        text=raw.text,
                        start_line=raw.start_line,
                        end_line=raw.end_line,
                        commit_sha=head,
                        file_mtime=mtime,
                        token_count=count_tokens(raw.text),
                        hash=text_hash(raw.text),
                        terms=raw.terms,
                        meta=meta,
                        repo=cfg.repo_name,
                    )
                )
                sr.added += 1

        sr.removed = db.delete_chunks(existing - seen)
        db.upsert_source(
            source.id,
            source.type,
            source.bucket,
            source.to_config_dict(),
            head,
            source.manual,
            source.stale_after_days,
        )
        db.commit()
        report.sources.append(sr)

    configured = {s.id for s in cfg.sources}
    for row in db.list_sources():
        if row["id"] not in configured:
            db.delete_source(row["id"])
            report.removed_sources.append(row["id"])
    db.commit()
    return report
