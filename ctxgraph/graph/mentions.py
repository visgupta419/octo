"""Link prose chunks to the entities they name."""

from __future__ import annotations

import re

from ..store.db import Database
from .model import COMPONENT, FLOW, OBJECT, SERVICE, SYMBOL, TEAM, GraphBuild

MENTION_TYPES = (SYMBOL, OBJECT, SERVICE, TEAM, COMPONENT, FLOW)
MIN_NAME_LEN = 4
MAX_PER_CHUNK = 20


def scan_mentions(db: Database, b: GraphBuild, source_types: tuple[str, ...] = ("markdown", "text")) -> int:
    index: dict[str, list[str]] = {}
    for e in b.entities.values():
        if e.type not in MENTION_TYPES or len(e.name) < MIN_NAME_LEN:
            continue
        if e.type == SYMBOL and "." in e.name:
            continue  # methods are matched through their class
        index.setdefault(e.name.lower(), []).append(e.id)
    if not index:
        return 0
    names = sorted(index, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.IGNORECASE)
    rows: list[tuple[str, str]] = []
    for chunk_id, _path, text, _stype in db.iter_chunks(source_types):
        found: list[str] = []
        for m in pattern.finditer(text):
            for eid in index[m.group(1).lower()]:
                if eid not in found:
                    found.append(eid)
            if len(found) >= MAX_PER_CHUNK:
                break
        rows.extend((chunk_id, eid) for eid in found)
    db.add_mentions(rows)
    return len(rows)
