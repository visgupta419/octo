"""Evaluation harness: does a pack contain what a good answer needs?

Questions live in ``evals/questions.yaml`` in the target repo. For each one
the harness compiles a pack exactly as ``ctxgraph query`` would and scores:

* path recall: expected files present among the pack's chunks;
* candidate recall: expected files present among retrieved candidates
  (before the budget cut), which separates ranking/budget misses from
  retrieval misses;
* entity recall: expected entities named in the Facts block or by a chunk;
* pack tokens and latency.

It also runs a deterministic "ungrounded" baseline: an agent that greps the
query terms and reads the matching files. That is a proxy, not a real agent
run, but it gives a stable tokens-to-answer comparison with no model call.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..compile import compile_pack
from ..config import Config
from ..graph.facts import facts_for_query
from ..paths import GLOB_CHARS, glob_to_regex
from ..retrieve import QueryFilters, search_mode
from ..retrieve.bm25 import STOPWORDS, query_terms
from ..store.db import Database

DEFAULT_QUESTIONS_PATH = "evals/questions.yaml"

_FACT_PATH_RE = re.compile(r"(?<![\w/.-])([\w./-]+\.\w+)#L\d+")


class EvalError(Exception):
    pass


@dataclass
class Question:
    id: str
    q: str
    expect_paths: list[str] = field(default_factory=list)
    expect_entities: list[str] = field(default_factory=list)
    buckets: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    budget_tokens: int | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class BaselineResult:
    files: int
    tokens: int
    path_hits: int
    terms: list[str]


@dataclass
class QuestionResult:
    id: str
    q: str
    path_hits: list[str]
    path_total: int
    candidate_hits: list[str]
    entity_hits: list[str]
    entity_total: int
    pack_tokens: int
    pack_chunks: int
    candidates: int
    mode: str
    duration_ms: int
    top_paths: list[str]
    baseline: BaselineResult | None = None

    @property
    def path_recall(self) -> float | None:
        return len(self.path_hits) / self.path_total if self.path_total else None

    @property
    def candidate_recall(self) -> float | None:
        return len(self.candidate_hits) / self.path_total if self.path_total else None

    @property
    def entity_recall(self) -> float | None:
        return len(self.entity_hits) / self.entity_total if self.entity_total else None


@dataclass
class EvalReport:
    results: list[QuestionResult]
    budget_tokens: int

    def _mean(self, values: list[float | None]) -> float | None:
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    @property
    def path_recall(self) -> float | None:
        return self._mean([r.path_recall for r in self.results])

    @property
    def candidate_recall(self) -> float | None:
        return self._mean([r.candidate_recall for r in self.results])

    @property
    def entity_recall(self) -> float | None:
        return self._mean([r.entity_recall for r in self.results])

    @property
    def mean_tokens(self) -> float:
        return round(statistics.mean(r.pack_tokens for r in self.results), 1) if self.results else 0.0

    def latency(self, pct: float) -> int:
        if not self.results:
            return 0
        times = sorted(r.duration_ms for r in self.results)
        return times[min(len(times) - 1, int(len(times) * pct))]

    @property
    def baseline_recall(self) -> float | None:
        vals = [r.baseline.path_hits / r.path_total for r in self.results if r.baseline and r.path_total]
        return round(sum(vals) / len(vals), 3) if vals else None

    @property
    def baseline_mean_tokens(self) -> float | None:
        vals = [r.baseline.tokens for r in self.results if r.baseline]
        return round(statistics.mean(vals), 1) if vals else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "questions": len(self.results),
            "path_recall": self.path_recall,
            "candidate_recall": self.candidate_recall,
            "entity_recall": self.entity_recall,
            "mean_pack_tokens": self.mean_tokens,
            "p50_ms": self.latency(0.5),
            "p90_ms": self.latency(0.9),
            "baseline_recall": self.baseline_recall,
            "baseline_mean_tokens": self.baseline_mean_tokens,
            "results": [
                {
                    **asdict(r),
                    "path_recall": r.path_recall,
                    "candidate_recall": r.candidate_recall,
                    "entity_recall": r.entity_recall,
                }
                for r in self.results
            ],
        }


# --------------------------------------------------------------------------
# Questions file


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise EvalError(f"{where} must be a string or list of strings")


def parse_questions(data: Any) -> tuple[dict[str, Any], list[Question]]:
    if not isinstance(data, dict):
        raise EvalError("questions file must be a mapping with a 'questions' list")
    raw = data.get("questions")
    if not isinstance(raw, list) or not raw:
        raise EvalError("questions file needs a non-empty 'questions' list")
    out: list[Question] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"questions[{i}]"
        if not isinstance(item, dict) or not item.get("q"):
            raise EvalError(f"{where} needs a 'q' text")
        qid = str(item.get("id") or f"q{i + 1}")
        if qid in seen:
            raise EvalError(f"duplicate question id {qid!r}")
        seen.add(qid)
        budget = item.get("budget_tokens")
        q = Question(
            id=qid,
            q=str(item["q"]),
            expect_paths=_str_list(item.get("expect_paths"), f"{where}.expect_paths"),
            expect_entities=_str_list(item.get("expect_entities"), f"{where}.expect_entities"),
            buckets=_str_list(item.get("buckets"), f"{where}.buckets"),
            sources=_str_list(item.get("sources"), f"{where}.sources"),
            paths=_str_list(item.get("paths"), f"{where}.paths"),
            budget_tokens=None if budget is None else int(budget),
            tags=_str_list(item.get("tags"), f"{where}.tags"),
        )
        if not q.expect_paths and not q.expect_entities:
            raise EvalError(f"{where} ({qid}) needs expect_paths or expect_entities")
        out.append(q)
    defaults = {k: v for k, v in data.items() if k != "questions"}
    return defaults, out


def load_questions(path: Path) -> tuple[dict[str, Any], list[Question]]:
    if not path.is_file():
        raise EvalError(f"no questions file at {path}; `ctxgraph init` writes a template")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_questions(data)


# --------------------------------------------------------------------------
# Matching


def path_matches(expected: str, path: str) -> bool:
    """A glob matches by pattern; a bare name matches the path or its tail."""
    if GLOB_CHARS & set(expected):
        return bool(glob_to_regex(expected).match(path))
    return path == expected or path.endswith("/" + expected)


def _fact_names(facts: list[str]) -> list[str]:
    return [line.split(" — ", 1)[0].strip().lower() for line in facts]


def entity_present(expected: str, facts: list[str], headings: list[str]) -> bool:
    e = expected.lower()
    for name in _fact_names(facts):
        if name == e or name.startswith(e + ".") or name.endswith("." + e):
            return True
    for h in headings:
        parts = [p.strip().lower() for p in h.split(" > ")]
        if e in parts or any(p.endswith("." + e) for p in parts):
            return True
    return False


# --------------------------------------------------------------------------
# Baseline: an agent that greps the query terms and reads the matching files


def build_file_index(db: Database) -> dict[str, tuple[str, int]]:
    """path -> (lowercase text, tokens) for the grep baseline."""
    index: dict[str, list[Any]] = {}
    for r in db.conn.execute("SELECT path, text, token_count FROM chunks ORDER BY path, start_line"):
        cur = index.setdefault(r["path"], ["", 0])
        cur[0] += r["text"].lower() + "\n"
        cur[1] += r["token_count"]
    return {p: (t, n) for p, (t, n) in index.items()}


def grep_baseline(
    file_index: dict[str, tuple[str, int]],
    query: str,
    expected: list[str],
    max_files: int = 20,
) -> BaselineResult:
    terms = [t.lower() for t in query_terms(query) if t.lower() not in STOPWORDS and len(t) > 2]
    if not terms:
        return BaselineResult(files=0, tokens=0, path_hits=0, terms=[])
    scored: list[tuple[int, str]] = []
    for path, (text, _tokens) in file_index.items():
        coverage = sum(1 for t in terms if t in text)
        if coverage:
            scored.append((coverage, path))
    if not scored:
        return BaselineResult(files=0, tokens=0, path_hits=0, terms=terms)
    best = max(c for c, _ in scored)
    # grep output has no ranking: the agent opens the best-covered files in path order
    chosen = sorted(p for c, p in scored if c == best)[:max_files]
    tokens = sum(file_index[p][1] for p in chosen)
    hits = sum(1 for e in expected if any(path_matches(e, p) for p in chosen))
    return BaselineResult(files=len(chosen), tokens=tokens, path_hits=hits, terms=terms)


# --------------------------------------------------------------------------
# Runner


def run_eval(
    cfg: Config,
    db: Database,
    questions: list[Question],
    budget_tokens: int | None = None,
    baseline: bool = True,
    tags: list[str] | None = None,
) -> EvalReport:
    budget = budget_tokens or cfg.budget_tokens_default
    file_index = build_file_index(db) if baseline else {}
    results: list[QuestionResult] = []
    for q in questions:
        if tags and not set(tags) & set(q.tags):
            continue
        filters = QueryFilters(buckets=q.buckets, sources=q.sources, path_prefixes=q.paths)
        qbudget = q.budget_tokens or budget
        started = time.monotonic()
        hits, mode = search_mode(
            db, q.q, filters, limit=cfg.retrieval.candidates,
            test_penalty=cfg.retrieval.test_path_penalty, importance_boost=cfg.retrieval.importance_boost,
        )
        cap = cfg.retrieval.max_chunks_per_file
        draft = compile_pack(q.q, hits, qbudget, max_chunks_per_file=cap)
        pack = compile_pack(q.q, hits, qbudget, facts=facts_for_query(db, q.q, draft.chunks), max_chunks_per_file=cap)
        duration = int((time.monotonic() - started) * 1000)

        # a Facts line that cites path#L1-20 hands the agent the pointer too
        pack_paths = [c.path for c in pack.chunks] + [m for line in pack.facts for m in _FACT_PATH_RE.findall(line)]
        cand_paths = [h.path for h in hits]
        path_hits = [e for e in q.expect_paths if any(path_matches(e, p) for p in pack_paths)]
        cand_hits = [e for e in q.expect_paths if any(path_matches(e, p) for p in cand_paths)]
        headings = [c.heading for c in pack.chunks if c.heading]
        entity_hits = [e for e in q.expect_entities if entity_present(e, pack.facts, headings)]
        top: list[str] = []
        for p in cand_paths:
            if p not in top:
                top.append(p)
            if len(top) == 5:
                break
        results.append(
            QuestionResult(
                id=q.id,
                q=q.q,
                path_hits=path_hits,
                path_total=len(q.expect_paths),
                candidate_hits=cand_hits,
                entity_hits=entity_hits,
                entity_total=len(q.expect_entities),
                pack_tokens=pack.used_tokens,
                pack_chunks=len(pack.chunks),
                candidates=len(hits),
                mode=mode,
                duration_ms=duration,
                top_paths=top,
                baseline=grep_baseline(file_index, q.q, q.expect_paths) if baseline else None,
            )
        )
    return EvalReport(results=results, budget_tokens=budget)


def _frac(hits: int, total: int) -> str:
    return f"{hits}/{total}" if total else "-"


def render_report(report: EvalReport) -> str:
    out = [f"# ctxgraph eval — {len(report.results)} questions, budget {report.budget_tokens} tokens", ""]
    width = max((len(r.id) for r in report.results), default=4)
    out.append(f"{'id':<{width}}  paths  cands  ents   tokens    ms  mode")
    for r in report.results:
        out.append(
            f"{r.id:<{width}}  {_frac(len(r.path_hits), r.path_total):<5}  "
            f"{_frac(len(r.candidate_hits), r.path_total):<5}  {_frac(len(r.entity_hits), r.entity_total):<5} "
            f"{r.pack_tokens:>7} {r.duration_ms:>5}  {r.mode}"
        )
    out.append("")

    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v:.0%}"

    out.append(
        f"aggregate: path recall {pct(report.path_recall)} | candidate recall {pct(report.candidate_recall)}"
        f" | entity recall {pct(report.entity_recall)} | mean pack {report.mean_tokens:,.0f} tokens"
        f" | p50 {report.latency(0.5)} ms, p90 {report.latency(0.9)} ms"
    )
    if report.baseline_mean_tokens is not None:
        ratio = report.baseline_mean_tokens / report.mean_tokens if report.mean_tokens else 0
        out.append(
            f"baseline (grep the query terms, read the matching files): recall {pct(report.baseline_recall)}"
            f", mean {report.baseline_mean_tokens:,.0f} tokens"
            + (f" -> packs are {ratio:.0f}x smaller" if ratio >= 1 else "")
        )
    misses = [r for r in report.results if r.path_total and len(r.path_hits) < r.path_total]
    if misses:
        out.append("")
        out.append("misses:")
        for r in misses:
            where = "retrieved but cut by budget" if len(r.candidate_hits) > len(r.path_hits) else "not retrieved"
            out.append(f"  - {r.id}: {where}; top candidates: " + ", ".join(r.top_paths[:3]))
    return "\n".join(out)
