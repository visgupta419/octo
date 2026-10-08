"""Language-aware code chunking that needs no parser binaries.

Two strategies cover most codebases:

* **Brace languages** (Apex, Java, Kotlin, JS/TS, C#, Go, Rust, ...): a small
  scanner masks strings and comments, then tracks ``{``/``}`` nesting. Blocks
  whose header looks like a declaration (class, trigger, method, property,
  function) become chunks, carrying a ``Class > method`` breadcrumb. Files or
  classes that fit the token budget stay whole.
* **Python**: ``def``/``class`` lines split by indentation level.

Anything else falls back to plain-text windowing. Tree-sitter can replace
this later behind the same ``code_sections`` function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import ChunkingConfig
from ..tokens import count_tokens

# --------------------------------------------------------------------------
# Language detection

LANGUAGE_BY_EXT: dict[str, str] = {
    # Salesforce
    ".cls": "apex",
    ".trigger": "apex",
    ".apex": "apex",
    ".page": "visualforce",
    ".component": "visualforce",
    ".cmp": "aura",
    ".app": "aura",
    ".evt": "aura",
    ".design": "aura",
    # JVM
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",
    # .NET / C family
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".m": "objc",
    ".swift": "swift",
    ".go": "go",
    ".rs": "rust",
    ".dart": "dart",
    ".php": "php",
    # Web
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".css": "css",
    ".scss": "css",
    ".html": "html",
    ".xml": "xml",
    # Scripting
    ".py": "python",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".soql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
}

BRACE_LANGUAGES = {
    "apex", "java", "kotlin", "scala", "groovy", "csharp", "c", "cpp", "objc",
    "swift", "go", "rust", "dart", "php", "javascript", "typescript", "css",
}
PYTHON_LANGUAGES = {"python"}

# Languages whose single quote never opens a string (Rust lifetimes 'a).
_NO_SINGLE_QUOTE = {"rust"}
# Languages with backtick template literals that may span lines.
_BACKTICK = {"javascript", "typescript", "go"}


def detect_language(path: str) -> str | None:
    low = path.lower()
    dot = low.rfind(".")
    return LANGUAGE_BY_EXT.get(low[dot:]) if dot != -1 else None


# --------------------------------------------------------------------------
# Scanner: mask strings and comments so braces and headers can be read safely


def mask_source(lines: list[str], language: str) -> list[str]:
    """Return a copy of ``lines`` with string and comment contents blanked."""
    allow_single = language not in _NO_SINGLE_QUOTE
    allow_backtick = language in _BACKTICK
    out: list[str] = []
    state = "code"  # code | block_comment | string
    quote = ""
    for line in lines:
        buf: list[str] = []
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if state == "code":
                two = line[i : i + 2]
                if two == "//":
                    buf.append(" " * (n - i))
                    i = n
                elif two == "/*":
                    state = "block_comment"
                    buf.append("  ")
                    i += 2
                elif c == '"' or (c == "'" and allow_single) or (c == "`" and allow_backtick):
                    state, quote = "string", c
                    buf.append(c)
                    i += 1
                else:
                    buf.append(c)
                    i += 1
            elif state == "block_comment":
                if line.startswith("*/", i):
                    state = "code"
                    buf.append("  ")
                    i += 2
                else:
                    buf.append(" ")
                    i += 1
            else:  # string
                if c == "\\":
                    buf.append("  ")
                    i += 2
                elif c == quote:
                    state = "code"
                    buf.append(c)
                    i += 1
                else:
                    buf.append(" ")
                    i += 1
        out.append("".join(buf))
        # Ordinary quoted strings end at the line; only template literals span.
        if state == "string" and quote != "`":
            state = "code"
    return out


# --------------------------------------------------------------------------
# Block tree

@dataclass
class Block:
    open_line: int
    close_line: int
    depth: int
    children: list["Block"] = field(default_factory=list)
    header_start: int = -1
    name: str | None = None
    kind: str | None = None


def scan_blocks(masked: list[str]) -> list[Block]:
    """Build the nested block tree from masked lines; returns top-level blocks."""
    roots: list[Block] = []
    stack: list[Block] = []
    for idx, line in enumerate(masked):
        for ch in line:
            if ch == "{":
                blk = Block(open_line=idx, close_line=-1, depth=len(stack))
                (stack[-1].children if stack else roots).append(blk)
                stack.append(blk)
            elif ch == "}" and stack:
                stack.pop().close_line = idx
    last = len(masked) - 1
    for blk in stack:  # unbalanced input: close at EOF
        blk.close_line = last
    return roots


# --------------------------------------------------------------------------
# Header analysis

_TYPE_DECL_RE = re.compile(
    r"\b(class|interface|enum|trigger|record|struct|namespace|module|object|trait|impl|protocol|extension)"
    r"\s+([A-Za-z_]\w*)"
)
# `def name(`, `fun <T> Receiver.name(`, `function name(`: the name must be
# followed by a parameter list, so `def x = ...` is a variable, not a function.
_NAMED_FUNC_RE = re.compile(
    r"\b(?:fn|func|function|def|sub|proc|fun)\s+(?:<[^>]*>\s*)?(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*)\s*(?:<[^>]*>\s*)?\("
)
_ANNOTATION_TYPE_RE = re.compile(r"@interface\s+([A-Za-z_]\w*)")
_ASSIGN_FUNC_RE = re.compile(
    r"([A-Za-z_]\w*)\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_]\w*\s*=>)"
)
_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
_ANNOTATION_RE = re.compile(r"@[A-Za-z_][\w.]*(?:\s*\([^)]*\))?")
_FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z_]\w*)")
_PROPERTY_RE = re.compile(r"^[\w<>\[\],.?\s]*?\b([A-Za-z_]\w*)\s*$")

# Languages where `name { ... }` / `name(args) { ... }` is usually a call with a
# trailing lambda, so only keyword-introduced declarations count.
TRAILING_LAMBDA_LANGUAGES = {"kotlin", "groovy", "scala", "swift"}
# Languages where a method name is always preceded by a type or modifier
# (`void run(`, `def run(`), so a bare `run(` is a call, not a declaration.
TYPED_METHOD_LANGUAGES = {"java", "apex", "groovy", "csharp", "c", "cpp", "objc", "dart", "kotlin", "scala"}
# Languages with `Name { get; set; }` properties.
PROPERTY_LANGUAGES = {"apex", "csharp"}
_TYPED_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][\w<>\[\],?.]*)\s+([A-Za-z_]\w*)\s*\(")
# Kotlin property with an accessor block: `val x: T get() {` (no `=` initializer)
_KOTLIN_PROP_RE = re.compile(
    r"\b(?:val|var)\s+(?:<[^>]*>\s*)?(?:[A-Za-z_][\w.<>]*\.)?([A-Za-z_]\w*)\b[^=]*\b(?:get|set)\s*\("
)
_SPOCK_NAME_RE = re.compile(r"\bdef\s+(['\"])(.+?)\1\s*\(")

_CONTROL = {
    "if", "else", "for", "foreach", "while", "do", "switch", "case", "default", "try",
    "catch", "finally", "return", "throw", "with", "when", "match", "select", "elif",
    "using", "lock", "synchronized", "static", "new", "get", "set", "init", "unsafe",
    "fixed", "checked", "unchecked", "defer", "go", "loop", "unless", "until", "begin",
    "import", "export", "from", "package", "const", "let", "var", "val",
}
# Identifiers that can precede "(" without being the declared name.
_NOT_CALL_NAMES = {
    "if", "elif", "for", "foreach", "while", "until", "unless", "switch", "catch",
    "synchronized", "using", "lock", "return", "throw", "new", "sizeof", "typeof",
    "super", "this", "fn", "func", "function", "def", "sub", "proc", "when", "match",
}
_NOT_PROPERTY_NAMES = _CONTROL | {"fn", "func", "function", "def", "sub", "proc"}


def _header_start(lines: list[str], masked: list[str], open_line: int, floor: int) -> int:
    """Walk up from the ``{`` line over signature, annotation and comment lines.

    Stops at a blank line (checked on the raw text, so comment lines count as
    content) or at a line that ends a previous statement or block.
    """
    j = open_line
    while j - 1 >= floor:
        if not lines[j - 1].strip():
            break
        if masked[j - 1].rstrip().endswith((";", "{", "}")):
            break
        j -= 1
    return j


def describe_header(text: str, language: str | None = None, raw: str = "") -> tuple[str | None, str | None]:
    """Return (kind, name) for a block header, or (None, None) if unnamed.

    ``text`` is the masked header (strings and comments blanked); ``raw`` is
    the original, used only for string-named Spock/Groovy test methods.
    """
    m = _ANNOTATION_TYPE_RE.search(text)
    if m:
        return "annotation", m.group(1)
    text = _ANNOTATION_RE.sub(" ", text)
    cut = max(text.rfind("}"), text.rfind(";"))
    if cut != -1:
        text = text[cut + 1 :]
    text = text.strip()
    if not text:
        return None, None
    # A brace inside unclosed parentheses is an argument (object literal,
    # lambda body), never a declaration body.
    if text.count("(") > text.count(")"):
        return None, None
    if re.search(r"\bcompanion\s+object\s*$", text):
        return "object", "companion"
    m = _TYPE_DECL_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _NAMED_FUNC_RE.search(text)
    if m:
        return "function", m.group(1)
    if language == "groovy" and raw:
        m = _SPOCK_NAME_RE.search(raw)
        if m:
            return "function", m.group(2).strip()
    if language == "kotlin":
        m = _KOTLIN_PROP_RE.search(text)
        if m:
            return "property", m.group(1)
    first = _FIRST_TOKEN_RE.match(text)
    # `static {` / `synchronized (lock) {` are blocks; `static void f(` and
    # `synchronized void f(` are modifiers on a declaration.
    while first and first.group(1) in ("static", "synchronized", "default", "unsafe") and not text[first.end():].lstrip().startswith(("(", "{")) and text[first.end():].strip():
        text = text[first.end() :].strip()
        first = _FIRST_TOKEN_RE.match(text)
    if first and first.group(1) in ("get", "set") and "(" in text:
        text = text[first.end() :].strip()  # JS/C# accessor: `get name() {`
        first = _FIRST_TOKEN_RE.match(text)
    m = _ASSIGN_FUNC_RE.search(text)
    if m:
        return "function", m.group(1)
    if first and first.group(1) in _CONTROL:
        return None, None
    if "=>" in text or "->" in text:
        return None, None
    if language in TYPED_METHOD_LANGUAGES:
        if language == "kotlin":
            return None, None  # Kotlin functions always use `fun`
        for m in _TYPED_CALL_RE.finditer(text):
            type_tok, name = m.group(1), m.group(2)
            if name in _NOT_CALL_NAMES or type_tok in ("new", "return", "throw", "else", "case"):
                continue
            if type_tok in _CONTROL:
                continue
            return "function", name
        # a constructor with no modifier (`Foo(int x) {`) at the start of the header
        m = _CALL_RE.match(text)
        if m and m.group(1)[0].isupper() and m.group(1) not in _NOT_CALL_NAMES:
            return "function", m.group(1)
    else:
        for m in _CALL_RE.finditer(text):
            name = m.group(1)
            if name in _NOT_CALL_NAMES or text[: m.start()].rstrip().endswith("new"):
                continue
            return "function", name
    if "(" not in text and (language is None or language in PROPERTY_LANGUAGES):
        m = _PROPERTY_RE.match(text)
        if m and m.group(1) not in _NOT_PROPERTY_NAMES:
            return "property", m.group(1)
    return None, None


def _annotate(
    blocks: list[Block],
    lines: list[str],
    masked: list[str],
    floor: int,
    language: str | None = None,
    in_function: bool = False,
) -> None:
    cursor = floor
    for b in blocks:
        b.header_start = max(_header_start(lines, masked, b.open_line, cursor), cursor)
        head = " ".join(masked[b.header_start : b.open_line + 1])
        raw = " ".join(lines[b.header_start : b.open_line + 1])
        brace = head.rfind("{")
        if brace != -1:
            head = head[:brace]
        b.kind, b.name = describe_header(head, language, raw)
        if in_function and language in TYPED_METHOD_LANGUAGES:
            # a "method" inside a method is an anonymous class member or a
            # lambda body: not a declaration worth naming
            b.kind, b.name = None, None
        _annotate(
            b.children, lines, masked, b.open_line + 1, language,
            in_function or b.kind == "function",
        )
        cursor = b.close_line + 1


# --------------------------------------------------------------------------
# Python: indentation-based declarations

_PY_DECL_RE = re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")


def _python_blocks(lines: list[str]) -> list[Block]:
    decls = []
    for idx, line in enumerate(lines):
        m = _PY_DECL_RE.match(line)
        if m:
            decls.append((len(m.group(1).expandtabs(4)), m.group(2), m.group(3), idx))

    def py_header_start(idx: int, indent: int, floor: int) -> int:
        j = idx
        while j - 1 >= floor:
            prev = lines[j - 1]
            stripped = prev.strip()
            if not stripped:
                break
            lead = len(prev) - len(prev.lstrip())
            if lead == indent and stripped.startswith(("@", "#")):
                j -= 1
                continue
            break
        return j

    def build(items: list[tuple[int, str, str, int]], region_end: int, floor: int) -> list[Block]:
        if not items:
            return []
        level = min(i[0] for i in items)
        tops = [i for i in items if i[0] == level]
        out: list[Block] = []
        cursor = floor
        for n, (indent, kind, name, idx) in enumerate(tops):
            nxt_idx = tops[n + 1][3] if n + 1 < len(tops) else None
            inner = [i for i in items if idx < i[3] and (nxt_idx is None or i[3] < nxt_idx) and i[0] > level]
            hs = max(py_header_start(idx, indent, cursor), cursor)
            end = (py_header_start(nxt_idx, tops[n + 1][0], idx + 1) if nxt_idx is not None else region_end) - 1
            blk = Block(open_line=idx, close_line=end, depth=0, header_start=hs, name=name, kind=kind)
            blk.children = build(inner, end + 1, idx + 1)
            out.append(blk)
            cursor = end + 1
        return out

    return build(decls, len(lines), 0)


# --------------------------------------------------------------------------
# Sections

@dataclass
class CodeSection:
    start: int  # 0-based inclusive
    end: int  # exclusive
    breadcrumb: str
    kind: str


def _partition(
    lines: list[str],
    start: int,
    end: int,
    blocks: list[Block],
    crumb: str,
    kind: str,
    cfg: ChunkingConfig,
) -> list[CodeSection]:
    named = [b for b in blocks if b.name and b.header_start < b.close_line + 1]
    if not named:
        return [CodeSection(start, end, crumb, kind)]
    out: list[CodeSection] = []
    cursor = start
    for b in named:
        hs = max(b.header_start, cursor)
        b_end = b.close_line + 1
        if hs >= b_end:
            continue
        if hs > cursor:
            out.append(CodeSection(cursor, hs, crumb, kind))
        bcrumb = f"{crumb} > {b.name}" if crumb else b.name or ""
        bkind = b.kind or "block"
        size = count_tokens("\n".join(lines[hs:b_end]))
        if size > cfg.max_tokens and any(c.name for c in b.children):
            out.extend(_partition(lines, hs, b_end, b.children, bcrumb, bkind, cfg))
        else:
            out.append(CodeSection(hs, b_end, bcrumb, bkind))
        cursor = b_end
    if cursor < end:
        out.append(CodeSection(cursor, end, crumb, kind))
    return out


def code_sections(lines: list[str], language: str | None, cfg: ChunkingConfig) -> list[CodeSection]:
    """Split source lines into declaration-aligned sections."""
    if not lines:
        return []
    decls = parsed_declarations(lines, language, getattr(cfg, "parser", "auto"))
    if decls is not None:
        return _partition(lines, 0, len(lines), blocks_from_declarations(decls, lines), "", "file", cfg)
    if language in BRACE_LANGUAGES:
        masked = mask_source(lines, language)
        roots = scan_blocks(masked)
        _annotate(roots, lines, masked, 0, language)
    elif language in PYTHON_LANGUAGES:
        roots = _python_blocks(lines)
    else:
        return [CodeSection(0, len(lines), "", "file")]
    return _partition(lines, 0, len(lines), roots, "", "file", cfg)


# --------------------------------------------------------------------------
# Identifier splitting (so "AccountService" also matches "account service")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def identifier_terms(text: str, limit: int = 400) -> str:
    """Space-joined lowercase sub-words of compound identifiers in ``text``."""
    seen: set[str] = set()
    out: list[str] = []
    for ident in _IDENT_RE.findall(text):
        parts = [p.lower() for piece in ident.split("_") for p in _CAMEL_RE.findall(piece)]
        if len(parts) < 2:
            continue
        for p in parts:
            if len(p) >= 2 and p not in seen:
                seen.add(p)
                out.append(p)
                if len(out) >= limit:
                    return " ".join(out)
    return " ".join(out)


# --------------------------------------------------------------------------
# Declarations (for the graph)

@dataclass
class Declaration:
    kind: str
    name: str  # simple name
    qualified: str  # Outer.Inner.method
    start_line: int  # 1-based
    end_line: int
    depth: int


def list_declarations(lines: list[str], language: str | None, parser: str = "auto") -> list[Declaration]:
    """Every named block in the file, outermost first."""
    if not lines:
        return []
    decls = parsed_declarations(lines, language, parser)
    if decls is not None:
        return sorted(decls, key=lambda d: (d.start_line, -d.end_line))
    if language in BRACE_LANGUAGES:
        masked = mask_source(lines, language)
        roots = scan_blocks(masked)
        _annotate(roots, lines, masked, 0, language)
    elif language in PYTHON_LANGUAGES:
        roots = _python_blocks(lines)
    else:
        return []
    out: list[Declaration] = []

    def walk(blocks: list[Block], prefix: str, depth: int) -> None:
        for b in blocks:
            if not b.name:
                walk(b.children, prefix, depth)
                continue
            qualified = f"{prefix}.{b.name}" if prefix else b.name
            out.append(
                Declaration(
                    kind=b.kind or "block",
                    name=b.name,
                    qualified=qualified,
                    start_line=max(b.header_start, 0) + 1,
                    end_line=b.close_line + 1,
                    depth=depth,
                )
            )
            walk(b.children, qualified, depth + 1)

    walk(roots, "", 0)
    return out


# --------------------------------------------------------------------------
# Parser-backed path (tree-sitter), with the heuristic as fallback

_COMMENT_PREFIXES = ("//", "/*", "*", "*/", "#", "@")


def use_parser(language: str | None, parser: str = "auto") -> bool:
    """Whether tree-sitter handles ``language`` under the ``parser`` setting."""
    if parser == "heuristic":
        return False
    from . import treesitter

    ok = treesitter.supports(language)
    if parser == "treesitter" and not ok and language in treesitter.SUPPORTED:
        raise RuntimeError(
            "chunking.parser is treesitter but tree-sitter is not installed: pip install 'ctxgraph[parse]'"
        )
    return ok


def blocks_from_declarations(decls: list[Declaration], lines: list[str]) -> list[Block]:
    """Nest exact declarations into the Block tree the chunker partitions.

    The header is extended upward over doc comments and decorators so they
    travel with the declaration.
    """
    roots: list[Block] = []
    stack: list[Block] = []
    for d in sorted(decls, key=lambda d: (d.start_line, -d.end_line)):
        b = Block(open_line=d.start_line - 1, close_line=d.end_line - 1, depth=d.depth, name=d.name, kind=d.kind)
        b.header_start = _comment_header_start(lines, b.open_line)
        while stack and stack[-1].close_line < b.open_line:
            stack.pop()
        (stack[-1].children if stack else roots).append(b)
        stack.append(b)
    return roots


def _comment_header_start(lines: list[str], open_line: int) -> int:
    """Walk up over the comment block (line or block comments, decorators)
    directly above a declaration; stop at a blank line or any code."""
    j = open_line
    inside_block = False
    while j - 1 >= 0:
        prev = lines[j - 1].strip()
        if not prev:
            break
        if inside_block:
            j -= 1
            if "/*" in prev:
                inside_block = False
            continue
        if prev.startswith(_COMMENT_PREFIXES):
            j -= 1
            continue
        if prev.endswith("*/"):
            inside_block = "/*" not in prev
            j -= 1
            continue
        break
    return j


def parsed_declarations(lines: list[str], language: str | None, parser: str = "auto") -> list[Declaration] | None:
    """Declarations from tree-sitter when enabled and supported, else None."""
    if not use_parser(language, parser):
        return None
    from . import treesitter

    parsed = treesitter.parse_file("\n".join(lines), language)
    return parsed.declarations if parsed else None
