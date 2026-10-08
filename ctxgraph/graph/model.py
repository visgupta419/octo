"""In-memory graph under construction, flushed to SQLite in one pass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..store.db import Edge, Entity

# Entity types (the "node types" of the context graph).
TEAM, PERSON, SERVICE, FILE, SYMBOL, OBJECT, FIELD, FLOW, COMPONENT = (
    "team", "person", "service", "file", "symbol", "object", "field", "flow", "component",
)
RULE, RECORDTYPE, PERMISSIONSET, LAYOUT = "rule", "recordtype", "permissionset", "layout"


def ent_id(type_: str, name: str) -> str:
    return f"{type_}:{name}"


def file_id(path: str) -> str:
    return ent_id(FILE, path)


def symbol_id(qualified: str, path: str) -> str:
    return f"{SYMBOL}:{qualified}@{path}"


@dataclass
class GraphBuild:
    entities: dict[str, Entity] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    # (src_id, simple name, kind) references to resolve once every name is known
    pending: list[tuple[str, str, str]] = field(default_factory=list)

    def add_entity(self, type_: str, name: str, id_: str | None = None, **attrs: Any) -> str:
        """Create or merge an entity. Existing attrs win over new ones."""
        eid = id_ or ent_id(type_, name)
        cur = self.entities.get(eid)
        if cur is None:
            self.entities[eid] = Entity(id=eid, type=type_, name=name, attrs=dict(attrs))
        elif attrs:
            merged = {**attrs, **cur.attrs}
            self.entities[eid] = Entity(id=eid, type=cur.type, name=cur.name, attrs=merged)
        return eid

    def set_attrs(self, eid: str, **attrs: Any) -> None:
        cur = self.entities[eid]
        self.entities[eid] = Entity(id=eid, type=cur.type, name=cur.name, attrs={**cur.attrs, **attrs})

    def has(self, eid: str) -> bool:
        return eid in self.entities

    def add_edge(self, src: str, dst: str, kind: str, **attrs: Any) -> None:
        if src == dst or src not in self.entities or dst not in self.entities:
            return
        key = (src, dst, kind)
        cur = self.edges.get(key)
        if cur is None:
            self.edges[key] = Edge(src, dst, kind, dict(attrs))
        else:
            merged = dict(cur.attrs)
            for k, v in attrs.items():
                if k == "count" and isinstance(v, int):
                    merged[k] = merged.get(k, 0) + v
                else:
                    merged.setdefault(k, v)
            self.edges[key] = Edge(src, dst, kind, merged)

    def defer(self, src: str, name: str, kind: str) -> None:
        self.pending.append((src, name, kind))

    def by_type(self, type_: str) -> list[Entity]:
        return [e for e in self.entities.values() if e.type == type_]

    def name_index(self, types: tuple[str, ...]) -> dict[str, list[str]]:
        """lower(name) -> entity ids for the given types."""
        idx: dict[str, list[str]] = {}
        for e in self.entities.values():
            if e.type in types:
                idx.setdefault(e.name.lower(), []).append(e.id)
        return idx
