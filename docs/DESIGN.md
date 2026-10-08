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

## 7. Data model (SQLite) **[M1, M2]**

```sql
sources(id PK, type, bucket, config_json, ingested_commit, ingested_at,
        manual, stale_after_days, last_verified)
chunks(rid INTEGER PK, id UNIQUE, source_id FK, path, bucket, text,
       start_line, end_line, commit_sha, file_mtime, token_count, hash, terms)
chunk_meta(chunk_id FK, key, value)       -- heading, kind, language, doc_type
entities(id PK, type, name, lname, repo, attrs_json)
edges(src_id, dst_id, kind, attrs_json)
mentions(chunk_id, entity_id)
query_log(ts, text, filters_json, mode, candidates, pack_chunks, used_tokens, ...)
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

## 9. Retrieval and compilation **[M1-M4]**

Query input: free text plus `buckets`, `sources`, `paths`, later `teams`,
`services`, `since_commit` / `since_days`, and `budget_tokens`.

1. Hard filters in SQL.
2. BM25 top-k (k=50) with stopwords dropped; the all-terms and any-term
   lists are fused by reciprocal rank. Test paths are demoted and files
   whose symbols are referenced heavily are boosted (`retrieval:` knobs).
3. Embedding top-k (cosine over stored unit vectors, brute force; fine to
   ~100k chunks), merged with the BM25 list by reciprocal rank fusion.
4. Optional cross-encoder rerank of the top 30 (off by default).
5. Graph facts: up to five entities named in the query or declared by the
   top hits, each rendered as one line from its one-hop neighbourhood,
   capped at 40% of the budget and placed first.
6. Dedupe by content hash and by near-duplicate cosine (0.95) when
   vectors exist.
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

## 13. Evaluation **[M3]**

`evals/questions.yaml` with expected paths and entities per question;
`ctxgraph eval` reports path recall at budget (chunks plus paths cited by
Facts), candidate recall (retrieved at all), entity recall, pack size,
latency, and a grep-and-read baseline for tokens-to-answer without the
pack; `--fail-under` fails CI.

Results on spinnaker/orca, 24 questions at 3,000 tokens (grep baseline:
42% recall at 14x the tokens):

| Retriever | Path | Candidate | Entity | p50 |
| --- | --- | --- | --- | --- |
| BM25, start of M3 | 21% | 67% | 25% | |
| BM25, tuned (M3) | 75% | 96% | 71% | 80 ms |
| Vectors only, bge-small (M4) | 67% | 96% | 67% | 85 ms |
| Hybrid BM25 + vectors (M4) | 83% | 96% | 83% | 104 ms |
| Hybrid + cross-encoder rerank (M4) | 88% | 96% | 88% | 1.7 s |
| BM25 + rerank | 71% | 96% | 71% | 1.6 s |

Decisions taken from the table: hybrid is the default whenever vectors
exist; vectors alone are not (they lose exact-identifier matches BM25
gets); rerank is a toggle, off by default, because 1.7 s per query is
acceptable for a once-per-task brief but not per turn. Feeding the
reranker truncated chunks (1,500 or 800 characters) dropped recall to
79%, so it scores full chunks. Three misses remain at the top setting,
all ranking-within-budget on long method bodies; smaller code chunks or
a code-tuned embedding model are the next levers.

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

## 14b. Lessons applied from Driver (driver.ai)

Driver positions itself as a compiler for context: parse every file, resolve
symbols, trace call chains, map dependencies, then emit symbol-complete
documentation, architecture maps and history guides, deterministically
("the same codebase state produces the same context"), regenerated
incrementally when commits land, and served to agents over a handful of MCP
tools (file documentation, architecture overview, a task brief that gathers
context for a task). Its critique of the alternatives is the one ctxgraph
has to answer: hand-written markdown goes stale and causes merge conflicts;
RAG over code fragments loses the structural relationships agents need.

What ctxgraph adopts, scoped to one repo:

1. **Structure first, chunks as evidence.** The graph (milestone 2) is the
   primary product: declarations, references, triggers-on, imports,
   ownership, co-change. Retrieval over chunks remains for prose and for
   code the graph does not model yet.
2. **Compiled context is a build artifact, not prose someone wrote.** The
   committed `context/` folder (milestone 6) becomes: a per-file symbol
   page (what it declares, what it references, who references it, owners,
   recent history), an architecture map (services, components, objects and
   the edges between them), and a history guide (recent change clusters
   per area from git). All of it is generated deterministically from the
   graph with no model call, so it diffs cleanly and never drifts.
3. **DAG-keyed regeneration.** Each compiled page records a hash of its
   inputs (file content hashes plus neighbour entity ids). Ingest
   regenerates a page only when that hash changes, which is the
   single-repo version of "only the affected parts of the DAG are
   reprocessed".
4. **Optional synthesis pass, pluggable.** Driver's bottom-up summaries
   (file → module → system) are useful but need a model. ctxgraph keeps a
   hook for a model-written summary per file or module, cached by the same
   input hash, off by default, picked by the eval harness like any other
   model use.
5. **Two more lookups.** `ctxgraph graph <symbol|path>` answers "describe
   this thing" from the graph, and a task-shaped `query` (facts, relevant
   files with their symbol pages, applicable norms) is the repo-scale
   version of a task brief.
6. **Registered content maps to sources.** Driver lets people and agents
   register documents as auto-updating or static. ctxgraph's `sources`
   with `manual: true` and `verify` are the same idea; expertise written by
   people is tracked for staleness instead of regenerated.

Where ctxgraph deliberately differs: it stays local and git-native, with
no service, and models only what it can compute from the repo.

## 14c. Findings from the Spinnaker run

Running milestone 2 against `spinnaker/orca` (Java, Kotlin, Groovy; 60
Gradle modules) changed four things that a small Salesforce sample never
exercised:

- **Header classification is per language family.** In Kotlin and Groovy a
  bare `name { ... }` or `name(args) { ... }` is almost always a call with
  a trailing lambda, so only keyword-introduced declarations (`fun`, `def
  name(`, `class`, `val x get()`) count. In Java-family languages a method
  name is always preceded by a type or modifier. JavaScript keeps its
  looser rules. Methods nested inside a method (anonymous classes) are not
  named.
- **Names collide in big repos.** Two classes called `Task` existed. The
  reference pass now resolves a simple name through the file's imports
  (explicit or wildcard) and then its own package directory, and declines
  to guess when still ambiguous.
- **Fact selection needs an importance signal.** A query word like "task"
  matched a test-local class and a Kotlin property. Entities are now ranked
  by incoming reference count with penalties for test code, nested symbols
  and properties, and a plain English word only selects a symbol the
  codebase actually leans on. Hits collapse to their top-level class.
- **Spock feature methods are named by a string.** `def "stores a
  pipeline"()` is extracted from the raw header so tests are discoverable
  by what they assert.

## 15. Milestones

1. **Done.** Config loader, SQLite schema, markdown + code + text ingest,
   BM25 search with filters, budgeted packs, CLI `init`/`ingest`/`query`.
2. **Done.** Graph: ownership (`ctx/ownership.yaml`, CODEOWNERS,
   `ctx/dependencies.yaml`); entities for services, teams, people, files,
   symbols, Salesforce objects, fields, flows and Lightning components;
   edges for ownership, declares/contains, references, triggers_on, calls,
   uses, authored, co_changed and doc mentions; `graph` command; Facts
   block first in packs; `query_log` and `ctxgraph stats`. Commits are
   not entities (authorship and co-change edges carry what packs need).
3. **Done.** Evals: `evals/questions.yaml` per repo (24 questions for
   spinnaker/orca shipped as an example, 10 for ctxgraph itself run in CI);
   `eval` reports path, candidate and entity recall, pack tokens, latency
   and a deterministic grep-and-read baseline; `--fail-under` gates CI;
   pack output verified byte-identical across runs. Tuning driven by the
   harness: stopword removal, strict/loose rank fusion, heading and file
   name terms, test-path demotion, importance boost, per-file cap, Facts
   derived from the compiled pack. A real agent A/B (tokens and tool calls
   with a model in the loop) remains an optional `--agent` hook for later.
4. **Done.** Hybrid retrieval: embedding providers behind one interface
   (local fastembed/ONNX bge-small by default, OpenAI-compatible endpoints,
   Voyage, and a deterministic hash stand-in for tests), vectors stored per
   chunk id and embedded incrementally, cosine search fused with BM25 by
   reciprocal rank, test demotion and importance boost applied to both
   lists, optional cross-encoder rerank, near-duplicate dedupe by cosine,
   `--retriever` and `--rerank` on `query` and `eval` for A/B runs.
   Measurements are in §13.
5. Code ingest hardening: tree-sitter where the heuristic splitter is not
   enough; Salesforce metadata XML parsed into entities instead of text;
   call edges where cheap.
6. Freshness and compiled context: `status`, `verify`, stale warnings;
   `context/` with per-file symbol pages, an architecture map and a history
   guide, generated from the graph and regenerated by input hash; git hook.
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
