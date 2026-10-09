"""Code structure: declarations, references, Lightning components and imports."""

from __future__ import annotations

import re
from pathlib import Path

from ..ingest.code import BRACE_LANGUAGES, detect_language, list_declarations, mask_source, use_parser
from ..ingest.treesitter import ParsedFile, parse_file
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
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;?\s*$", re.MULTILINE)
_PACKAGE_LANGUAGES = {"java", "kotlin", "groovy", "scala"}


def _dir_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _resolve(
    name_lower: str,
    candidates: list[tuple[str, str]],
    file_dir: str,
    imports: list[str],
) -> str | None:
    """Pick one symbol id for a simple name, using imports and package layout."""
    if len(candidates) == 1:
        return candidates[0][0]
    # explicit import: com.x.y.Name or wildcard com.x.y.* -> candidate under /com/x/y/
    for imp in imports:
        pkg = imp.rsplit(".", 1)[0] if imp.rsplit(".", 1)[-1].lower() == name_lower else imp
        pkg_dir = "/" + pkg.replace(".", "/") + "/"
        for sid, path in candidates:
            if pkg_dir in "/" + _dir_of(path) + "/":
                return sid
    # same package (same directory)
    for sid, path in candidates:
        if _dir_of(path) == file_dir:
            return sid
    return None


def _kebab_to_camel(s: str) -> str:
    head, *rest = s.split("-")
    return head + "".join(p.capitalize() for p in rest)


def _component_for(path: str) -> str | None:
    m = _COMPONENT_DIR_RE.search(path)
    return m.group(2) if m else None


def extract_structure(
    repo_root: Path, b: GraphBuild, code_paths: list[str], parser: str = "auto"
) -> dict[str, tuple[str, str, str, str, ParsedFile | None]]:
    """Add file, symbol, component and trigger-object entities.

    Returns path -> (language, masked text, raw text, source entity id,
    parsed file or None) for the reference pass, which runs after every
    name is known.
    """
    texts: dict[str, tuple[str, str, str, str, ParsedFile | None]] = {}
    for path in code_paths:
        try:
            text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        language = detect_language(path)
        fid = file_id(path)
        b.add_entity(FILE, path, kind="code", language=language)
        lines = text.splitlines()
        parsed = parse_file(text, language) if use_parser(language, parser) else None
        decls = sorted(parsed.declarations, key=lambda d: (d.start_line, -d.end_line)) if parsed else list_declarations(lines, language, "heuristic")
        src_entity = fid
        for d in decls:
            sid = symbol_id(d.qualified, path)
            extra = dict(parsed.attrs.get(d.qualified, {})) if parsed else {}
            b.add_entity(
                SYMBOL, d.qualified, id_=sid,
                kind=d.kind, language=language, path=path,
                start_line=d.start_line, end_line=d.end_line, simple=d.name, **extra,
            )
            if extra.get("object") and d.kind == "trigger":
                oid = b.add_entity(OBJECT, extra["object"], standard=not extra["object"].endswith("__c"))
                b.add_edge(sid, oid, "triggers_on")
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
        texts[path] = (language or "", masked, text, src_entity, parsed)
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


def resolve_references(b: GraphBuild, texts: dict[str, tuple[str, str, str, str, ParsedFile | None]]) -> None:
    """Link each file's primary entity to the known names it mentions."""
    top_level_all: dict[str, list[tuple[str, str]]] = {}
    for e in b.by_type(SYMBOL):
        if "." not in e.name and e.attrs.get("kind") in TYPE_KINDS:
            top_level_all.setdefault(e.name.lower(), []).append((e.id, e.attrs.get("path", "")))
    # unambiguous names, for the LWC and deferred lookups below
    top_level = {k: v[0][0] for k, v in top_level_all.items() if len(v) == 1}
    objects = {e.name.lower(): e.id for e in b.by_type(OBJECT)}
    fields = {e.name.lower(): e.id for e in b.by_type(FIELD)}
    # Apex names fields without the object: Industry_Segment__c, not Account.Industry_Segment__c
    fields_simple: dict[str, list[str]] = {}
    for e in b.by_type(FIELD):
        fields_simple.setdefault(e.name.rsplit(".", 1)[-1].lower(), []).append(e.id)
    components = {e.name.lower(): e.id for e in b.by_type(COMPONENT)}
    known = {**objects, **top_level}

    for path, (language, masked, raw, src, _parsed) in texts.items():
        self_names = {b.entities[src].name.lower()} if src in b.entities else set()
        file_dir = _dir_of(path)
        imports = _IMPORT_RE.findall(raw) if language in _PACKAGE_LANGUAGES else []
        counts: dict[str, int] = {}
        resolved: dict[str, str | None] = {}
        for m in _CAP_IDENT_RE.finditer(masked):
            k = m.group(0).lower()
            if k in self_names:
                continue
            if k not in resolved:
                if k in top_level_all:
                    resolved[k] = _resolve(k, top_level_all[k], file_dir, imports)
                else:
                    resolved[k] = objects.get(k)
            target = resolved[k]
            if target:
                counts[target] = counts.get(target, 0) + 1
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

    flows = {e.name.lower(): e.id for e in b.by_type("flow")}
    for src, name, kind in b.pending:
        target = known.get(name.lower()) or components.get(name.lower()) or flows.get(name.lower())
        if target and src in b.entities:
            b.add_edge(src, target, kind)

    resolve_calls(b, texts, top_level_all, objects, fields)


def _class_of(qualified: str) -> str:
    return qualified.split(".", 1)[0]


def resolve_calls(
    b: GraphBuild,
    texts: dict[str, tuple[str, str, str, str, ParsedFile | None]],
    top_level_all: dict[str, list[tuple[str, str]]],
    objects: dict[str, str],
    fields: dict[str, str],
) -> None:
    """Method-level ``calls`` edges from parsed call sites, plus Apex SOQL/DML.

    A callee is matched by simple name against methods in: the receiver's
    class when the receiver is a known class (static call), else the
    caller's own class, else the classes this file references, else the
    whole repo when the name is unique. Anything still ambiguous is skipped.
    """
    from ..retrieve.bm25 import is_test_path

    methods: dict[str, list[tuple[str, str, str]]] = {}  # simple -> [(id, class, path)]
    for e in b.by_type(SYMBOL):
        if e.attrs.get("kind") == "function" and "." in e.name:
            methods.setdefault(e.name.rsplit(".", 1)[-1].lower(), []).append((e.id, _class_of(e.name).lower(), e.attrs.get("path", "")))

    def pick(cands: list[tuple[str, str, str]], classes: set[str] | None) -> str | None:
        if classes is not None:
            cands = [c for c in cands if c[1] in classes]
        return cands[0][0] if len(cands) == 1 else None

    for path, (language, _masked, _raw, src, parsed) in texts.items():
        if parsed is None:
            continue
        caller_is_test = is_test_path(path)
        referenced = {b.entities[dst].name.lower() for (s, dst, kind), _ in b.edges.items() if s == src and kind == "references" and dst in b.entities}
        for call in parsed.calls:
            cands = methods.get(call.callee.lower())
            if not cands:
                continue
            caller_id = symbol_id(call.caller, path) if call.caller else src
            if caller_id not in b.entities:
                caller_id = src
            own_class = _class_of(call.caller).lower() if call.caller else None
            target = None
            if call.receiver and call.receiver.lower() in top_level_all:
                target = pick(cands, {call.receiver.lower()})
            elif call.receiver in (None, "this", "self"):
                target = pick(cands, {own_class}) if own_class else None
                if target is None:
                    target = pick(cands, referenced)
            else:
                target = pick(cands, referenced)
            if target is None and len(cands) == 1 and (call.receiver is not None or own_class is None):
                # repo-wide unique name: never link production code to a test method
                if caller_is_test or not is_test_path(cands[0][2]):
                    target = cands[0][0]
            if target and target != caller_id:
                b.add_edge(caller_id, target, "calls", count=1)
        for q in parsed.soql:
            caller_id = symbol_id(q.caller, path) if q.caller else src
            if caller_id not in b.entities:
                caller_id = src
            oid = b.add_entity(OBJECT, q.object, standard=not q.object.endswith("__c"))
            b.add_edge(caller_id, oid, "references", count=1, via="soql")
            for f in q.fields:
                if "." in f:
                    continue
                fid = fields.get(f"{q.object}.{f}".lower())
                if fid:
                    b.add_edge(caller_id, fid, "references", count=1, via="soql")
        ops: dict[str, set[str]] = {}
        for caller, op in parsed.dml:
            ops.setdefault(caller, set()).add(op)
        for caller, names in ops.items():
            sid = symbol_id(caller, path)
            if sid in b.entities:
                b.set_attrs(sid, dml=sorted(names))
