"""Build the context graph from the indexed repo and write it to the store."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..paths import list_repo_files
from ..store.db import Database
from .history import extract_history
from .mentions import scan_mentions
from .model import FILE, GraphBuild
from .ownership import load_codeowners, load_dependencies, load_ownership, propagate_owners
from .salesforce import extract_metadata
from .symbols import extract_structure, resolve_references


@dataclass
class GraphReport:
    entities: int = 0
    edges: int = 0
    mentions: int = 0
    commits: int = 0
    by_type: dict[str, int] = field(default_factory=dict)


def build_graph(cfg: Config, db: Database) -> GraphReport:
    paths = db.chunk_paths()  # path -> source id
    source_types = {s.id: s.type for s in cfg.sources}
    b = GraphBuild()
    for path, sid in paths.items():
        stype = source_types.get(sid, "text")
        b.add_entity(FILE, path, kind="code" if stype == "code" else "doc" if stype == "markdown" else "text", source=sid)

    indexed = sorted(paths)
    load_ownership(cfg, b, indexed)
    load_codeowners(cfg, b, indexed)
    load_dependencies(cfg, b)

    code_paths = [p for p, sid in paths.items() if source_types.get(sid) == "code"]
    texts = extract_structure(cfg.repo_root, b, sorted(code_paths), cfg.chunking.parser)
    extract_metadata(cfg.repo_root, b, list_repo_files(cfg.repo_root))
    resolve_references(b, texts)
    commits = extract_history(cfg, b, set(indexed))
    propagate_owners(b)

    db.clear_graph()
    for ent in b.entities.values():
        db.upsert_entity(ent, repo=cfg.repo_name)
    for edge in b.edges.values():
        db.add_edge(edge)
    db.commit()
    mentions = scan_mentions(db, b)
    db.commit()

    by_type: dict[str, int] = {}
    for ent in b.entities.values():
        by_type[ent.type] = by_type.get(ent.type, 0) + 1
    return GraphReport(
        entities=len(b.entities), edges=len(b.edges), mentions=mentions, commits=commits, by_type=by_type
    )
