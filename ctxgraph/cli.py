"""ctxgraph command-line interface."""

from __future__ import annotations

from pathlib import Path

import click

from . import __version__
from .compile import compile_pack, render_markdown, to_json
from .config import (
    BUCKETS,
    CONFIG_FILENAME,
    render_init_template,
    Config,
    ConfigError,
    find_config,
    load_config,
)
from .ingest import run_ingest
from .ingest.loaders import known_types
from .retrieve import QueryFilters, search
from .store import Database


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
    if (target / "sfdx-project.json").is_file():
        click.echo("detected a Salesforce DX project: added apex, lwc-aura-vf and sf-metadata sources")
    click.echo("next: edit the sources, then run `ctxgraph ingest` and `ctxgraph query \"...\"`")


@main.command()
@click.option("--full", is_flag=True, help="Rebuild every source instead of ingesting incrementally.")
@click.pass_context
def ingest(ctx: click.Context, full: bool) -> None:
    """Index the configured sources into the local store."""
    cfg = _load(ctx)
    with Database(cfg.db_path) as db:
        try:
            report = run_ingest(cfg, db, full=full)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc

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


@main.command()
@click.argument("text")
@click.option("-b", "--bucket", "buckets", multiple=True, type=click.Choice(BUCKETS),
              help="Restrict to a bucket (repeatable).")
@click.option("-s", "--source", "sources", multiple=True, help="Restrict to a source id (repeatable).")
@click.option("-p", "--path", "paths", multiple=True, help="Restrict to a path prefix (repeatable).")
@click.option("--budget", type=int, default=None, help="Token budget for the pack (default from config).")
@click.option("--limit", type=int, default=50, show_default=True, help="Candidate chunks to retrieve before compiling.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of markdown.")
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
) -> None:
    """Compile a bounded context pack for a free-text query."""
    cfg = _load(ctx)
    if not cfg.db_path.exists():
        raise click.ClickException(f"no index at {cfg.db_path}; run `ctxgraph ingest` first")
    filters = QueryFilters(buckets=list(buckets), sources=list(sources), path_prefixes=list(paths))
    with Database(cfg.db_path) as db:
        hits = search(db, text, filters, limit=limit)
    pack = compile_pack(text, hits, budget or cfg.budget_tokens_default)
    click.echo(to_json(pack) if as_json else render_markdown(pack).rstrip("\n"))


if __name__ == "__main__":  # pragma: no cover
    main()
