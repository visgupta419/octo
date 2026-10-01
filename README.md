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

Milestone 1 of the [design](docs/DESIGN.md) is done: config loader, SQLite
schema, markdown and code ingest, BM25 retrieval with filters, token-budgeted
packs, and the `init` / `ingest` / `query` commands. Embeddings, the ownership
graph, freshness tracking, the MCP server and evals are later milestones.

## Quickstart

```bash
pip install -e .            # Python 3.11+, deps: click, pyyaml
cd /path/to/your/repo
ctxgraph init               # writes ctxgraph.yaml and a self-ignoring .ctxgraph/
ctxgraph ingest             # builds .ctxgraph/index.db (incremental; --full rebuilds)
ctxgraph query "how do we retry failed callouts" --budget 3000
ctxgraph query "AccountService getAccounts" --bucket knowledge --json
```

A pack looks like this:

```markdown
# Context pack — "deprecated retry pattern"

_2 chunks, 410 / 4000 tokens (from 7 candidates)._

## Norms
### [norms] CONTRIBUTING.md#L12-30 (abc1234)
```text
[CONTRIBUTING.md > Contributing > Retries]
Do not use the deprecated RetryHelper class ...
```
```

Every chunk cites its path, line range and the commit it was indexed at.
Sections appear in the order norms, expertise, knowledge; within a section
chunks keep retrieval rank order.

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
2. BM25 over an FTS5 index (Porter stemming, underscores kept in tokens).
   All query terms are required first; if nothing matches, any term will do.
   Compound identifiers are also indexed split, so `getAccounts` matches
   "get accounts".
3. Dedupe by content hash, then fill the token budget in rank order. Ten
   percent of the budget is reserved for graph facts and citations.

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
  store/           schema.sql, db.py
  retrieve/        bm25.py, search.py
  compile/         pack.py (budget, dedupe), render.py (markdown/json)
  cli.py
docs/DESIGN.md     the v0.1 design and milestone plan
```
