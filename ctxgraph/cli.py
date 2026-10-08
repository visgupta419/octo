"""ctxgraph command-line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from . import __version__
from .compile import compile_pack, render_markdown, to_json
from .config import (
    BUCKETS,
    CONFIG_FILENAME,
    DEPENDENCIES_TEMPLATE,
    OWNERSHIP_TEMPLATE,
    QUESTIONS_TEMPLATE,
    Config,
    ConfigError,
    find_config,
    load_config,
    render_init_template,
)
from .evals import DEFAULT_QUESTIONS_PATH, EvalError, load_questions, render_report, run_eval
from .graph import build_graph
from .graph.facts import describe_json, facts_for_query
from .ingest import embed_chunks, run_ingest
from .ingest.loaders import known_types
from .retrieve import QueryFilters, SearchOptions, search_detailed
from .stats import compute_stats, render_stats
from .store import Database, SchemaMismatch


@click.group()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to {CONFIG_FILENAME} (default: search the current directory and its parents).",
)
@click.version_option(__version__, prog_name="ctxgraph")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """ctxgraph: a git-native context layer for coding agents."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _load(ctx: click.Context) -> Config:
    path = ctx.obj.get("config_path") or find_config()
    if path is None:
        raise click.ClickException(f"no {CONFIG_FILENAME} found; run `ctxgraph init` first")
    try:
        return load_config(path, known_types())
    except ConfigError as exc:
        raise click.ClickException(f"{path}: {exc}") from exc


@main.command()
@click.option("--path", "target", type=click.Path(path_type=Path), default=Path("."),
              show_default=True, help="Repository directory to initialise.")
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(target: Path, force: bool) -> None:
    """Write a starter ctxgraph.yaml into the repository."""
    target = target.resolve()
    if not target.is_dir():
        raise click.ClickException(f"not a directory: {target}")
    cfg_path = target / CONFIG_FILENAME
    if cfg_path.exists() and not force:
        raise click.ClickException(f"{cfg_path} already exists (use --force to overwrite)")
    cfg_path.write_text(render_init_template(target), encoding="utf-8")
    state_dir = target / ".ctxgraph"
    state_dir.mkdir(exist_ok=True)
    # The state directory ignores itself so the SQLite index never gets committed.
    (state_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    click.echo(f"wrote {cfg_path}")
    ctx_dir = target / "ctx"
    ctx_dir.mkdir(exist_ok=True)
    for name, template in (("ownership.yaml", OWNERSHIP_TEMPLATE), ("dependencies.yaml", DEPENDENCIES_TEMPLATE)):
        p = ctx_dir / name
        if not p.exists():
            p.write_text(template, encoding="utf-8")
            click.echo(f"wrote {p}")
    questions = target / DEFAULT_QUESTIONS_PATH
    if not questions.exists():
        questions.parent.mkdir(exist_ok=True)
        questions.write_text(QUESTIONS_TEMPLATE, encoding="utf-8")
        click.echo(f"wrote {questions}")
    if (target / "sfdx-project.json").is_file():
        click.echo("detected a Salesforce DX project: added apex, lwc-aura-vf and sf-metadata sources")
    click.echo("next: edit the sources, then run `ctxgraph ingest` and `ctxgraph query \"...\"`")


@main.command()
@click.option("--full", is_flag=True, help="Rebuild every source instead of ingesting incrementally.")
@click.option("--no-embed", is_flag=True, help="Skip the embedding step even if a provider is configured.")
@click.pass_context
def ingest(ctx: click.Context, full: bool, no_embed: bool) -> None:
    """Index the configured sources into the local store."""
    cfg = _load(ctx)
    embedder = None if no_embed else _embedder(cfg)
    with Database(cfg.db_path, on_mismatch="rebuild") as db:
        if db.rebuilt:
            click.echo("index schema changed: rebuilding from scratch")
        try:
            report = run_ingest(cfg, db, full=full)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        graph = build_graph(cfg, db)
        embed_report = embed_chunks(cfg, db, embedder) if embedder else None

    head = (report.head_commit or "no commit")[:12]
    mode = "full rebuild" if full else "incremental"
    click.echo(f"ingest ({mode}) at {head} -> {cfg.db_path}")
    width = max((len(s.source_id) for s in report.sources), default=6)
    for s in report.sources:
        click.echo(
            f"  {s.source_id:<{width}}  {s.type}/{s.bucket:<9}  files={s.files:<4} "
            f"chunks={s.chunks:<5} +{s.added} -{s.removed} ={s.unchanged}"
        )
        for rel, reason in s.skipped[:10]:
            click.echo(f"      skipped {rel}: {reason}")
        if len(s.skipped) > 10:
            click.echo(f"      ... {len(s.skipped) - 10} more skipped")
    for sid in report.removed_sources:
        click.echo(f"  removed source no longer in config: {sid}")
    click.echo(
        f"total: {report.chunks} chunks (+{report.added} -{report.removed} ={report.unchanged})"
    )
    types = ", ".join(f"{k}={v}" for k, v in sorted(graph.by_type.items()))
    click.echo(
        f"graph: {graph.entities} entities ({types}), {graph.edges} edges, "
        f"{graph.mentions} doc mentions, {graph.commits} commits read"
    )
    if embed_report:
        extra = f", dropped {embed_report.dropped_other_model} from another model" if embed_report.dropped_other_model else ""
        click.echo(
            f"embeddings: +{embed_report.embedded} with {embed_report.model} in {embed_report.seconds}s "
            f"({embed_report.total} total{extra})"
        )
    elif cfg.embedding.provider == "none":
        click.echo("embeddings: off (set embedding.provider in ctxgraph.yaml for hybrid retrieval)")


def _embedder(cfg: Config):
    """The configured embedder, or None (with a warning) when it cannot be built.

    A missing package or key degrades to BM25-only retrieval rather than
    failing, so a fresh `ctxgraph init` works before any extras are installed.
    """
    from .retrieve.embed import make_embedder

    try:
        return make_embedder(cfg.embedding)
    except ConfigError as exc:
        click.echo(f"warning: embeddings disabled: {exc}", err=True)
        return None


def _reranker(cfg: Config):
    from .retrieve.rerank import make_reranker

    try:
        return make_reranker(cfg.rerank)
    except ConfigError as exc:
        click.echo(f"warning: rerank disabled: {exc}", err=True)
        return None


def search_options(cfg: Config, retriever: str = "auto", limit: int | None = None, rerank: bool | None = None) -> SearchOptions:
    """SearchOptions from config; constructs the embedder/reranker lazily."""
    embedder = _embedder(cfg) if retriever != "bm25" and cfg.embedding.provider != "none" else None
    use_rerank = cfg.rerank.enabled if rerank is None else rerank
    return SearchOptions(
        limit=limit or cfg.retrieval.candidates,
        test_penalty=cfg.retrieval.test_path_penalty,
        importance_boost=cfg.retrieval.importance_boost,
        embedder=embedder,
        reranker=_reranker(cfg) if use_rerank else None,
        rerank_top_n=cfg.rerank.top_n,
        near_duplicate_cosine=cfg.retrieval.near_duplicate_cosine,
        rrf_k=cfg.retrieval.rrf_k,
        retriever=retriever,
    )


def compile_with_facts(db: Database, text: str, hits, budget: int, cfg: Config, facts: bool = True):
    """Compile twice: the first pass decides which chunks fit, the second adds
    Facts for the entities those chunks (and the query) name."""
    cap = cfg.retrieval.max_chunks_per_file
    draft = compile_pack(text, hits, budget, max_chunks_per_file=cap)
    lines = facts_for_query(db, text, draft.chunks) if facts else []
    return compile_pack(text, hits, budget, facts=lines, max_chunks_per_file=cap)


def _open_index(cfg: Config) -> Database:
    if not cfg.db_path.exists():
        raise click.ClickException(f"no index at {cfg.db_path}; run `ctxgraph ingest` first")
    try:
        return Database(cfg.db_path)
    except SchemaMismatch as exc:
        raise click.ClickException(str(exc)) from exc


def _join(items: list[dict], n: int = 15) -> str:
    text = ", ".join(f"{i['name']} ({i['type']})" for i in items[:n])
    return text + (f" (+{len(items) - n} more)" if len(items) > n else "")


@main.command()
@click.argument("text")
@click.option("-b", "--bucket", "buckets", multiple=True, type=click.Choice(BUCKETS),
              help="Restrict to a bucket (repeatable).")
@click.option("-s", "--source", "sources", multiple=True, help="Restrict to a source id (repeatable).")
@click.option("-p", "--path", "paths", multiple=True, help="Restrict to a path prefix (repeatable).")
@click.option("--budget", type=int, default=None, help="Token budget for the pack (default from config).")
@click.option("--limit", type=int, default=None, help="Candidate chunks to retrieve before compiling (default from config).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of markdown.")
@click.option("--no-facts", is_flag=True, help="Skip the graph Facts block.")
@click.option("--no-log", is_flag=True, help="Do not record this query in the query log.")
@click.option("--retriever", type=click.Choice(["auto", "bm25", "hybrid", "vector"]), default="auto",
              show_default=True, help="auto = hybrid when embeddings exist, else BM25.")
@click.option("--rerank/--no-rerank", default=None, help="Override rerank.enabled from the config.")
@click.pass_context
def query(
    ctx: click.Context,
    text: str,
    buckets: tuple[str, ...],
    sources: tuple[str, ...],
    paths: tuple[str, ...],
    budget: int | None,
    limit: int,
    as_json: bool,
    no_facts: bool,
    no_log: bool,
    retriever: str,
    rerank: bool | None,
) -> None:
    """Compile a bounded context pack for a free-text query."""
    cfg = _load(ctx)
    filters = QueryFilters(buckets=list(buckets), sources=list(sources), path_prefixes=list(paths))
    opts = search_options(cfg, retriever, limit, rerank)
    started = time.monotonic()
    with _open_index(cfg) as db:
        result = search_detailed(db, text, filters, opts)
        hits, mode = result.hits, result.mode
        pack = compile_with_facts(db, text, hits, budget or cfg.budget_tokens_default, cfg, facts=not no_facts)
        if not no_log:
            db.log_query(
                text,
                {"buckets": list(buckets), "sources": list(sources), "paths": list(paths)},
                mode,
                len(hits),
                len(pack.chunks),
                pack.used_tokens,
                pack.budget_tokens,
                pack.dropped_over_budget,
                [h.source_id for h in hits],
                int((time.monotonic() - started) * 1000),
            )
    click.echo(to_json(pack) if as_json else render_markdown(pack).rstrip("\n"))


@main.command()
@click.argument("name")
@click.option("-t", "--type", "type_", default=None,
              help="Entity type: symbol, object, service, team, component, flow, field, file, person.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def graph(ctx: click.Context, name: str, type_: str | None, as_json: bool) -> None:
    """Describe an entity (class, object, service, file, ...) and its neighbours."""
    cfg = _load(ctx)
    with _open_index(cfg) as db:
        ents = db.find_entities(name, type_)
        if not ents:
            raise click.ClickException(
                f"no entity named {name!r}" + (f" of type {type_}" if type_ else "")
            )
        described = [describe_json(db, e) for e in ents]
    if as_json:
        click.echo(json.dumps(described, indent=2, ensure_ascii=False))
        return
    for d in described:
        click.echo(f"{d['type']}: {d['summary']}")
        for kind, items in d["outgoing"].items():
            click.echo(f"  -> {kind}: {_join(items)}")
        for kind, items in d["incoming"].items():
            click.echo(f"  <- {kind}: {_join(items)}")
        if d["mentioned_in"]:
            click.echo("  mentioned in: " + ", ".join(d["mentioned_in"]))
        click.echo()


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def stats(ctx: click.Context, as_json: bool) -> None:
    """Report context anti-patterns from the query log and the index."""
    cfg = _load(ctx)
    with _open_index(cfg) as db:
        s = compute_stats(cfg, db)
    click.echo(json.dumps(s.to_dict(), indent=2) if as_json else render_stats(s))


if __name__ == "__main__":  # pragma: no cover
    main()


@main.command("eval")
@click.option("-q", "--questions", "questions_path", type=click.Path(path_type=Path), default=None,
              help=f"Questions file (default: {DEFAULT_QUESTIONS_PATH} in the repo).")
@click.option("--budget", type=int, default=None, help="Token budget per question (default from the questions file or config).")
@click.option("--tag", "tags", multiple=True, help="Run only questions with this tag (repeatable).")
@click.option("--fail-under", type=float, default=None, help="Exit 1 when aggregate path recall is below this fraction.")
@click.option("--no-baseline", is_flag=True, help="Skip the grep-and-read baseline.")
@click.option("--retriever", type=click.Choice(["auto", "bm25", "hybrid", "vector"]), default="auto",
              show_default=True, help="Compare retrievers: run once with bm25 and once with hybrid.")
@click.option("--rerank/--no-rerank", default=None, help="Override rerank.enabled from the config.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def eval_cmd(
    ctx: click.Context,
    questions_path: Path | None,
    budget: int | None,
    tags: tuple[str, ...],
    fail_under: float | None,
    no_baseline: bool,
    retriever: str,
    rerank: bool | None,
    as_json: bool,
) -> None:
    """Score packs against evals/questions.yaml (path, candidate and entity recall)."""
    cfg = _load(ctx)
    path = questions_path or (cfg.repo_root / DEFAULT_QUESTIONS_PATH)
    try:
        defaults, questions = load_questions(path)
    except EvalError as exc:
        raise click.ClickException(str(exc)) from exc
    budget = budget or (int(defaults["budget_tokens"]) if defaults.get("budget_tokens") else None)
    opts = search_options(cfg, retriever, None, rerank)
    with _open_index(cfg) as db:
        report = run_eval(cfg, db, questions, budget_tokens=budget, baseline=not no_baseline, tags=list(tags), opts=opts)
    if not report.results:
        raise click.ClickException("no questions matched")
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else render_report(report))
    if fail_under is not None and (report.path_recall or 0.0) < fail_under:
        raise click.exceptions.Exit(1)
