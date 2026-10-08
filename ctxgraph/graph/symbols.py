"""Code structure: declarations, references, Lightning components and imports."""

from __future__ import annotations

import re
from pathlib import Path

from ..ingest.code import detect_language, list_declarations, mask_source, BRACE_LANGUAGES
from .model import COMPONENT, FIELD, FILE, OBJECT, SYMBOL, GraphBuild, ent_id, file_id, symbol_id
from .salesforce import STANDARD_OBJECTS

TYPE_KINDS = {"class", "interface", "enum", "trigger", "record", "struct", "trait", "object", "module", "namespace", "protocol", "extension", "impl"}
_TRIGGER_ON_RE = re.compile(r"\btrigger\s+\w+\s+on\s+(\w+)", re.IGNORECASE)
_CAP_IDENT_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
_CUSTOM_RE = re.compile(r"\b\w+__[cr]\b")
_SOQL_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_]\w*)", re.IGNORECASE)
_APEX_IMPORT_RE = re.compile(r"@salesforce/apex/(\w+)\.(\w+)")
_SCHEMA_IMPORT_RE = re.compile(r"@salesforce/schema/(\w+)(?:\.(\w+))?")
_LWC_TAG_RE = re.compile(r"<c-([a-z0-9]+(?:-[a-z0-9]+)*)")
_COMPONENT_DIR_RE = re.compile(r"(?:^|/)(lwc|aura)/([^/]+)/")


def _kebab_to_camel(s: str) -> str:
    head, *rest = s.split("-")
    return head + "".join(p.capitalize() for p in rest)


def _component_for(path: str) -> str | None:
    m = _COMPONENT_DIR_RE.search(path)
    return m.group(2) if m else None


def extract_structure(repo_root: Path, b: GraphBuild, code_paths: list[str]) -> dict[str, tuple[str, str, str, str]]:
    """Add file, symbol, component and trigger-object entities.

    Returns path -> (language, masked text, raw text, source entity id) for
    the reference pass, which runs after every name is known.
    """
    texts: dict[str, tuple[str, str, str, str]] = {}
    for path in code_paths:
        try:
            text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        language = detect_language(path)
        fid = file_id(path)
        b.add_entity(FILE, path, kind="code", language=language)
        lines = text.splitlines()
        decls = list_declarations(lines, language)
        src_entity = fid
        for d in decls:
            sid = symbol_id(d.qualified, path)
            b.add_entity(
                SYMBOL, d.qualified, id_=sid,
                kind=d.kind, language=language, path=path,
                start_line=d.start_line, end_line=d.end_line, simple=d.name,
            )
            if d.depth == 0:
                b.add_edge(fid, sid, "declares")
                if src_entity == fid and d.kind in TYPE_KINDS:
                    src_entity = sid
            else:
                parent_q = d.qualified.rsplit(".", 1)[0]
                b.add_edge(symbol_id(parent_q, path), sid, "contains")
            if d.kind == "trigger":
                m = _TRIGGER_ON_RE.search("\n".join(lines[d.start_line - 1 : d.start_line + 2]))
                if m:
                    oid = b.add_entity(OBJECT, m.group(1), standard=not m.group(1).endswith("__c"))
                    b.add_edge(sid, oid, "triggers_on")
        comp = _component_for(path)
        if comp:
            cid = b.add_entity(COMPONENT, comp, kind="lwc" if "/lwc/" in f"/{path}" else "aura")
            b.add_edge(fid, cid, "part_of")
            src_entity = cid
        masked = "\n".join(mask_source(lines, language)) if language in BRACE_LANGUAGES else text
        texts[path] = (language or "", masked, text, src_entity)
        # SOQL FROM targets and well-known standard objects are objects even
        # when the repo holds no metadata for them.
        if language == "apex":
            for m in _SOQL_FROM_RE.finditer(masked):
                name = m.group(1)
                if name[0].isupper():
                    b.add_entity(OBJECT, name, standard=not name.endswith("__c"))
            for m in _CAP_IDENT_RE.finditer(masked):
                if m.group(0) in STANDARD_OBJECTS:
                    b.add_entity(OBJECT, m.group(0), standard=True)
    return texts


def resolve_references(b: GraphBuild, texts: dict[str, tuple[str, str, str, str]]) -> None:
    """Link each file's primary entity to the known names it mentions."""
    top_level = {
        e.name.lower(): e.id
        for e in b.by_type(SYMBOL)
        if "." not in e.name and e.attrs.get("kind") in TYPE_KINDS
    }
    objects = {e.name.lower(): e.id for e in b.by_type(OBJECT)}
    fields = {e.name.lower(): e.id for e in b.by_type(FIELD)}
    # Apex names fields without the object: Industry_Segment__c, not Account.Industry_Segment__c
    fields_simple: dict[str, list[str]] = {}
    for e in b.by_type(FIELD):
        fields_simple.setdefault(e.name.rsplit(".", 1)[-1].lower(), []).append(e.id)
    components = {e.name.lower(): e.id for e in b.by_type(COMPONENT)}
    known = {**objects, **top_level}

    for path, (language, masked, raw, src) in texts.items():
        self_names = {b.entities[src].name.lower()} if src in b.entities else set()
        counts: dict[str, int] = {}
        for m in _CAP_IDENT_RE.finditer(masked):
            k = m.group(0).lower()
            if k in known and k not in self_names:
                counts[known[k]] = counts.get(known[k], 0) + 1
        for m in _CUSTOM_RE.finditer(masked):
            k = m.group(0).lower()
            targets = [objects[k]] if k in objects else [fields[k]] if k in fields else fields_simple.get(k, [])
            if len(targets) > 2:  # same field name on many objects: too ambiguous
                continue
            for target in targets:
                counts[target] = counts.get(target, 0) + 1
        for target, n in counts.items():
            b.add_edge(src, target, "references", count=n)

        if language in ("javascript", "typescript"):
            # import specifiers are string literals, so scan the raw text
            for m in _APEX_IMPORT_RE.finditer(raw):
                cls, method = m.group(1), m.group(2)
                target = None
                for e in b.by_type(SYMBOL):
                    if e.name.lower() == f"{cls}.{method}".lower():
                        target = e.id
                        break
                target = target or top_level.get(cls.lower())
                if target:
                    b.add_edge(src, target, "calls")
            for m in _SCHEMA_IMPORT_RE.finditer(raw):
                obj, fld = m.group(1), m.group(2)
                oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
                b.add_edge(src, oid, "references", count=1)
                if fld:
                    fid_ = fields.get(f"{obj}.{fld}".lower())
                    if fid_:
                        b.add_edge(src, fid_, "references", count=1)
        if language == "html":
            for m in _LWC_TAG_RE.finditer(raw):
                target = components.get(_kebab_to_camel(m.group(1)).lower())
                if target:
                    b.add_edge(src, target, "uses")

    for src, name, kind in b.pending:
        target = known.get(name.lower()) or components.get(name.lower())
        if target and src in b.entities:
            b.add_edge(src, target, kind)
