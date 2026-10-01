"""Render a ContextPack as markdown (for agents) or JSON (for tooling)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from .pack import ContextPack, PackChunk

BUCKET_ORDER = ("norms", "expertise", "knowledge")
_BACKTICKS = re.compile(r"`+")


def _fence_for(text: str) -> str:
    longest = max((len(m) for m in _BACKTICKS.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def _render_chunk(c: PackChunk) -> str:
    fence = _fence_for(c.text)
    head = f"### [{c.bucket}] {c.citation}"
    return f"{head}\n{fence}text\n{c.text}\n{fence}\n"


def render_markdown(pack: ContextPack) -> str:
    out: list[str] = [f'# Context pack — "{pack.query}"', ""]
    summary = (
        f"_{len(pack.chunks)} chunks, {pack.used_tokens} / {pack.budget_tokens} tokens"
        f" (from {pack.candidates} candidates"
    )
    if pack.dropped_duplicates:
        summary += f", {pack.dropped_duplicates} duplicates dropped"
    if pack.dropped_over_budget:
        summary += f", {pack.dropped_over_budget} over budget"
    summary += ")._"
    out.append(summary)
    out.append("")

    if pack.stale_sources:
        out.append("> **Warning:** stale sources contributed to this pack: "
                   + ", ".join(pack.stale_sources))
        out.append("")

    if pack.facts:
        out.append("## Facts")
        out.extend(f"- {f}" for f in pack.facts)
        out.append("")

    if not pack.chunks:
        out.append("_No matching context._")
        return "\n".join(out).rstrip() + "\n"

    for bucket in BUCKET_ORDER:
        group = [c for c in pack.chunks if c.bucket == bucket]
        if not group:
            continue
        out.append(f"## {bucket.capitalize()}")
        for c in group:
            out.append(_render_chunk(c))
    return "\n".join(out).rstrip() + "\n"


def render_json(pack: ContextPack) -> dict[str, Any]:
    d = asdict(pack)
    for c, pc in zip(d["chunks"], pack.chunks):
        c["citation"] = pc.citation
    d["buckets"] = pack.buckets
    return d


def to_json(pack: ContextPack, indent: int | None = 2) -> str:
    return json.dumps(render_json(pack), indent=indent, ensure_ascii=False)
