"""Chunking: turn a document into retrievable, line-addressable pieces.

Markdown is split by heading. Each section carries its heading breadcrumb so a
chunk still makes sense out of context. Sections that exceed the chunking
budget are windowed by paragraph (fenced code blocks are never split) with a
small overlap; tiny sections are merged into their neighbour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import ChunkingConfig
from ..tokens import count_tokens

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass
class RawChunk:
    text: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    heading: str = ""  # breadcrumb like "Title > Section" or "Class > method"
    terms: str = ""  # split compound identifiers, indexed for BM25
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class _Section:
    start: int  # 0-based line index, inclusive
    end: int  # exclusive
    breadcrumb: str
    kind: str = "section"


def _is_closing_fence(marker: str, open_marker: str) -> bool:
    return marker[0] == open_marker[0] and len(marker) >= len(open_marker)


def _split_sections(lines: list[str], headings: bool) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    cur_start, cur_crumb = 0, ""
    fence: str | None = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif _is_closing_fence(marker, fence):
                fence = None
            continue
        if fence is not None or not headings:
            continue
        h = HEADING_RE.match(line)
        if not h:
            continue
        level, title = len(h.group(1)), h.group(2).strip()
        if i > cur_start:
            sections.append(_Section(cur_start, i, cur_crumb))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        cur_start, cur_crumb = i, " > ".join(t for _, t in stack)
    if len(lines) > cur_start:
        sections.append(_Section(cur_start, len(lines), cur_crumb))
    return [s for s in sections if any(l.strip() for l in lines[s.start : s.end])]


def _tokens(lines: list[str], start: int, end: int) -> int:
    return count_tokens("\n".join(lines[start:end]))


def _merge_small(sections: list[_Section], lines: list[str], cfg: ChunkingConfig) -> list[_Section]:
    merged: list[_Section] = []
    for s in sections:
        if merged:
            prev = merged[-1]
            pt, st = _tokens(lines, prev.start, prev.end), _tokens(lines, s.start, s.end)
            tiny_tail = st < cfg.min_tokens
            tiny_head = pt < cfg.min_tokens and pt + st <= cfg.target_tokens
            if tiny_tail or tiny_head:
                merged[-1] = _Section(prev.start, s.end, prev.breadcrumb or s.breadcrumb, prev.kind)
                continue
        merged.append(s)
    return merged


def _paragraphs(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Blank-line separated blocks; a fenced code block is one block."""
    paras: list[tuple[int, int]] = []
    fence: str | None = None
    ps: int | None = None
    for i in range(start, end):
        line = lines[i]
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                if ps is not None:
                    paras.append((ps, i))
                fence, ps = m.group(1), i
            elif not line.strip():
                if ps is not None:
                    paras.append((ps, i))
                    ps = None
            elif ps is None:
                ps = i
        elif m and _is_closing_fence(m.group(1), fence):
            fence = None
            paras.append((ps if ps is not None else i, i + 1))
            ps = None
    if ps is not None:
        paras.append((ps, end))
    return paras


def _hard_split(lines: list[str], start: int, end: int, cfg: ChunkingConfig) -> list[tuple[int, int]]:
    """Line-by-line split for a single block larger than max_tokens."""
    out: list[tuple[int, int]] = []
    s, tok = start, 0
    for i in range(start, end):
        t = count_tokens(lines[i])
        if i > s and tok + t > cfg.target_tokens:
            out.append((s, i))
            s, tok = i, 0
        tok += t
    if s < end:
        out.append((s, end))
    return out


def _windows(lines: list[str], start: int, end: int, cfg: ChunkingConfig) -> list[tuple[int, int]]:
    if _tokens(lines, start, end) <= cfg.max_tokens:
        return [(start, end)]
    paras: list[tuple[int, int]] = []
    for p in _paragraphs(lines, start, end):
        if _tokens(lines, *p) > cfg.max_tokens:
            paras.extend(_hard_split(lines, p[0], p[1], cfg))
        else:
            paras.append(p)
    overlap_budget = int(cfg.target_tokens * cfg.overlap_ratio)
    windows: list[tuple[int, int]] = []
    cur: list[tuple[int, int]] = []
    cur_tokens, fresh = 0, 0

    def flush() -> None:
        if cur and fresh:
            windows.append((cur[0][0], cur[-1][1]))

    for p in paras:
        t = _tokens(lines, *p)
        if cur and cur_tokens + t > cfg.target_tokens:
            flush()
            carry: list[tuple[int, int]] = []
            ct = 0
            if fresh:
                for q in reversed(cur):
                    qt = _tokens(lines, *q)
                    if ct + qt > overlap_budget:
                        break
                    carry.insert(0, q)
                    ct += qt
            cur, cur_tokens, fresh = carry, ct, 0
        cur.append(p)
        cur_tokens += t
        fresh += 1
    flush()
    return windows


def _trim(lines: list[str], start: int, end: int) -> tuple[int, int]:
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def sections_to_chunks(
    path: str,
    lines: list[str],
    sections: list[_Section],
    cfg: ChunkingConfig,
    meta: dict[str, str] | None = None,
) -> list[RawChunk]:
    """Merge tiny sections, window large ones, and emit line-addressed chunks."""
    from .code import identifier_terms

    chunks: list[RawChunk] = []
    for sec in _merge_small(sections, lines, cfg):
        for ws, we in _windows(lines, sec.start, sec.end, cfg):
            ws, we = _trim(lines, ws, we)
            if ws >= we:
                continue
            body = "\n".join(lines[ws:we])
            prefix = f"[{path}" + (f" > {sec.breadcrumb}" if sec.breadcrumb else "") + "]"
            chunk_meta = dict(meta or {})
            chunk_meta["kind"] = sec.kind
            chunks.append(
                RawChunk(
                    text=f"{prefix}\n{body}",
                    start_line=ws + 1,
                    end_line=we,
                    heading=sec.breadcrumb,
                    terms=identifier_terms(body),
                    meta=chunk_meta,
                )
            )
    return chunks


def chunk_document(
    path: str,
    text: str,
    cfg: ChunkingConfig | None = None,
    *,
    headings: bool = True,
) -> list[RawChunk]:
    """Chunk ``text`` (located at ``path``) into RawChunks.

    ``headings=False`` treats the document as plain text (no heading split).
    """
    cfg = cfg or ChunkingConfig()
    lines = text.splitlines()
    return sections_to_chunks(path, lines, _split_sections(lines, headings), cfg)


def chunk_code(
    path: str,
    text: str,
    cfg: ChunkingConfig | None = None,
    language: str | None = None,
) -> list[RawChunk]:
    """Chunk source code by declaration (see ``ingest.code``)."""
    from .code import code_sections, detect_language

    cfg = cfg or ChunkingConfig()
    language = language or detect_language(path)
    lines = text.splitlines()
    sections = [
        _Section(s.start, s.end, s.breadcrumb, s.kind)
        for s in code_sections(lines, language, cfg)
    ]
    meta = {"language": language} if language else {}
    return sections_to_chunks(path, lines, sections, cfg, meta)
