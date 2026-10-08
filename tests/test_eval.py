from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import CONFIG, write

from ctxgraph.cli import main
from ctxgraph.config import Config, parse_config
from ctxgraph.evals import EvalError, Question, entity_present, parse_questions, path_matches, run_eval, render_report
from ctxgraph.graph import build_graph
from ctxgraph.ingest import run_ingest
from ctxgraph.store import Database

QUESTIONS = {
    "version": 1,
    "budget_tokens": 2000,
    "questions": [
        {"id": "retry", "q": "deprecated retry pattern", "expect_paths": ["CONTRIBUTING.md"], "tags": ["norms"]},
        {"id": "sqlite", "q": "why sqlite with fts5", "expect_paths": ["**/adr/*.md"], "expect_entities": ["nothing-here"]},
        {"id": "miss", "q": "kubernetes ingress annotations", "expect_paths": ["docs/missing.md"]},
        {"id": "svc", "q": "ad_decision service owner", "expect_paths": ["ad-decision.md"]},
        {"id": "any", "q": "deprecated retry pattern", "expect_any_paths": ["nope.md", "CONTRIBUTING.md"]},
    ],
}


def test_path_matches():
    assert path_matches("Foo.java", "a/b/Foo.java")
    assert path_matches("a/b/Foo.java", "a/b/Foo.java")
    assert not path_matches("Foo.java", "a/b/BarFoo.java")
    assert path_matches("**/adr/*.md", "docs/adr/0001.md")
    assert not path_matches("**/adr/*.md", "docs/0001.md")


def test_entity_present():
    facts = ["AccountService — apex class, x.cls#L1-9; members: a", "Account.Name — field"]
    assert entity_present("accountservice", facts, [])
    assert entity_present("Name", facts, [])
    assert entity_present("getAccounts", [], ["AccountService > getAccounts"])
    assert not entity_present("Billing", facts, ["AccountService > getAccounts"])


def test_parse_questions_validation():
    with pytest.raises(EvalError):
        parse_questions({"questions": []})
    with pytest.raises(EvalError):
        parse_questions({"questions": [{"q": "x"}]})  # no expectations
    _, any_q = parse_questions({"questions": [{"id": "a", "q": "x", "expect_any_paths": ["A.java", "B.java"]}]})
    assert any_q[0].expect_any_paths == ["A.java", "B.java"] and any_q[0].expect_paths == []
    with pytest.raises(EvalError):
        parse_questions({"questions": [{"id": "a", "q": "x", "expect_paths": "p"}, {"id": "a", "q": "y", "expect_paths": "p"}]})
    defaults, qs = parse_questions(QUESTIONS)
    assert defaults["budget_tokens"] == 2000 and qs[0].tags == ["norms"] and qs[1].expect_entities == ["nothing-here"]


def test_run_eval_scores_and_baseline(config: Config, db: Database):
    run_ingest(config, db)
    build_graph(config, db)
    _, qs = parse_questions(QUESTIONS)
    report = run_eval(config, db, qs, budget_tokens=2000)
    by_id = {r.id: r for r in report.results}
    assert by_id["retry"].path_recall == 1.0 and by_id["retry"].mode == "any"  # "pattern" is not in the file
    assert by_id["sqlite"].path_recall == 1.0 and by_id["sqlite"].entity_recall == 0.0
    assert by_id["miss"].path_recall == 0.0
    assert by_id["svc"].path_recall == 1.0
    assert by_id["any"].path_recall == 1.0 and by_id["any"].path_total == 1
    assert report.path_recall == 0.8
    assert all(r.baseline is not None for r in report.results)
    assert by_id["retry"].baseline.path_hits == 1 and by_id["retry"].baseline.tokens > 0
    assert report.baseline_mean_tokens is not None
    text = render_report(report)
    assert "aggregate: path recall 80%" in text and "misses:" in text and "miss:" in text
    assert "baseline" in text
    d = report.to_dict()
    assert d["questions"] == 5 and d["results"][0]["path_recall"] == 1.0

    only = run_eval(config, db, qs, budget_tokens=2000, baseline=False, tags=["norms"])
    assert [r.id for r in only.results] == ["retry"] and only.results[0].baseline is None


def _offline(repo: Path) -> str:
    import yaml

    p = repo / "ctxgraph.yaml"
    data = yaml.safe_load(p.read_text())
    data["embedding"] = {"provider": "none"}
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_eval_cli_fail_under_and_template(repo: Path):
    runner = CliRunner()
    assert runner.invoke(main, ["init", "--path", str(repo)]).exit_code == 0
    cfg = _offline(repo)
    template = repo / "evals" / "questions.yaml"
    assert template.is_file() and "questions: []" in template.read_text()
    assert runner.invoke(main, ["-c", cfg, "ingest"]).exit_code == 0
    res = runner.invoke(main, ["-c", cfg, "eval"])
    assert res.exit_code != 0 and "non-empty" in res.output

    import yaml
    template.write_text(yaml.safe_dump(QUESTIONS))
    res = runner.invoke(main, ["-c", cfg, "eval", "--fail-under", "0.9"])
    assert res.exit_code == 1, res.output
    assert "path recall 80%" in res.output
    res = runner.invoke(main, ["-c", cfg, "eval", "--fail-under", "0.5", "--json", "--no-baseline"])
    assert res.exit_code == 0, res.output
    import json
    assert json.loads(res.output)["path_recall"] == 0.8


def test_query_output_is_deterministic(repo: Path):
    runner = CliRunner()
    runner.invoke(main, ["init", "--path", str(repo)])
    cfg = _offline(repo)
    runner.invoke(main, ["-c", cfg, "ingest"])
    a = runner.invoke(main, ["-c", cfg, "query", "retry sqlite service", "--budget", "2000"]).output
    b = runner.invoke(main, ["-c", cfg, "query", "retry sqlite service", "--budget", "2000"]).output
    assert a == b and "## " in a
