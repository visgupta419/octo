"""Configuration loading and validation for ``ctxgraph.yaml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUCKETS = ("knowledge", "expertise", "norms")
CONFIG_FILENAME = "ctxgraph.yaml"
DEFAULT_DB_PATH = ".ctxgraph/index.db"

# Excluded from every source unless a source overrides ``exclude``.
DEFAULT_EXCLUDE = [
    ".ctxgraph/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/*.min.*",
    "**/*.lock",
    "**/package-lock.json",
]


class ConfigError(Exception):
    pass


@dataclass
class ChunkingConfig:
    target_tokens: int = 600
    max_tokens: int = 800
    min_tokens: int = 40
    overlap_ratio: float = 0.10

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ChunkingConfig":
        d = d or {}
        cfg = cls(
            target_tokens=int(d.get("target_tokens", cls.target_tokens)),
            max_tokens=int(d.get("max_tokens", cls.max_tokens)),
            min_tokens=int(d.get("min_tokens", cls.min_tokens)),
            overlap_ratio=float(d.get("overlap_ratio", cls.overlap_ratio)),
        )
        if cfg.target_tokens <= 0 or cfg.max_tokens < cfg.target_tokens:
            raise ConfigError("chunking: need 0 < target_tokens <= max_tokens")
        if not 0 <= cfg.overlap_ratio < 1:
            raise ConfigError("chunking.overlap_ratio must be in [0, 1)")
        return cfg


@dataclass
class EmbeddingConfig:
    provider: str = "none"  # none | local | openai | voyage | hash
    model: str = "BAAI/bge-small-en-v1.5"
    base_url: str | None = None  # openai-compatible endpoint
    api_key_env: str | None = None
    batch_size: int = 64
    # bge-small reads 512 tokens; ~2000 chars of code covers that, and cutting
    # earlier only saves tokenizer time (the vector is identical).
    max_chars: int = 2000
    parallel: int | None = None  # fastembed worker processes (None = single process)
    cache_dir: str | None = None  # model download cache
    dim: int | None = None  # hash provider only

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EmbeddingConfig":
        d = d or {}
        provider = str(d.get("provider", cls.provider))
        if provider not in ("none", "local", "openai", "voyage", "hash"):
            raise ConfigError("embedding.provider must be none, local, openai, voyage or hash")
        default_model = {
            "local": cls.model, "openai": "text-embedding-3-small", "voyage": "voyage-code-3", "hash": "hash", "none": cls.model,
        }[provider]
        return cls(
            provider=provider,
            model=str(d.get("model", default_model)),
            base_url=d.get("base_url"),
            api_key_env=d.get("api_key_env"),
            batch_size=int(d.get("batch_size", cls.batch_size)),
            max_chars=int(d.get("max_chars", cls.max_chars)),
            parallel=int(d["parallel"]) if d.get("parallel") else None,
            cache_dir=d.get("cache_dir"),
            dim=int(d["dim"]) if d.get("dim") else None,
        )


@dataclass
class RerankConfig:
    enabled: bool = False
    model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    top_n: int = 30
    max_chars: int = 0  # 0 = score the full chunk; cutting text lost recall on orca
    cache_dir: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RerankConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", cls.enabled)),
            model=str(d.get("model", cls.model)),
            top_n=int(d.get("top_n", cls.top_n)),
            max_chars=int(d.get("max_chars", cls.max_chars)),
            cache_dir=d.get("cache_dir"),
        )


@dataclass
class RetrievalConfig:
    candidates: int = 50  # chunks retrieved before compiling
    test_path_penalty: float = 0.5  # score multiplier for test/spec files
    max_chunks_per_file: int = 2  # per pack, so one file cannot eat the budget
    importance_boost: float = 0.15  # score *= 1 + boost * log1p(incoming references)
    near_duplicate_cosine: float = 0.95  # drop candidates this similar to a kept one (needs vectors)
    rrf_k: int = 60

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RetrievalConfig":
        d = d or {}
        cfg = cls(
            candidates=int(d.get("candidates", cls.candidates)),
            test_path_penalty=float(d.get("test_path_penalty", cls.test_path_penalty)),
            max_chunks_per_file=int(d.get("max_chunks_per_file", cls.max_chunks_per_file)),
            importance_boost=float(d.get("importance_boost", cls.importance_boost)),
            near_duplicate_cosine=float(d.get("near_duplicate_cosine", cls.near_duplicate_cosine)),
            rrf_k=int(d.get("rrf_k", cls.rrf_k)),
        )
        if cfg.candidates <= 0 or not 0 < cfg.test_path_penalty <= 1 or cfg.max_chunks_per_file <= 0:
            raise ConfigError("retrieval: candidates and max_chunks_per_file must be > 0, test_path_penalty in (0, 1]")
        return cfg


@dataclass
class GraphConfig:
    ownership: str = "ctx/ownership.yaml"
    dependencies: str = "ctx/dependencies.yaml"
    codeowners: bool = True
    history_max_commits: int = 1000
    cochange_min: int = 2
    cochange_max_files: int = 30

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "GraphConfig":
        d = d or {}
        hist = d.get("history") or {}
        return cls(
            ownership=str(d.get("ownership", cls.ownership)),
            dependencies=str(d.get("dependencies", cls.dependencies)),
            codeowners=bool(d.get("codeowners", cls.codeowners)),
            history_max_commits=int(hist.get("max_commits", cls.history_max_commits)),
            cochange_min=int(hist.get("cochange_min", cls.cochange_min)),
            cochange_max_files=int(hist.get("cochange_max_files", cls.cochange_max_files)),
        )


@dataclass
class Source:
    id: str
    type: str
    bucket: str
    paths: list[str]
    exclude: list[str] | None = None  # None -> use global exclude
    manual: bool = False
    stale_after_days: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_config_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "bucket": self.bucket,
            "paths": list(self.paths),
        }
        if self.exclude is not None:
            d["exclude"] = list(self.exclude)
        if self.manual:
            d["manual"] = True
        if self.stale_after_days is not None:
            d["stale_after_days"] = self.stale_after_days
        d.update(self.extra)
        return d


@dataclass
class Config:
    repo_root: Path
    config_path: Path | None
    sources: list[Source]
    db_path: Path
    budget_tokens_default: int = 4000
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def exclude_for(self, source: Source) -> list[str]:
        return source.exclude if source.exclude is not None else self.exclude

    @property
    def repo_name(self) -> str:
        return str(self.raw.get("repo_name") or self.repo_root.name)


def _as_str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"{where} must be a string or a list of strings")


def _parse_source(raw: Any, index: int, known_types: set[str] | None) -> Source:
    where = f"sources[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    sid = raw.get("id")
    if not sid or not isinstance(sid, str):
        raise ConfigError(f"{where}.id is required")
    stype = raw.get("type")
    if not stype or not isinstance(stype, str):
        raise ConfigError(f"{where}.type is required")
    if known_types is not None and stype not in known_types:
        raise ConfigError(
            f"{where}.type '{stype}' is not supported; known types: {sorted(known_types)}"
        )
    bucket = raw.get("bucket")
    if bucket not in BUCKETS:
        raise ConfigError(f"{where}.bucket must be one of {list(BUCKETS)}")
    paths = _as_str_list(raw.get("paths", raw.get("path")), f"{where}.paths")
    if not paths:
        raise ConfigError(f"{where}.paths is required")
    exclude = raw.get("exclude")
    reserved = {"id", "type", "bucket", "paths", "path", "exclude", "manual", "stale_after_days"}
    extra = {k: v for k, v in raw.items() if k not in reserved}
    stale = raw.get("stale_after_days")
    return Source(
        id=sid,
        type=stype,
        bucket=bucket,
        paths=paths,
        exclude=None if exclude is None else _as_str_list(exclude, f"{where}.exclude"),
        manual=bool(raw.get("manual", False)),
        stale_after_days=None if stale is None else int(stale),
        extra=extra,
    )


def parse_config(
    data: dict[str, Any],
    config_path: Path | None,
    known_types: set[str] | None = None,
) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r} (expected 1)")

    base = config_path.parent if config_path else Path.cwd()
    repo_root = (base / str(data.get("repo_root", "."))).resolve()
    if not repo_root.is_dir():
        raise ConfigError(f"repo_root does not exist: {repo_root}")

    db_path = Path(str(data.get("db_path", DEFAULT_DB_PATH)))
    if not db_path.is_absolute():
        db_path = repo_root / db_path

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("config needs a non-empty 'sources' list")
    sources = [_parse_source(s, i, known_types) for i, s in enumerate(raw_sources)]
    ids = [s.id for s in sources]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(f"duplicate source ids: {dupes}")

    exclude = data.get("exclude")
    return Config(
        repo_root=repo_root,
        config_path=config_path,
        sources=sources,
        db_path=db_path,
        budget_tokens_default=int(data.get("budget_tokens_default", 4000)),
        exclude=list(DEFAULT_EXCLUDE) if exclude is None else _as_str_list(exclude, "exclude"),
        chunking=ChunkingConfig.from_dict(data.get("chunking")),
        graph=GraphConfig.from_dict(data.get("graph")),
        retrieval=RetrievalConfig.from_dict(data.get("retrieval")),
        embedding=EmbeddingConfig.from_dict(data.get("embedding")),
        rerank=RerankConfig.from_dict(data.get("rerank")),
        raw=data,
    )


def load_config(path: Path, known_types: set[str] | None = None) -> Config:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_config(data, path, known_types)


def find_config(start: Path | None = None) -> Path | None:
    """Look for ctxgraph.yaml in ``start`` and its parents."""
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        p = candidate / CONFIG_FILENAME
        if p.is_file():
            return p
    return None


_INIT_HEAD = """\
# ctxgraph configuration. See README.md for the full reference.
version: 1
repo_root: .
db_path: .ctxgraph/index.db      # local SQLite index; keep it out of git
budget_tokens_default: 4000

# Embeddings for hybrid (BM25 + vector) retrieval. `none` keeps BM25 only.
# `local` runs bge-small on CPU via fastembed (pip install 'ctxgraph[embed]',
# one-time ~130 MB model download); `openai` works with any OpenAI-compatible
# endpoint (set base_url for Ollama/LM Studio/vLLM); `voyage` for Voyage AI.
embedding:
  provider: local
  model: BAAI/bge-small-en-v1.5

# Optional cross-encoder rerank of the top candidates (same extra as above).
rerank:
  enabled: false

# Graph inputs (all optional; missing files are skipped).
graph:
  ownership: ctx/ownership.yaml       # teams, services and the paths they own
  dependencies: ctx/dependencies.yaml # service -> service edges
  codeowners: true                    # also read CODEOWNERS if present
  history:
    max_commits: 1000                 # git log depth for authors and co-change
    cochange_min: 2                   # commits two files must share to be linked

# Patterns excluded from every source (per-source `exclude` overrides this).
exclude:
  - ".ctxgraph/**"
  - "**/node_modules/**"
  - "**/dist/**"
  - "**/build/**"
  - "**/target/**"
  - "**/vendor/**"
  - "**/*.min.*"
  - "**/*.lock"
  - "**/package-lock.json"

# A file is claimed by the FIRST source whose paths match it, so list the
# narrow, high-signal sources before the catch-alls.
sources:
  - id: agent-rules
    type: markdown
    bucket: norms
    paths: ["CLAUDE.md", "AGENTS.md", ".cursorrules", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"]

  - id: adrs
    type: markdown
    bucket: expertise
    paths: ["docs/adr/**/*.md", "docs/decisions/**/*.md", "adr/**/*.md"]

  - id: postmortems
    type: markdown
    bucket: expertise
    paths: ["postmortems/**/*.md", "docs/postmortems/**/*.md"]
    manual: true
    stale_after_days: 90

  - id: docs
    type: markdown
    bucket: knowledge
    paths: ["**/*.md", "**/*.markdown", "**/*.mdx"]
"""

_INIT_SALESFORCE = """\

  # Salesforce DX project detected (sfdx-project.json).
  - id: apex
    type: code
    bucket: knowledge
    paths: ["**/*.cls", "**/*.trigger"]

  - id: lwc-aura-vf
    type: code
    bucket: knowledge
    paths:
      - "**/lwc/**/*.js"
      - "**/lwc/**/*.html"
      - "**/aura/**/*.js"
      - "**/aura/**/*.cmp"
      - "**/*.page"
      - "**/*.component"

  # Object, field, flow and permission metadata, read as text (no XML parser yet).
  - id: sf-metadata
    type: text
    bucket: knowledge
    paths:
      - "**/objects/**/*.xml"
      - "**/*.flow-meta.xml"
      - "**/*.permissionset-meta.xml"
      - "**/*.labels-meta.xml"
      - "**/*.workflow-meta.xml"
      - "**/*.globalValueSet-meta.xml"
    exclude:
      - ".ctxgraph/**"
      - "**/*.cls-meta.xml"
      - "**/*.trigger-meta.xml"
      - "**/*.profile-meta.xml"
"""

_INIT_CODE = """\

  - id: code
    type: code
    bucket: knowledge
    paths:
      - "**/*.cls"
      - "**/*.trigger"
      - "**/*.java"
      - "**/*.kt"
      - "**/*.kts"
      - "**/*.scala"
      - "**/*.groovy"
      - "**/*.cs"
      - "**/*.go"
      - "**/*.rs"
      - "**/*.swift"
      - "**/*.c"
      - "**/*.h"
      - "**/*.cpp"
      - "**/*.hpp"
      - "**/*.js"
      - "**/*.jsx"
      - "**/*.ts"
      - "**/*.tsx"
      - "**/*.py"
      - "**/*.rb"
      - "**/*.php"
      - "**/*.dart"
    # languages: [apex, java]   # optional: keep only these detected languages
"""


def render_init_template(repo_root: Path) -> str:
    """Starter config; adds Salesforce sources when the repo is an SFDX project."""
    out = _INIT_HEAD
    if (repo_root / "sfdx-project.json").is_file():
        out += _INIT_SALESFORCE
    out += _INIT_CODE
    return out


OWNERSHIP_TEMPLATE = """\
# Ownership for ctxgraph. Teams own services; services own paths.
# Delete what you don't use; an empty file is fine.
teams: []
#  - name: platform
#    oncall: "#platform-oncall"
#    members: [alice, bob]

services: []
#  - name: account-service
#    team: platform
#    paths: ["force-app/main/default/classes/Account*.cls"]
#    runbook: docs/runbooks/accounts.md
"""

QUESTIONS_TEMPLATE = """\
# Eval questions for ctxgraph. Run `ctxgraph eval` after `ctxgraph ingest`.
# Write questions the way you would ask a teammate, and list the file(s) a
# good answer must cite. A bare file name matches any path ending in it;
# globs (**/Foo*.java) work too. expect_entities are class/object/service
# names that should appear in the Facts block.
version: 1
budget_tokens: 3000

questions: []
#  - id: discounts
#    q: how do we calculate opportunity discounts
#    expect_paths: [DiscountService.cls]
#    expect_entities: [DiscountService, Opportunity]
#    tags: [sales]
"""

DEPENDENCIES_TEMPLATE = """\
# Service-to-service edges for ctxgraph.
edges: []
#  - from: account-service
#    to: billing-service
#    kind: rpc
"""
