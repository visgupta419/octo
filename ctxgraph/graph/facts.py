"""Turn an entity's one-hop neighbourhood into short fact lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..retrieve.bm25 import Hit
from ..store.db import Database, Edge, Entity
from .model import COMPONENT, FIELD, FILE, FLOW, OBJECT, PERSON, SERVICE, SYMBOL, TEAM, file_id, symbol_id

MAX_NAMES = 6
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_TYPE_ORDER = {SYMBOL: 0, OBJECT: 1, SERVICE: 2, COMPONENT: 3, FLOW: 4, TEAM: 5, FIELD: 6, FILE: 7, PERSON: 8}


_TEST_PATH_RE = re.compile(r"(^|/)(test|tests|spec|specs|__tests__)(/|$)")
_TEST_NAME_RE = re.compile(r"(Test|Tests|Spec|IT)$")


def is_test(e: Entity) -> bool:
    path = e.attrs.get("path") or (e.name if e.type == FILE else "")
    return bool(_TEST_PATH_RE.search(path)) or bool(_TEST_NAME_RE.search(e.name.split(".")[0]))


def _names(ents: list[Entity], n: int = MAX_NAMES, label=None) -> str:
    ents = sorted(ents, key=lambda e: (is_test(e), e.name))  # production code first
    shown = [label(e) if label else e.name for e in ents[:n]]
    extra = len(ents) - n
    return ", ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


@dataclass
class Neighborhood:
    entity: Entity
    out: dict[str, list[tuple[Edge, Entity]]] = field(default_factory=dict)
    inc: dict[str, list[tuple[Edge, Entity]]] = field(default_factory=dict)
    mentions: list[str] = field(default_factory=list)

    def out_ents(self, kind: str, type_: str | None = None) -> list[Entity]:
        return [e for _, e in self.out.get(kind, []) if type_ is None or e.type == type_]

    def in_ents(self, kind: str, type_: str | None = None) -> list[Entity]:
        return [e for _, e in self.inc.get(kind, []) if type_ is None or e.type == type_]


def neighborhood(db: Database, ent: Entity) -> Neighborhood:
    n = Neighborhood(entity=ent)
    for edge, other in db.edges_from(ent.id):
        n.out.setdefault(edge.kind, []).append((edge, other))
    for edge, other in db.edges_to(ent.id):
        n.inc.setdefault(edge.kind, []).append((edge, other))
    n.mentions = db.mentions_of(ent.id)
    return n


def _file_facts(db: Database, path: str) -> list[str]:
    f = db.get_entity(file_id(path))
    if f is None:
        return []
    parts: list[str] = []
    if f.attrs.get("owner"):
        parts.append(f"owner: {f.attrs['owner']}")
    if f.attrs.get("service"):
        parts.append(f"service: {f.attrs['service']}")
    if f.attrs.get("last_changed"):
        who = f" by {f.attrs['last_author']}" if f.attrs.get("last_author") else ""
        parts.append(f"last changed {f.attrs['last_changed']}{who} ({f.attrs.get('commits', '?')} commits)")
    co = [e for edge, e in db.edges_from(f.id) + db.edges_to(f.id) if edge.kind == "co_changed"]
    if co:
        parts.append("co-changes with: " + _names(co, 4, label=lambda e: e.name.rsplit("/", 1)[-1]))
    return parts


def describe(db: Database, ent: Entity) -> str:
    """One fact line for an entity."""
    n = neighborhood(db, ent)
    a = ent.attrs
    parts: list[str] = []
    if ent.type == SYMBOL:
        loc = f"{a.get('path')}#L{a.get('start_line')}-{a.get('end_line')}" if a.get("path") else ""
        head = f"{ent.name} — {a.get('language') or ''} {a.get('kind') or 'symbol'}".strip()
        if loc:
            head += f", {loc}"
        members = n.out_ents("contains")
        if members:
            parts.append("members: " + _names(members, label=lambda e: _short(e.name)))
        refs = n.out_ents("references") + n.out_ents("calls")
        if refs:
            parts.append("references: " + _names(refs))
        on = n.out_ents("triggers_on")
        if on:
            parts.append("trigger on: " + _names(on))
        by = n.in_ents("references") + n.in_ents("calls")
        if by:
            parts.append("referenced by: " + _names(by))
        if a.get("path"):
            parts.extend(_file_facts(db, a["path"]))
    elif ent.type == OBJECT:
        head = f"{ent.name} — {'standard' if a.get('standard') else 'custom'} sObject"
        if a.get("label"):
            head += f" ({a['label']})"
        fields = n.in_ents("belongs_to")
        if fields:
            parts.append("fields: " + _names(fields, label=lambda e: _short(e.name)))
        trig = n.in_ents("triggers_on")
        if trig:
            parts.append("triggers: " + _names(trig))
        used = [e for e in n.in_ents("references") if e.type != FIELD]
        if used:
            parts.append("used by: " + _names(used))
    elif ent.type == SERVICE:
        head = f"{ent.name} — service"
        owners = n.in_ents("owns", TEAM)
        if owners:
            oc = owners[0].attrs.get("oncall")
            parts.append("owned by " + _names(owners) + (f" (on-call {oc})" if oc else ""))
        if a.get("runbook"):
            parts.append(f"runbook: {a['runbook']}")
        deps = n.out_ents("depends_on")
        if deps:
            parts.append("depends on: " + _names(deps))
        rdeps = n.in_ents("depends_on")
        if rdeps:
            parts.append("depended on by: " + _names(rdeps))
        files = n.out_ents("contains")
        if files:
            parts.append(f"{len(files)} files, e.g. " + _names(files, 3))
    elif ent.type == TEAM:
        head = f"{ent.name} — team"
        if a.get("oncall"):
            parts.append(f"on-call {a['oncall']}")
        members = n.in_ents("member_of")
        if members:
            parts.append("members: " + _names(members))
        svcs = n.out_ents("owns", SERVICE)
        if svcs:
            parts.append("owns services: " + _names(svcs))
        files = n.out_ents("owns", FILE)
        if files:
            parts.append(f"owns {len(files)} files directly")
    elif ent.type == COMPONENT:
        head = f"{ent.name} — {a.get('kind', 'lwc')} component"
        calls = n.out_ents("calls")
        if calls:
            parts.append("calls Apex: " + _names(calls))
        refs = n.out_ents("references")
        if refs:
            parts.append("references: " + _names(refs))
        uses = n.out_ents("uses")
        if uses:
            parts.append("uses components: " + _names(uses))
        used = n.in_ents("uses")
        if used:
            parts.append("used by: " + _names(used))
        files = n.in_ents("part_of")
        if files:
            parts.append("files: " + _names(files, 4))
    elif ent.type == FLOW:
        head = f"{ent.name} — flow"
        extra = [a[k] for k in ("process_type", "status") if a.get(k)]
        if extra:
            head += f" ({', '.join(extra)})"
        refs = n.out_ents("references")
        if refs:
            parts.append("objects: " + _names(refs))
        calls = n.out_ents("calls")
        if calls:
            parts.append("calls Apex: " + _names(calls))
    elif ent.type == FIELD:
        head = f"{ent.name} — field"
        if a.get("type"):
            head += f" ({a['type']})"
        refs = n.out_ents("references")
        if refs:
            parts.append("lookup to: " + _names(refs))
        used = n.in_ents("references")
        if used:
            parts.append("used by: " + _names(used))
    elif ent.type == FILE:
        head = f"{ent.name} — file"
        decl = n.out_ents("declares")
        if decl:
            parts.append("declares: " + _names(decl))
        parts.extend(_file_facts(db, ent.name))
        authors = n.in_ents("authored")
        if authors:
            parts.append("authors: " + _names(authors, 4))
    elif ent.type == PERSON:
        head = f"{ent.name} — person"
        files = n.out_ents("authored")
        if files:
            parts.append(f"touched {len(files)} files, e.g. " + _names(files, 4))
        teams = n.out_ents("member_of")
        if teams:
            parts.append("member of: " + _names(teams))
    else:
        head = f"{ent.name} — {ent.type}"
    if n.mentions and ent.type != FILE:
        parts.append("mentioned in: " + ", ".join(n.mentions[:3]))
    return head + ("; " + "; ".join(parts) if parts else "")


def _importance(db: Database, e: Entity) -> float:
    """Higher is better: referenced a lot, production code, top-level, not a property."""
    score = float(db.in_degree(e.id))
    if e.type == SYMBOL:
        if "." in e.name:
            score -= 2
        if e.attrs.get("kind") == "property":
            score -= 3
    if is_test(e):
        score -= 5
    return score


def _query_entities(db: Database, query: str) -> list[Entity]:
    tokens = _WORD_RE.findall(query)
    candidates: list[tuple[str, bool]] = []  # (text, identifier-like)
    for i, tok in enumerate(tokens):
        ident = "." in tok or "_" in tok or (tok[1:] != tok[1:].lower() and tok[1:] != tok[1:].upper())
        if len(tok) >= 3:
            candidates.append((tok, ident))
        if i + 1 < len(tokens):
            candidates.append((tok + tokens[i + 1], True))
            candidates.append((tok + "_" + tokens[i + 1], True))
    scored: dict[str, tuple[float, Entity]] = {}
    for cand, ident in candidates:
        for e in db.find_entities(cand) if ident else _exact(db, cand):
            if e.type in (PERSON, FILE) or e.id in scored:
                continue
            score = _importance(db, e)
            # A plain English word ("task", "run") only counts when it names
            # something the codebase actually leans on; an exact-case match
            # on a capitalised name ("Run") is taken as deliberate.
            deliberate = ident or (cand[0].isupper() and e.name == cand)
            if not deliberate and e.type == SYMBOL and (score < 2 or "." in e.name):
                continue
            scored[e.id] = (score, e)
    ranked = sorted(scored.values(), key=lambda t: (-t[0], _TYPE_ORDER.get(t[1].type, 9), t[1].name))
    return [e for _, e in ranked]


def _exact(db: Database, name: str) -> list[Entity]:
    rows = db.conn.execute(
        "SELECT * FROM entities WHERE lname = ? AND type != 'file' ORDER BY type, name LIMIT 10",
        (name.lower(),),
    ).fetchall()
    return [db._row_entity(r) for r in rows]


def _hit_entities(db: Database, hits: list[Hit], top: int = 5) -> list[Entity]:
    """The top-level class (not the method) behind each of the best hits."""
    out: list[Entity] = []
    for h in hits[:top]:
        ent = None
        if h.heading:
            ent = db.get_entity(symbol_id(h.heading.split(" > ")[0], h.path))
        if ent is None:
            f = db.get_entity(file_id(h.path))
            if f and f.attrs.get("kind") == "code":
                ent = f
        if ent and all(ent.id != e.id for e in out):
            out.append(ent)
    return sorted(out, key=lambda e: is_test(e))


def entities_for_query(db: Database, query: str, hits: list[Hit], limit: int = 5) -> list[Entity]:
    chosen: list[Entity] = []
    for e in _query_entities(db, query) + _hit_entities(db, hits):
        if all(e.id != c.id for c in chosen):
            chosen.append(e)
        if len(chosen) >= limit:
            break
    return chosen


def facts_for_query(db: Database, query: str, hits: list[Hit], limit: int = 5) -> list[str]:
    return [describe(db, e) for e in entities_for_query(db, query, hits, limit)]


def describe_json(db: Database, ent: Entity) -> dict[str, Any]:
    n = neighborhood(db, ent)
    return {
        "id": ent.id,
        "type": ent.type,
        "name": ent.name,
        "attrs": ent.attrs,
        "summary": describe(db, ent),
        "outgoing": {k: [{"id": e.id, "name": e.name, "type": e.type, **edge.attrs} for edge, e in v] for k, v in n.out.items()},
        "incoming": {k: [{"id": e.id, "name": e.name, "type": e.type, **edge.attrs} for edge, e in v] for k, v in n.inc.items()},
        "mentioned_in": n.mentions,
    }
