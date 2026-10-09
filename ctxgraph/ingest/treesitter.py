"""Parser-backed code analysis via tree-sitter (optional ``parse`` extra).

Gives exact declarations, method-level call sites, and for Apex the SOQL
objects/fields, DML statements, annotations and sharing modifiers. Groovy's
grammar has no declaration nodes, so it stays on the heuristic splitter.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache

from .code import Declaration

SUPPORTED = {"java", "apex", "kotlin", "python", "javascript", "typescript"}

# Languages where a "method" nested inside a method is an anonymous class
# member or lambda, not a declaration worth naming.
_NO_NESTED_FUNCTIONS = {"java", "apex", "kotlin"}

_DECL_KINDS: dict[str, dict[str, str]] = {
    "java": {
        "class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
        "record_declaration": "record", "annotation_type_declaration": "annotation",
        "method_declaration": "function", "constructor_declaration": "function",
    },
    "apex": {
        "class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
        "trigger_declaration": "trigger", "method_declaration": "function",
        "constructor_declaration": "function",
    },
    "kotlin": {
        "class_declaration": "class", "object_declaration": "object", "companion_object": "object",
        "function_declaration": "function",
    },
    "python": {"class_definition": "class", "function_definition": "function"},
    "javascript": {
        "class_declaration": "class", "method_definition": "function", "function_declaration": "function",
        "generator_function_declaration": "function",
    },
    "typescript": {
        "class_declaration": "class", "abstract_class_declaration": "class", "interface_declaration": "interface",
        "enum_declaration": "enum", "method_definition": "function", "function_declaration": "function",
        "generator_function_declaration": "function",
    },
}
_NAME_TYPES = ("identifier", "type_identifier", "simple_identifier", "property_identifier")


@dataclass
class Call:
    caller: str  # qualified name of the enclosing declaration ("" at file level)
    callee: str
    receiver: str | None
    line: int


@dataclass
class Soql:
    caller: str
    object: str
    fields: list[str]
    line: int


@dataclass
class ParsedFile:
    language: str
    declarations: list[Declaration] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    soql: list[Soql] = field(default_factory=list)
    dml: list[tuple[str, str]] = field(default_factory=list)  # (caller, operation)
    attrs: dict[str, dict] = field(default_factory=dict)  # qualified -> extra attrs


@lru_cache(maxsize=None)
def available() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


def supports(language: str | None) -> bool:
    return language in SUPPORTED and available()


@lru_cache(maxsize=None)
def _parser(language: str):
    import tree_sitter_language_pack as tslp

    return tslp.get_parser(language)


def _text(src: bytes, node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_child(node, types: tuple[str, ...]):
    for c in node.children:
        if c.type in types:
            return c
    return None


_TYPE_KINDS = {"class", "interface", "enum", "record", "object", "trait", "annotation"}


def _decl_name(node, language: str, src: bytes) -> str | None:
    if node.type == "companion_object":
        return "companion"
    n = node.child_by_field_name("name") if language != "kotlin" else None
    if n is None:
        n = _first_child(node, _NAME_TYPES)
    if n is None:
        return None
    name = _text(src, n)
    if language == "apex" and node.type == "trigger_declaration":
        ids = [c for c in node.children if c.type == "identifier"]
        name = _text(src, ids[0]) if ids else name
    return name or None


def _annotations_and_modifiers(node, src: bytes) -> tuple[list[str], list[str]]:
    mods = _first_child(node, ("modifiers",))
    if mods is None:
        return [], []
    ann: list[str] = []
    mod: list[str] = []
    for c in mods.children:
        if c.type in ("annotation", "marker_annotation"):
            n = c.child_by_field_name("name") or _first_child(c, ("identifier",))
            if n is not None:
                ann.append(_text(src, n))
        elif c.type == "modifier":
            mod.append(_text(src, c).strip())
        elif c.type.endswith("_modifier"):
            mod.append(_text(src, c).strip())
        elif c.child_count == 0:
            mod.append(_text(src, c).strip())
    return ann, mod


_HERITAGE_RES = {
    "extends": re.compile(r"\bextends\s+([A-Za-z_][\w.]*)"),
    "implements": re.compile(r"\bimplements\s+([^{]+)"),
}
_TYPE_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")


_BODY_TYPES = ("class_body", "enum_body", "interface_body", "block", "statement_block", "object_body")


def _header_text(node, src: bytes) -> str:
    """Declaration text up to its body (from the source bytes).

    Scans children by type rather than using ``child_by_field_name``: the
    Kotlin grammar's nodes have no fields and the field lookup faulted.
    """
    end = node.end_byte
    for c in node.children:
        if c.type in _BODY_TYPES:
            end = min(end, c.start_byte)
            break
    return src[node.start_byte : end].decode("utf-8", errors="replace")


def _split_types(text: str) -> list[str]:
    """Top-level comma-separated type names, generics and call arguments stripped."""
    out: list[str] = []
    depth = 0
    cur = ""
    for i, ch in enumerate(text):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            if not (ch == ">" and i > 0 and text[i - 1] == "-"):
                depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        if depth == 0:
            cur += ch
    out.append(cur)
    names = []
    for part in out:
        m = _TYPE_NAME_RE.search(part.strip())
        if m and m.group(0) not in ("extends", "implements", "by"):
            names.append(m.group(0).rsplit(".", 1)[-1])
    return names


def _heritage(node, language: str, src: bytes) -> tuple[list[str], str | None]:
    """(implements, extends) for a type declaration, read from its header text.

    Text-based on purpose: descending the Kotlin delegation subtree crashed
    the tree-sitter binding on nested test DSLs.
    """
    head = _header_text(node, src)
    if language == "kotlin":
        # the supertype list follows the ':' outside the primary constructor's parens
        depth = 0
        colon = -1
        for i, ch in enumerate(head):
            if ch in "<([":
                depth += 1
            elif ch in ">)]":
                if ch == ">" and i > 0 and head[i - 1] == "-":
                    continue  # `->` in a function type is not a closing generic
                depth -= 1
            elif ch == ":" and depth == 0:
                colon = i
                break
        if colon == -1:
            return [], None
        after = head[colon + 1 :]
        implements: list[str] = []
        extends: str | None = None
        for part in _split_types_raw(after):
            name = _TYPE_NAME_RE.search(part.strip())
            if not name:
                continue
            n = name.group(0).rsplit(".", 1)[-1]
            if "(" in part and extends is None:
                extends = n
            else:
                implements.append(n)
        return implements, extends
    if language in ("javascript", "typescript"):
        m = _HERITAGE_RES["extends"].search(head)
        impl = _HERITAGE_RES["implements"].search(head) if language == "typescript" else None
        return (_split_types(impl.group(1)) if impl else []), (m.group(1).rsplit(".", 1)[-1] if m else None)
    m = _HERITAGE_RES["extends"].search(head)
    impl = _HERITAGE_RES["implements"].search(head)
    extends = m.group(1).rsplit(".", 1)[-1] if m else None
    implements = _split_types(impl.group(1).split(" extends ")[0]) if impl else []
    if language == "java" and node.type == "interface_declaration" and m:
        # `interface A extends B, C`: all are supertypes
        implements = _split_types(head.split("extends", 1)[1])
        extends = None
    return implements, extends


def _split_types_raw(text: str) -> list[str]:
    """Top-level comma-separated parts of a Kotlin supertype list (text kept)."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for i, ch in enumerate(text):
        if ch in "<([":
            depth += 1
        elif ch in ">)]" and not (ch == ">" and i > 0 and text[i - 1] == "-"):
            depth -= 1
        if ch == "{" and depth == 0:
            break
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    parts.append(cur)
    return parts


def _callee(node, language: str, src: bytes) -> tuple[str | None, str | None]:
    """(callee simple name, receiver text) for a call node."""
    if language in ("java", "apex"):
        n = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        return (_text(src, n) if n is not None else None, _text(src, obj) if obj is not None and obj.type == "identifier" else None)
    if language == "kotlin":
        head = node.children[0] if node.children else None
        if head is None:
            return None, None
        if head.type == "simple_identifier":
            return _text(src, head), None
        if head.type == "navigation_expression":
            suffix = head.children[-1]
            ident = _first_child(suffix, ("simple_identifier",)) if suffix.type == "navigation_suffix" else None
            root = head
            while root.type == "navigation_expression" and root.children:
                root = root.children[0]
            receiver = _text(src, root) if root.type == "simple_identifier" else None
            return (_text(src, ident) if ident is not None else None), receiver
        return None, None
    if language == "python":
        fn = node.child_by_field_name("function")
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return _text(src, fn), None
        if fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            obj = fn.child_by_field_name("object")
            return (_text(src, attr) if attr is not None else None, _text(src, obj) if obj is not None and obj.type == "identifier" else None)
        return None, None
    fn = node.child_by_field_name("function")  # javascript / typescript
    if fn is None:
        return None, None
    if fn.type == "identifier":
        return _text(src, fn), None
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        return (_text(src, prop) if prop is not None else None, _text(src, obj) if obj is not None and obj.type in ("identifier", "this") else None)
    return None, None


_CALL_TYPES = {
    "java": ("method_invocation",), "apex": ("method_invocation",), "kotlin": ("call_expression",),
    "python": ("call",), "javascript": ("call_expression",), "typescript": ("call_expression",),
}


def parse_file(text: str, language: str | None) -> ParsedFile | None:
    if not supports(language):
        return None
    assert language is not None
    src = text.encode("utf-8", errors="replace")
    tree = _parser(language).parse(src)
    kinds = _DECL_KINDS[language]
    call_types = _CALL_TYPES[language]
    out = ParsedFile(language=language)
    # Line numbers come from byte offsets: reading a node's end position
    # corrupted later node reads on some Kotlin files (binding bug).
    newlines = [i for i, ch in enumerate(src) if ch == 0x0A]

    def line_of(byte: int) -> int:
        return bisect_right(newlines, byte - 1) + 1 if byte > 0 else 1

    def end_line_of(byte: int) -> int:
        # end_byte is exclusive; a declaration ending with "}\n" still ends on that line
        return bisect_right(newlines, max(byte - 1, 0) - 1) + 1 if byte > 1 else 1

    def handle(node, stack: list[str], in_function: bool) -> tuple[list[str], bool]:
        """Record what ``node`` declares or calls; return the scope for its children."""
        kind = kinds.get(node.type)
        decl_name: str | None = None
        if kind is None and language in ("javascript", "typescript"):
            # `const f = () => {}` and class fields `f = () => {}`
            if node.type in ("variable_declarator", "field_definition"):
                value = node.child_by_field_name("value") or (node.children[-1] if node.children else None)
                if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    kind = "function"
        if kind is None and language == "apex" and node.type == "field_declaration" and _first_child(node, ("accessor_list",)) is not None:
            kind = "property"  # `public String Name { get; set; }`
        if kind is None and language == "kotlin" and node.type == "property_declaration" and _first_child(node, ("getter", "setter")) is not None:
            kind = "property"  # `val x: T get() { ... }`
        if kind == "function" and in_function and language in _NO_NESTED_FUNCTIONS:
            kind = None
        if kind is not None:
            decl_name = _decl_name(node, language, src)
            if kind == "property":
                holder = node.child_by_field_name("declarator") or _first_child(node, ("variable_declaration", "variable_declarator"))
                ident = _first_child(holder, ("identifier", "simple_identifier")) if holder is not None else None
                if ident is None:
                    ident = _first_child(node, ("identifier",))
                decl_name = _text(src, ident) if ident is not None else decl_name
        if kind is not None and decl_name:
            qualified = ".".join(stack + [decl_name])
            start = line_of(node.start_byte)
            if language == "python" and node.parent is not None and node.parent.type == "decorated_definition":
                start = line_of(node.parent.start_byte)
            out.declarations.append(
                Declaration(kind=kind, name=decl_name, qualified=qualified, start_line=start,
                            end_line=end_line_of(node.end_byte), depth=len(stack))
            )
            ann, mods = _annotations_and_modifiers(node, src)
            attrs: dict = {}
            if ann:
                attrs["annotations"] = ann
            if mods:
                attrs["modifiers"] = mods
            if kind in _TYPE_KINDS:
                implements, extends = _heritage(node, language, src)
                if implements:
                    attrs["implements"] = implements
                if extends:
                    attrs["extends"] = extends
            if node.type == "trigger_declaration":
                ids = [c for c in node.children if c.type == "identifier"]
                if len(ids) >= 2:
                    attrs["object"] = _text(src, ids[1])
                attrs["events"] = [_text(src, c).strip() for c in node.children if c.type == "trigger_event"]
            if attrs:
                out.attrs[qualified] = attrs
            child_stack = stack + [decl_name]
            child_in_function = in_function or kind == "function"
        else:
            child_stack = stack
            child_in_function = in_function
        if node.type in call_types:
            callee, receiver = _callee(node, language, src)
            if callee:
                out.calls.append(Call(caller=".".join(child_stack), callee=callee, receiver=receiver, line=line_of(node.start_byte)))
        if language == "apex":
            if node.type == "query_expression":
                body = _first_child(node, ("soql_query_body",))
                frm = _first_child(body, ("from_clause",)) if body is not None else None
                storage = _first_child(frm, ("storage_identifier",)) if frm is not None else None
                if storage is not None:
                    sel = _first_child(body, ("select_clause",))
                    fields = [_text(src, c) for c in sel.children if c.type == "field_identifier"] if sel is not None else []
                    out.soql.append(Soql(caller=".".join(child_stack), object=_text(src, storage).strip(), fields=fields, line=line_of(node.start_byte)))
            elif node.type == "dml_expression":
                op = _first_child(node, ("dml_type",))
                if op is not None:
                    out.dml.append((".".join(child_stack), _text(src, op).strip().split()[0].lower()))
        return child_stack, child_in_function

    # Iterative pre-order walk: deeply nested test DSLs (Spek, nested
    # lambdas) overflow the C stack when walked recursively.
    todo = [(tree.root_node, [], False)]
    while todo:
        node, stack, in_function = todo.pop()
        child_stack, child_in_function = handle(node, stack, in_function)
        for c in reversed(node.children):
            todo.append((c, child_stack, child_in_function))
    return out
