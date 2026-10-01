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
embeddings(chunk_id PK, vector, model)    -- M4
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
5. (M6) Write compiled `context/`.

`ctxgraph ingest` is incremental by default; `--full` rebuilds.

## 9. Retrieval and compilation **[M1 for steps 1, 2, 6, 7]**

Query input: free text plus `buckets`, `sources`, `paths`, later `teams`,
`services`, `since_commit` / `since_days`, and `budget_tokens`.

1. Hard filters in SQL.
2. BM25 top-k (k=50). All terms required, fallback to any term.
3. (M4) Embedding top-k, merged with reciprocal rank fusion.
4. (M4) Optional rerank of the top 30.
5. (M2) Graph expansion: one-hop neighbours of mentioned services/teams as
   structured facts.
6. Dedupe by content hash (M4: near-duplicate by cosine > 0.95).
7. Fill the budget in rank order; 10% reserved for facts and citations.

Output is markdown (sections Facts, Norms, Expertise, Knowledge; each chunk
cites `path#Lstart-end (commit)`) or JSON with the same structure.

## 10. Committed context (`context/`) — M6

`context/INDEX.md`, `context/services/<name>.md`, `context/norms.md`,
`context/STATUS.md`, regenerated on ingest and committed so agents without
MCP can read them and changes diff in PRs.

## 11. Freshness — M6

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
ctxgraph status                      # M6
ctxgraph verify <source_id>          # M6
ctxgraph graph <service>             # M2
ctxgraph eval                        # M3
```

MCP server (`ctxgraph serve`) — M7: `get_context`, `get_service`,
`get_owner`, `list_stale_sources`, `search`.

## 13. Evaluation — M3

`evals/questions.yaml` with expected paths and entities per question;
`ctxgraph eval` reports path recall@budget, entity recall, pack size and
latency, and fails CI below a threshold. Add the question set for the target
repo before milestone 3 so retrieval changes are measured from the start.

## 14. Lessons applied from Uber's Software Factory

Uber's "Running a Software Factory Efficiently at Uber Scale" (Aug 2026)
decomposes agent spend as users × sessions/user × turns/session ×
requests/turn × tokens/request × price/token. A context engine moves two of
those terms: **requests/turn** (the agent stops searching) and
**tokens/request** (what it carries is small and cache-stable). Their
AI Context Graph (24M nodes, 80M edges, 86 node types, 117 edge types, 30+
systems) turned a 20-minute wrong answer into a 38-second right one. The
ideas below are scoped to a single repo and adopted into the milestones.

1. **Measure turns and tokens, not just recall.** Evals report, per
   question, tokens-to-answer and tool calls with ctxgraph versus without it
   (the grounded/ungrounded A/B in their Figure 10), alongside recall at
   budget. "Cost per correct answer" is the headline number. Build the
   question set from real work: recent PRs, on-call questions, review
   comments on the target repo.
2. **More node and edge types from sources the repo already has.** Uber's
   graph gains its value from joining systems. Within one repo the free
   systems are: git history (commits, authors, co-change edges, file churn,
   recency), code structure (class → method, caller → callee where cheap,
   test → code under test), platform metadata (for Salesforce: object,
   field, flow, trigger, permission set, with edges trigger → object,
   class → object via SOQL/DML, LWC → Apex via `@salesforce/apex` imports,
   field → object), docs → code mentions, and ownership. Milestone 2 grows
   the entity model to cover these, not only service/team.
3. **Graph facts first, chunks second.** A question like "who owns X and
   what calls it" should be answered from edges in one call, with chunks as
   evidence. Packs lead with Facts; `get_service`/`get_owner`-style lookups
   return facts only.
4. **Small tool surface, compact responses.** Their 100+ tool schemas cost
   50-70K tokens per turn before any work. ctxgraph exposes few MCP tools
   with terse schemas, a `search` that returns refs (path, lines, score,
   heading) rather than text, and `get_context` with a hard budget. The CLI
   stays first-class so agents can use it from a shell without any schema in
   context ("CLI tool resolution").
5. **Code-mode friendly.** Expose a Python API and a batch CLI mode
   (`ctxgraph query --batch questions.jsonl`) so an agent script can run
   many lookups in one subprocess and return only a summary to the model.
6. **Cache-stable output.** Prompt-cache reads cost 0.1x; packs must be
   byte-deterministic for identical inputs (stable ordering, stable chunk
   ids, no timestamps in the body) so repeated packs and the committed
   `context/` files hit the prefix cache.
7. **Visibility and anti-patterns for context, like their session
   dashboard.** Log every query (text, filters, hits, tokens, fallback mode)
   in a `query_log` table. `ctxgraph stats` reports: sources never
   retrieved, stale sources that keep getting retrieved, queries that fell
   back to any-term matching, packs that hit the budget ceiling, and the
   largest chunks. Each is paired with a remediation (re-chunk, exclude,
   verify, add a source).
8. **Continuous improvement from traces.** Accept feedback
   (`ctxgraph feedback <chunk_id> --useful/--noise`, and the same over MCP)
   and use it to re-rank and to flag chunks to split or exclude. This is the
   repo-scale version of their "record papercuts, auto-generate skill
   updates".
9. **Pareto-driven choices for any model we introduce.** Embeddings and the
   optional reranker (milestone 3) are picked by the eval harness on cost
   per correct answer and latency, local-first, and can be switched off.

## 15. Milestones

1. **Done.** Config loader, SQLite schema, markdown + code + text ingest,
   BM25 search with filters, budgeted packs, CLI `init`/`ingest`/`query`.
2. Graph: ownership and edges ingest; entities for services, teams, files,
   classes/methods, commits and authors (from git history), and platform
   metadata (Salesforce objects, fields, flows, triggers); edges for
   ownership, co-change, class → object, LWC → Apex, doc → code mentions;
   `graph` command; Facts block first in packs; `query_log` and
   `ctxgraph stats`.
3. Evals (pulled forward): `evals/questions.yaml` built from real work on
   the target repo; `eval` reports recall at budget, tokens-to-answer and
   tool calls grounded vs ungrounded; CI threshold. Deterministic pack
   output verified by a test.
4. Hybrid retrieval: local embeddings, RRF, rerank toggle, near-dup dedupe,
   each admitted only if the eval harness shows a win on cost per correct
   answer.
5. Code ingest hardening: tree-sitter where the heuristic splitter is not
   enough; Salesforce metadata XML parsed into entities instead of text;
   call edges where cheap.
6. Freshness: `status`, `verify`, stale warnings, `context/` compile, hook.
7. MCP server with a minimal tool surface (`get_context`, `search` returning
   refs, `get_entity`, `list_stale_sources`), batch CLI mode and Python API
   for code-mode use, `feedback` tool; tested against Claude Code and Cursor.

## 16. Integration point with memvine

Context is organisational truth; memory is what an agent learned from tasks.
ctxgraph never writes task-derived learnings; memvine never ingests docs or
ownership. memvine entries may reference ctxgraph chunk ids or entity ids,
and `get_context` accepts an optional `memory_hints` list to boost chunks a
memory points to. Nothing else couples them in v0.1.

## 17. Open questions

- Chunking for large generated or vendored code: `exclude` covers the common
  directories; add `.ctxignore` if teams need it outside the config.
- Service attribution when a file matches no ownership path: `unowned`
  pseudo-service surfaced in `status`.
- Rerank cost: default on for MCP, off for CLI?
- Commit `context/` or generate in CI? Default commit; revisit if noisy.
- Salesforce: should profiles/permission sets be indexed at all (large, low
  signal) or summarised into graph facts?
