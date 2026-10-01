"""Per-source-type loaders. A loader maps (relpath, text) -> RawChunks.

Adding a source type means adding one function here and registering it in
``LOADERS``. Types that produce graph entities instead of chunks (ownership,
edges) arrive in milestone 2; code (tree-sitter) in milestone 4.
"""

from __future__ import annotations

from typing import Callable

from ..config import ChunkingConfig, ConfigError, Source
from .chunk import RawChunk, chunk_code, chunk_document
from .code import detect_language

Loader = Callable[[Source, str, str, ChunkingConfig], list[RawChunk]]


def markdown_loader(source: Source, path: str, text: str, cfg: ChunkingConfig) -> list[RawChunk]:
    return chunk_document(path, text, cfg, headings=True)


def text_loader(source: Source, path: str, text: str, cfg: ChunkingConfig) -> list[RawChunk]:
    return chunk_document(path, text, cfg, headings=False)


def code_loader(source: Source, path: str, text: str, cfg: ChunkingConfig) -> list[RawChunk]:
    """Declaration-aware chunking; unknown extensions are chunked as text."""
    return chunk_code(path, text, cfg, language=detect_language(path))


LOADERS: dict[str, Loader] = {
    "markdown": markdown_loader,
    "text": text_loader,
    "code": code_loader,
}


def known_types() -> set[str]:
    return set(LOADERS)


def loader_for(source_type: str) -> Loader:
    try:
        return LOADERS[source_type]
    except KeyError:
        raise ConfigError(
            f"unsupported source type '{source_type}'; known types: {sorted(LOADERS)}"
        ) from None


def source_file_filter(source: Source):
    """Predicate applied to resolved paths before loading.

    A ``code`` source may set ``languages: [apex, java]`` to keep only files
    whose detected language is listed.
    """
    wanted = source.extra.get("languages") if source.type == "code" else None
    if not wanted:
        return lambda _p: True
    allowed = set(wanted)
    return lambda p: detect_language(p) in allowed
