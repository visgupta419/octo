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


def build_graph(cfg: Config, db: Database, progress=None) -> GraphReport:
    """Build the graph from the indexed chunks. ``progress(stage, done, total)``
    is called between stages so a CLI can show activity."""
    paths = db.chunk_paths()  # path -> source id
    source_types = {s.id: s.type for s in cfg.sources}
    b = GraphBuild()
    for path, sid in paths.items():
        stype = source_types.get(sid, "text")
        b.add_entity(FILE, path, kind="code" if stype == "code" else "doc" if stype == "markdown" else "text", source=sid)

    indexed = sorted(paths)
    if progress:
        progress("graph: ownership", 0, 0)
    load_ownership(cfg, b, indexed)
    load_codeowners(cfg, b, indexed)
    load_dependencies(cfg, b)

    code_paths = sorted(p for p, sid in paths.items() if source_types.get(sid) == "code")
    if progress:
        progress("graph: parsing code", 0, len(code_paths))
    texts = extract_structure(cfg.repo_root, b, code_paths, cfg.chunking.parser, progress=progress)
    if progress:
        progress("graph: metadata", 0, 0)
    extract_metadata(cfg.repo_root, b, list_repo_files(cfg.repo_root))
    if progress:
        progress("graph: resolving references", 0, 0)
    resolve_references(b, texts)
    if progress:
        progress("graph: git history", 0, 0)
    commits = extract_history(cfg, b, set(indexed))
    propagate_owners(b)
    if progress:
        progress("graph: writing", 0, 0)

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
