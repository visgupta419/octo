"""Path resolution: glob patterns (with **) against the repository file list."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from .gitutil import git_tracked_and_untracked

GLOB_CHARS = set("*?[")

# Directories never worth indexing even when a repo is not under git.
DEFAULT_WALK_SKIP = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".ctxgraph"}


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob into a regex over '/'-separated paths.

    Supported: ``*`` (within a segment), ``**`` (any depth), ``?``, ``[...]``.
    A pattern with no glob characters that names a directory matches
    everything under it (``docs`` behaves like ``docs/**``).
    """
    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.rstrip("/")
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                if pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    if not (GLOB_CHARS & set(pattern)):
        # Literal path: match the file itself or anything under it as a dir.
        return re.compile("^" + "".join(out) + "(?:/.*)?$")
    return re.compile("^" + "".join(out) + "$")


class PathMatcher:
    def __init__(self, include: Iterable[str], exclude: Iterable[str] = ()):
        self._include = [glob_to_regex(p) for p in include]
        self._exclude = [glob_to_regex(p) for p in exclude]

    def matches(self, relpath: str) -> bool:
        relpath = relpath.replace(os.sep, "/")
        if not any(r.match(relpath) for r in self._include):
            return False
        return not any(r.match(relpath) for r in self._exclude)


def list_repo_files(repo_root: Path) -> list[str]:
    """All candidate files under repo_root as sorted '/'-separated relpaths.

    Uses ``git ls-files`` (so .gitignore is honored) when available, otherwise
    walks the tree skipping well-known junk directories.
    """
    files = git_tracked_and_untracked(repo_root)
    if files is not None:
        return sorted({f for f in files if (repo_root / f).is_file()})

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_WALK_SKIP)
        rel_dir = os.path.relpath(dirpath, repo_root)
        for name in filenames:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def resolve_files(
    all_files: Iterable[str], include: Iterable[str], exclude: Iterable[str] = ()
) -> list[str]:
    matcher = PathMatcher(include, exclude)
    return [f for f in all_files if matcher.matches(f)]
