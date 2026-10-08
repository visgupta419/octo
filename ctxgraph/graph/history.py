"""Git history: authorship, recency and co-change edges."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from ..config import Config
from .model import FILE, PERSON, GraphBuild, ent_id, file_id

_REC, _UNIT = "\x1e", "\x1f"


def git_history(repo_root: Path, max_commits: int) -> list[tuple[str, str, str, str, list[str]]]:
    """[(sha, author name, author email, date, [paths])] newest first."""
    try:
        out = subprocess.run(
            [
                "git", "log", "--no-merges", f"-n{max_commits}",
                f"--format={_REC}%H{_UNIT}%an{_UNIT}%ae{_UNIT}%as", "--name-only", "--", ".",
            ],
            cwd=repo_root, capture_output=True, text=True, check=True, errors="replace",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    commits = []
    for rec in out.split(_REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, body = rec.partition("\n")
        parts = head.split(_UNIT)
        if len(parts) != 4:
            continue
        files = [ln.strip() for ln in body.splitlines() if ln.strip()]
        commits.append((parts[0], parts[1], parts[2], parts[3], files))
    return commits


def extract_history(cfg: Config, b: GraphBuild, indexed: set[str]) -> int:
    commits = git_history(cfg.repo_root, cfg.graph.history_max_commits)
    if not commits:
        return 0
    per_author: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    author_name: dict[str, str] = {}
    author_last: dict[str, str] = {}
    file_commits: dict[str, int] = defaultdict(int)
    file_last: dict[str, tuple[str, str]] = {}
    cochange: dict[tuple[str, str], int] = defaultdict(int)
    for _sha, name, email, date, files in commits:
        files = [f for f in files if f in indexed]
        if not files:
            continue
        key = email or name
        author_name.setdefault(key, name)
        author_last[key] = max(author_last.get(key, ""), date)
        for f in files:
            per_author[key][f] += 1
            file_commits[f] += 1
            if f not in file_last:  # newest first
                file_last[f] = (date, name)
        if len(files) <= cfg.graph.cochange_max_files:
            for a, c in combinations(sorted(files), 2):
                cochange[(a, c)] += 1

    for f, n in file_commits.items():
        fid = file_id(f)
        if b.has(fid):
            date, who = file_last[f]
            b.set_attrs(fid, commits=n, last_changed=date, last_author=who)
    for key, files in per_author.items():
        pid = b.add_entity(PERSON, author_name[key], id_=ent_id(PERSON, key), email=key, last_active=author_last[key])
        for f, n in files.items():
            fid = file_id(f)
            if b.has(fid):
                b.add_edge(pid, fid, "authored", commits=n)

    per_file: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (a, c), n in cochange.items():
        if n >= cfg.graph.cochange_min:
            per_file[a].append((n, c))
            per_file[c].append((n, a))
    seen: set[tuple[str, str]] = set()
    for f, partners in per_file.items():
        for n, other in sorted(partners, reverse=True)[:10]:
            key = tuple(sorted((f, other)))
            if key in seen:
                continue
            seen.add(key)
            if b.has(file_id(key[0])) and b.has(file_id(key[1])):
                b.add_edge(file_id(key[0]), file_id(key[1]), "co_changed", count=n)
    return len(commits)
