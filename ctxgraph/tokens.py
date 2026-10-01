"""Token estimation.

Milestone 1 uses a model-independent heuristic (~4 characters per token for
English prose and code). It is deliberately pluggable so a real tokenizer can
be dropped in later without touching callers.
"""

from __future__ import annotations

from typing import Callable

TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate. Never returns less than 1."""
    return max(1, (len(text) + 3) // 4)


_counter: TokenCounter = estimate_tokens


def count_tokens(text: str) -> int:
    return _counter(text)


def set_token_counter(fn: TokenCounter) -> None:
    """Swap the token counter globally (e.g. for a tiktoken-backed one)."""
    global _counter
    _counter = fn
