# ctxgraph

A git-native context layer for coding agents. ctxgraph ingests a repository
and its surrounding documents, organises them into three context buckets
(**knowledge**, **expertise**, **norms**), and compiles bounded, citable
context packs on demand. State lives in the repo and one local SQLite file.
No services, no API keys.

Works on any codebase with no language toolchain installed: markdown is split
by heading, source code is split by declaration (Apex, Java, Kotlin, C#, Go,
Rust, JavaScript/TypeScript, Python, and more), everything else is windowed as
text. Salesforce DX projects are detected and get Apex, LWC/Aura/Visualforce
and metadata sources out of the box.

## Status

Milestones 1 to 4 of the [design](docs/DESIGN.md) are done: config loader,
SQLite schema, markdown and code ingest, hybrid retrieval (BM25 fused with
local embeddings, optional cross-encoder rerank, near-duplicate dedupe),
token-budgeted packs with a graph-derived Facts block, the context graph
(ownership, code structure, Salesforce metadata, git history), a query log
with anti-pattern stats, an eval harness with a grep baseline, and the
`init` / `ingest` / `query` / `graph` / `stats` / `eval` commands.
Freshness tracking, compiled `context/` pages and the MCP server are later
milestones.

## Quickstart

```bash
pip install -e ".[embed]"   # Python 3.11+; the embed extra adds fastembed (ONNX, CPU)
cd /path/to/your/repo
ctxgraph init               # writes ctxgraph.yaml and a self-ignoring .ctxgraph/
ctxgraph ingest             # builds .ctxgraph/index.db, graph and vectors (incremental)
ctxgraph query "how do we retry failed callouts" --budget 3000
ctxgraph query "AccountService getAccounts" --bucket knowledge --json
ctxgraph graph AccountService         # describe a class, object, service, file...
ctxgraph graph Account -t object
ctxgraph stats                        # context anti-patterns from the query log
ctxgraph eval --fail-under 0.7        # score packs against evals/questions.yaml
```

A pack looks like this:

```markdown
# Context pack — "deprecated retry pattern"

_2 chunks, 410 / 4000 tokens (from 7 candidates)._

## Facts
- RetryHelper — apex class, force-app/.../RetryHelper.cls#L1-80; referenced by: CalloutService, QueueJob; owner: platform; last changed 2026-09-12 by alice (14 commits); co-changes with: CalloutService.cls

## Norms
### [norms] CONTRIBUTING.md#L12-30 (abc1234)
```text
[CONTRIBUTING.md > Contributing > Retries]
Do not use the deprecated RetryHelper class ...
```
```

Every chunk cites its path, line range and the commit it was indexed at.
Facts come first and are built from the graph, never from a model. Sections
then appear in the order norms, expertise, knowledge; within a section
chunks keep retrieval rank order.

## The context graph

`ctxgraph ingest` builds a graph next to the chunks, from things the repo
already has:

| Source | Entities | Edges |
| --- | --- | --- |
| Code (any supported language) | file, symbol (class, method, trigger, function) | file declares symbol, class contains method, symbol references symbol |
| Apex and Lightning | object, field, component | trigger triggers_on object, class references object/field, component calls Apex method, component uses component |
| Salesforce metadata XML | object, field, flow | field belongs_to object, lookup references object, flow references object, flow calls Apex |
| `ctx/ownership.yaml`, `CODEOWNERS`, `ctx/dependencies.yaml` | team, person, service | team owns service/file, service contains file, service depends_on service, person member_of team |
| Git history | person | person authored file, file co_changed file; files carry commit count, last change and last author |
| Markdown chunks | | chunk mentions entity |

`ctxgraph graph <name>` prints an entity's one-hop neighbourhood. `query`
picks up to five entities named in the query or declared by the top hits and
puts their fact lines at the top of the pack.

## Configuration

`ctxgraph.yaml` sits at the repo root. `ctxgraph init` writes a starter you
can trim.

```yaml
version: 1
repo_root: .
db_path: .ctxgraph/index.db
budget_tokens_default: 4000

exclude: ["**/node_modules/**", "**/dist/**"]   # applied to every source

chunking:                 # optional, defaults shown
  target_tokens: 600
  max_tokens: 800
  min_tokens: 40          # smaller sections merge into a neighbour
  overlap_ratio: 0.1      # overlap between windows of an oversized section

sources:
  - id: agent-rules
    type: markdown
    bucket: norms
    paths: ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md"]
  - id: postmortems
    type: markdown
    bucket: expertise
    paths: ["postmortems/**/*.md"]
    manual: true
    stale_after_days: 90
  - id: apex
    type: code
    bucket: knowledge
    paths: ["**/*.cls", "**/*.trigger"]
    languages: [apex]     # optional filter on detected language
  - id: docs
    type: markdown
    bucket: knowledge
    paths: ["**/*.md"]

graph:                    # optional; missing files are skipped
  ownership: ctx/ownership.yaml
  dependencies: ctx/dependencies.yaml
  codeowners: true
  history:
    max_commits: 1000
    cochange_min: 2
```

Rules worth knowing:

- A file is claimed by the **first** source whose `paths` match it, so list
  narrow sources before catch-alls.
- `paths` are gitignore-style globs (`*`, `**`, `?`, `[..]`). A bare directory
  name matches everything under it.
- Inside a git repo the file list comes from `git ls-files`, so `.gitignore`
  is honoured. Outside git, the tree is walked and `.git`, `node_modules`,
  `__pycache__` are skipped.
- Binary files are skipped and reported by `ingest`.
- Source types: `markdown` (split by heading, breadcrumb kept), `code`
  (split by declaration, language detected from extension), `text` (windowed
  by paragraph).

## How retrieval works today

1. Hard filters in SQL: `--bucket`, `--source`, `--path` prefix.
2. BM25 over an FTS5 index (Porter stemming, underscores kept in tokens),
   with question words dropped. Chunks holding every term are ranked first
   and the any-term list is fused in by reciprocal rank. Compound
   identifiers, headings and file names are also indexed split, so
   "execution launcher" finds `ExecutionLauncher.java`.
3. Vectors, when an embedding provider is configured: cosine search over
   the stored chunk vectors, fused with the BM25 list by reciprocal rank.
   `--retriever bm25|hybrid|vector` picks; `auto` is hybrid when vectors
   exist.
4. Score adjustments on both lists: test and spec files are demoted, files
   whose classes the codebase references heavily are boosted. Optional
   cross-encoder rerank of the top 30 (`rerank.enabled`, or `--rerank`).
5. Dedupe by content hash and by vector similarity (`near_duplicate_cosine`),
   cap chunks per file, fill the remaining budget in rank order.
6. Graph facts for entities named in the query or declared by the chunks
   that made the pack, capped at 40% of the budget and placed first.

### Embeddings

```yaml
embedding:
  provider: local                 # none | local | openai | voyage | hash
  model: BAAI/bge-small-en-v1.5   # ~130 MB one-time download, CPU inference
  # parallel: 4                   # worker processes for the initial embed
rerank:
  enabled: false                  # Xenova/ms-marco-MiniLM-L-6-v2 via fastembed
```

`local` runs on CPU through fastembed (ONNX); `openai` is any
OpenAI-compatible `/embeddings` endpoint (set `base_url` for Ollama, LM
Studio or vLLM and `api_key_env` for the key); `voyage` is Voyage AI's
code-tuned models; `hash` is a deterministic feature-hashing stand-in with
no model, used by the tests. Vectors live in the index keyed by chunk id,
so an incremental ingest only embeds new or changed chunks, and switching
models re-embeds everything once. Without a provider, or when the package
is missing, everything degrades to BM25 with a warning. Expect roughly four
chunks per second per core for the initial embed of a large repo.

## Measuring it

`evals/questions.yaml` holds questions the way you would ask a teammate,
each with the files and entities a good answer must cite:

```yaml
questions:
  - id: start-execution
    q: what starts a pipeline execution when it is triggered
    expect_paths: [ExecutionLauncher.java]      # bare name matches the path tail
    expect_entities: [ExecutionLauncher]
    tags: [core]
```

`ctxgraph eval` compiles a pack per question and reports path recall
(expected file in the pack or cited by a Facts line), candidate recall
(expected file retrieved at all, which separates ranking misses from
retrieval misses), entity recall, pack tokens and latency. It also runs a
deterministic baseline, an agent that greps the query terms and reads the
matching files, so you get tokens-to-answer with and without the pack.
`--fail-under` makes it a CI gate; `evals/examples/spinnaker-orca.yaml` is a
24-question set for spinnaker/orca. `--retriever bm25` versus `--retriever
hybrid` (and `--rerank`) on the same questions is how a retrieval change
earns its place.

On spinnaker/orca (24 questions, 3,000-token budget, mean pack about 2,300
tokens against roughly 32,000 for the grep baseline):

| Retriever | Path recall | Entity recall | p50 latency |
| --- | --- | --- | --- |
| BM25 at the start of milestone 3 | 21% | 25% | |
| BM25, tuned | 75% | 71% | 80 ms |
| Vectors only (bge-small) | 67% | 67% | 85 ms |
| Hybrid (default when vectors exist) | 83% | 83% | 104 ms |
| Hybrid + cross-encoder rerank | 88% | 88% | 1.7 s |

Candidate recall is 96% in every tuned row: the remaining misses are
ranking, not retrieval. Rerank is off by default because of its latency;
turn it on for agents that call once per task rather than per turn. The
initial embed of orca's 7,300 chunks took 15 minutes on one CPU core.

Every query is logged locally; `ctxgraph stats` turns the log into findings
(sources never retrieved, queries that matched only some terms, packs that
hit the budget ceiling) with a remedy for each.

## Tested against

- A Salesforce DX layout (Apex classes and triggers, Lightning web
  components, object, field and flow metadata).
- [spinnaker/orca](https://github.com/spinnaker/orca), a Java, Kotlin and
  Groovy Gradle multi-module service: 1,866 source files, about 7,000 chunks
  and 9,000 symbols, indexed in roughly nine seconds including 300 commits
  of history. Spock feature methods keep their string names, Kotlin trailing
  lambdas and anonymous classes are not mistaken for declarations, and
  references to a name declared in several packages resolve through imports.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Layout:

```
ctxgraph/
  config.py        ctxgraph.yaml loading and validation, init template
  paths.py         glob matching and repo file listing
  gitutil.py       HEAD lookup, git ls-files
  tokens.py        token estimate (pluggable)
  ingest/          chunk.py (markdown/windowing), code.py (declarations),
                   loaders.py (type registry), __init__.py (pipeline)
  graph/           model.py, ownership.py, symbols.py, salesforce.py,
                   history.py, mentions.py, facts.py, __init__.py (builder)
  store/           schema.sql, db.py
  retrieve/        bm25.py, embed.py (providers), vector.py, fusion.py,
                   rerank.py, search.py (hybrid pipeline)
  evals/           question loading, runner, grep baseline, report
  compile/         pack.py (budget, facts, dedupe), render.py (markdown/json)
  stats.py         query-log anti-patterns
  cli.py
docs/DESIGN.md     the v0.1 design and milestone plan
```
