-- ctxgraph SQLite schema (schema_version 1).
-- The graph tables (entities, edges, mentions) and embeddings exist from day
-- one so later milestones add data, not migrations.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    bucket           TEXT NOT NULL,
    config_json      TEXT NOT NULL,
    ingested_commit  TEXT,
    ingested_at      TEXT,
    manual           INTEGER NOT NULL DEFAULT 0,
    stale_after_days INTEGER,
    last_verified    TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    rid         INTEGER PRIMARY KEY,
    id          TEXT NOT NULL UNIQUE,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    text        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    commit_sha  TEXT,
    file_mtime  REAL,
    token_count INTEGER NOT NULL,
    hash        TEXT NOT NULL,
    terms       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_path   ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_bucket ON chunks(bucket);
CREATE INDEX IF NOT EXISTS idx_chunks_hash   ON chunks(hash);

CREATE TABLE IF NOT EXISTS chunk_meta (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (chunk_id, key)
);

CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(type, name);

CREATE TABLE IF NOT EXISTS edges (
    src_id     TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id     TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (src_id, dst_id, kind)
);

CREATE TABLE IF NOT EXISTS mentions (
    chunk_id  TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, entity_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    vector   BLOB NOT NULL,
    model    TEXT NOT NULL
);

-- BM25 full-text index kept in sync with chunks via triggers.
-- Underscore is a token character so identifiers like ad_decision survive.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    path,
    terms,
    content='chunks',
    content_rowid='rid',
    tokenize='porter unicode61 tokenchars ''_'''
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, path, terms)
        VALUES (new.rid, new.text, new.path, new.terms);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, path, terms)
        VALUES ('delete', old.rid, old.text, old.path, old.terms);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, path, terms)
        VALUES ('delete', old.rid, old.text, old.path, old.terms);
    INSERT INTO chunks_fts(rowid, text, path, terms)
        VALUES (new.rid, new.text, new.path, new.terms);
END;
