# ctxgraph — Technical Design (v0.1)

This is the working design. Sections marked **[M1]** are implemented; the
rest is the plan. Deviations from the original draft are called out inline.

## 1. Summary

ctxgraph is a git-native context layer for AI agents. It ingests a repository
and its surrounding documents, organises them into three context buckets
(knowledge, expertise, norms), compiles bounded context packs on demand, and
serves them to any agent over a CLI and MCP. Compiled context is committed to
the repo so it versions, diffs and reviews like code.

Assumptions: Python 3.11+, SQLite with FTS5 (ships with CPython), one repo
per instance.

## 2. Goals

- Give coding and operational agents team-specific context without each
  agent rebuilding it.
- Retrieval that is filterable (bucket, team, service, path, time range) and
  bounded (hard token budget).
- Per-source freshness tracking, visible and enforceable.
- Zero infrastructure: runs locally, state lives in the repo and a local
  SQLite file.
- Works on any codebase without installing language toolchains.
- Measurable: a built-in eval set reports retrieval precision/recall per change.

## 3. Non-goals (v0.1)

- Agent memory (what an agent learned from doing tasks). See §15.
- Multi-repo federation.
- Hosted service, auth, multi-tenancy.
- Automatic ingestion of SaaS sources. v0.1 accepts exported markdown only.

## 4. Core concepts

- **Source**: a configured input (paths/globs) with a type and a bucket.
- **Bucket**: `knowledge` (what exists), `expertise` (what was learned),
  `norms` (how the team works).
- **Chunk**: a unit of retrievable text with path, line range, commit,
  breadcrumb and metadata.
- **Entity**: a node in the context graph (`service`, `module`, `team`,
  `person`, `doc`, `incident`).
- **Context pack**: the compiled output of a query: a bounded, deduplicated,
  ordered set of chunks plus graph facts, rendered as markdown or JSON.

## 5. Architecture **[M1 skeleton]**

```
ctxgraph.yaml ──► ingest ──► store (SQLite: chunks + FTS5, graph tables, embeddings)
                                │
                query ──► retrieve (filters → BM25 [+ embeddings, RRF, rerank])
                                │
                           compile (dedupe, budget, render)
                                │
                     ┌──────────┴──────────┐
                   CLI                  MCP server
                                │
                        context/ (committed markdown)
```

## 6. Configuration **[M1]**

See README for the schema. Notable decisions:

- A file belongs to the first source whose paths match it. This makes
  catch-all sources safe and keeps chunk ids stable.
- `ctxgraph init` detects Salesforce DX projects (`sfdx-project.json`) and
  adds Apex, LWC/Aura/Visualforce and metadata sources. Other ecosystems get
  a generic `code` source keyed on file extension.
- The ownership and dependency sources (§8) are unchanged from the draft and
  land in milestone 2.

## 7. Data model (SQLite) **[M1]**

```sql
sources(id PK, type, bucket, config_json, ingested_commit, ingested_at,
        manual, stale_after_days, last_verified)
chunks(rid INTEGER PK, id UNIQUE, source_id FK, path, bucket, text,
       start_line, end_line, commit_sha, file_mtime, token_count, hash, terms)
chunk_meta(chunk_id FK, key, value)       -- heading, kind, language, doc_type
entities(id PK, type, name, attrs_json)   -- M2
edges(src_id, dst_id, kind, attrs_json)   -- M2
mentions(chunk_id, entity_id)             -- M2
embeddings(chunk_id PK, vector, model)    -- M3
chunks_fts: FTS5 over (text, path, terms), porter + unicode61, '_' kept
```

Chunk ids are hashes of (source, path, text), so re-ingestion is idempotent
and unchanged chunks are never rewritten or re-embedded. `terms` holds the
split sub-words of compound identifiers (`getAccounts` → `get accounts`) so
BM25 matches natural-language queries against code.

## 8. Ingestion **[M1 for markdown, code, text]**

1. List repo files via `git ls-files` (honours `.gitignore`); walk the tree
   outside git. Resolve each source's globs minus excludes.
2. Load per type:
   - `markdown`: split by heading, keep the breadcrumb in the chunk text,
     merge tiny sections, window sections over `max_tokens` by paragraph
     with ~10% overlap. Fenced code is never split.
   - `code`: a string/comment-masking scanner tracks `{}` nesting and names
     declaration blocks (class, trigger, method, property, function) from
     their header. Files and classes that fit the budget stay whole; larger
     ones split by member with a `Class > method` breadcrumb. Python splits
     on `def`/`class` by indentation. Unknown languages fall back to text.
     *Deviation from the draft*: this replaces tree-sitter for v0.1 so no
     native parsers are needed; tree-sitter can slot in behind the same
     function later for languages that need it.
   - `text`: paragraph windows.
   - `ownership`, `edges` (M2): entities and edges, no chunks.
3. Skip binary files; record skips in the ingest report.
4. Hash, skip unchanged chunks, delete chunks that no longer exist, drop
   sources removed from the config. Record `ingested_commit` per source.
5. (M5) Write compiled `context/`.

`ctxgraph ingest` is incremental by default; `--full` rebuilds.

## 9. Retrieval and compilation **[M1 for steps 1, 2, 6, 7]**

Query input: free text plus `buckets`, `sources`, `paths`, later `teams`,
`services`, `since_commit` / `since_days`, and `budget_tokens`.

1. Hard filters in SQL.
2. BM25 top-k (k=50). All terms required, fallback to any term.
3. (M3) Embedding top-k, merged with reciprocal rank fusion.
4. (M3) Optional rerank of the top 30.
5. (M2) Graph expansion: one-hop neighbours of mentioned services/teams as
   structured facts.
6. Dedupe by content hash (M3: near-duplicate by cosine > 0.95).
7. Fill the budget in rank order; 10% reserved for facts and citations.

Output is markdown (sections Facts, Norms, Expertise, Knowledge; each chunk
cites `path#Lstart-end (commit)`) or JSON with the same structure.

## 10. Committed context (`context/`) — M5

`context/INDEX.md`, `context/services/<name>.md`, `context/norms.md`,
`context/STATUS.md`, regenerated on ingest and committed so agents without
MCP can read them and changes diff in PRs.

## 11. Freshness — M5

Sources store `ingested_commit`; `ctxgraph status` compares against HEAD.
Manual sources go stale after `stale_after_days` unless `ctxgraph verify`
updates `last_verified`. Packs carry a `stale_sources` warning block. A CI
step fails when `context/` is out of date.

## 12. Interfaces

CLI **[M1: init, ingest, query]**

```
ctxgraph init [--path DIR] [--force]
ctxgraph ingest [--full]
ctxgraph query "<text>" [-b bucket] [-s source] [-p path-prefix] [--budget N] [--limit K] [--json]
ctxgraph status                      # M5
ctxgraph verify <source_id>          # M5
ctxgraph graph <service>             # M2
ctxgraph eval                        # M7
```

MCP server (`ctxgraph serve`) — M6: `get_context`, `get_service`,
`get_owner`, `list_stale_sources`, `search`.

## 13. Evaluation — M7

`evals/questions.yaml` with expected paths and entities per question;
`ctxgraph eval` reports path recall@budget, entity recall, pack size and
latency, and fails CI below a threshold. Add the question set for the target
repo before milestone 3 so retrieval changes are measured from the start.

## 14. Milestones

1. **Done.** Config loader, SQLite schema, markdown + code + text ingest,
   BM25 search with filters, budgeted packs, CLI `init`/`ingest`/`query`.
2. Graph: ownership and edges ingest, entities, `get_service`, `graph`
   command, Facts block in packs, service attribution of code chunks.
3. Hybrid retrieval: local embeddings, RRF, rerank toggle, near-dup dedupe.
4. Code ingest hardening: tree-sitter where the heuristic splitter is not
   enough; Salesforce metadata XML parsed into entities (objects, fields,
   flows) instead of text.
5. Freshness: `status`, `verify`, stale warnings, `context/` compile, hook.
6. MCP server, tested against Claude Code and Cursor.
7. Evals: question set for the target repo, `eval` command, CI check.

## 15. Integration point with memvine

Context is organisational truth; memory is what an agent learned from tasks.
ctxgraph never writes task-derived learnings; memvine never ingests docs or
ownership. memvine entries may reference ctxgraph chunk ids or entity ids,
and `get_context` accepts an optional `memory_hints` list to boost chunks a
memory points to. Nothing else couples them in v0.1.

## 16. Open questions

- Chunking for large generated or vendored code: `exclude` covers the common
  directories; add `.ctxignore` if teams need it outside the config.
- Service attribution when a file matches no ownership path: `unowned`
  pseudo-service surfaced in `status`.
- Rerank cost: default on for MCP, off for CLI?
- Commit `context/` or generate in CI? Default commit; revisit if noisy.
- Salesforce: should profiles/permission sets be indexed at all (large, low
  signal) or summarised into graph facts?
