"""Thin git helpers. Everything degrades gracefully outside a git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout


def head_commit(repo_root: Path) -> str | None:
    """Full SHA of HEAD, or None when not a git repo / no commits yet."""
    out = _git(repo_root, "rev-parse", "--verify", "HEAD")
    return out.strip() if out else None


def is_git_repo(repo_root: Path) -> bool:
    return _git(repo_root, "rev-parse", "--is-inside-work-tree") is not None


def git_tracked_and_untracked(repo_root: Path) -> list[str] | None:
    """Files git knows about plus untracked files not ignored by .gitignore.

    Returns None when git is unavailable so callers can fall back to a walk.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    raw = out.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in raw.split("\0") if p]
