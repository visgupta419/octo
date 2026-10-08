"""Ownership inputs: ctx/ownership.yaml, CODEOWNERS, ctx/dependencies.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from ..config import Config
from ..paths import glob_to_regex
from .model import FILE, PERSON, SERVICE, TEAM, GraphBuild, ent_id, file_id

CODEOWNERS_LOCATIONS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def load_ownership(cfg: Config, b: GraphBuild, indexed: list[str]) -> None:
    data = _load_yaml(cfg.repo_root / cfg.graph.ownership)
    for t in data.get("teams") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tid = b.add_entity(TEAM, str(t["name"]), oncall=t.get("oncall"))
        for m in t.get("members") or []:
            pid = b.add_entity(PERSON, str(m))
            b.add_edge(pid, tid, "member_of")
    for s in data.get("services") or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        sid = b.add_entity(SERVICE, str(s["name"]), runbook=s.get("runbook"), team=s.get("team"))
        if s.get("team"):
            tid = b.add_entity(TEAM, str(s["team"]))
            b.add_edge(tid, sid, "owns")
        patterns = s.get("paths") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        regexes = [glob_to_regex(p) for p in patterns]
        for path in indexed:
            if any(r.match(path) for r in regexes):
                fid = file_id(path)
                if b.has(fid):
                    b.add_edge(sid, fid, "contains")
                    b.set_attrs(fid, service=s["name"], owner=s.get("team") or b.entities[fid].attrs.get("owner"))


def _codeowners_regex(pattern: str):
    p = pattern.strip()
    if p.startswith("/"):
        p = p[1:]
    elif "/" not in p.rstrip("/"):
        p = "**/" + p
    return glob_to_regex(p)


def load_codeowners(cfg: Config, b: GraphBuild, indexed: list[str]) -> None:
    if not cfg.graph.codeowners:
        return
    path = next((cfg.repo_root / loc for loc in CODEOWNERS_LOCATIONS if (cfg.repo_root / loc).is_file()), None)
    if path is None:
        return
    rules: list[tuple[object, list[str]]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        owners = [o.lstrip("@") for o in parts[1:]]
        if owners:
            rules.append((_codeowners_regex(parts[0]), owners))
    for rel in indexed:
        owners: list[str] = []
        for regex, names in rules:  # last matching rule wins, as in GitHub
            if regex.match(rel):
                owners = names
        if not owners:
            continue
        fid = file_id(rel)
        if not b.has(fid):
            continue
        for name in owners:
            tid = b.add_entity(TEAM, name, source="CODEOWNERS")
            b.add_edge(tid, fid, "owns", via="CODEOWNERS")
        if not b.entities[fid].attrs.get("owner"):
            b.set_attrs(fid, owner=owners[0])


def load_dependencies(cfg: Config, b: GraphBuild) -> None:
    data = _load_yaml(cfg.repo_root / cfg.graph.dependencies)
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or not e.get("from") or not e.get("to"):
            continue
        src = b.add_entity(SERVICE, str(e["from"]))
        dst = b.add_entity(SERVICE, str(e["to"]))
        b.add_edge(src, dst, "depends_on", via=e.get("kind", "unknown"))


def propagate_owners(b: GraphBuild) -> None:
    """Files owned by a service inherit the service's team as owner."""
    for e in b.by_type(FILE):
        if e.attrs.get("owner"):
            continue
        svc = e.attrs.get("service")
        if svc:
            team = b.entities.get(ent_id(SERVICE, svc))
            if team and team.attrs.get("team"):
                b.set_attrs(e.id, owner=team.attrs["team"])
