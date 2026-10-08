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

Milestones 1 to 3 of the [design](docs/DESIGN.md) are done: config loader,
SQLite schema, markdown and code ingest, BM25 retrieval with filters,
token-budgeted packs with a graph-derived Facts block, the context graph
(ownership, code structure, Salesforce metadata, git history), a query log
with anti-pattern stats, an eval harness with a grep baseline, and the
`init` / `ingest` / `query` / `graph` / `stats` / `eval` commands.
Embeddings, freshness tracking, compiled `context/` pages and the MCP server
are later milestones.

## Quickstart

```bash
pip install -e .            # Python 3.11+, deps: click, pyyaml
cd /path/to/your/repo
ctxgraph init               # writes ctxgraph.yaml and a self-ignoring .ctxgraph/
ctxgraph ingest             # builds .ctxgraph/index.db (incremental; --full rebuilds)
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
3. Rerank: test and spec files are demoted, files whose classes the
   codebase references heavily are boosted. Both are `retrieval:` knobs.
4. Dedupe by content hash, cap chunks per file, fill the remaining budget in
   rank order.
5. Graph facts for entities named in the query or declared by the chunks
   that made the pack, capped at 40% of the budget and placed first.

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
24-question set for spinnaker/orca.

On orca, the milestone-3 tuning moved path recall from 21% to 75% and
candidate recall from 67% to 96%, at a mean pack of about 2,300 tokens
against roughly 32,000 for the grep baseline.

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
  retrieve/        bm25.py, fusion.py, search.py
  evals/           question loading, runner, grep baseline, report
  compile/         pack.py (budget, facts, dedupe), render.py (markdown/json)
  stats.py         query-log anti-patterns
  cli.py
docs/DESIGN.md     the v0.1 design and milestone plan
```
