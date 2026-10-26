"""Tests for evals.run and the CLI (no network: the session factory returns canned results)."""

import json

import pytest
import yaml

from data_agent.config import load_config
from data_agent.events import TurnResult
from evals.__main__ import format_summary, main
from evals.run import run_eval

CONFIG = {
    "name": "t",
    "dataset": {"path": "evals/datasets/shipments_v1.yaml"},
    "eval": {"metrics": ["tool_sequence_match", "answer_contains", "intent_accuracy"]},
}


def search(*sections):
    passages = [{"source": s, "section_number": n} for s, n in sections]
    return {"name": "search_documents", "args": {}, "response": {"passages": passages}}


# canned results per question: one right, one wrong, the rest empty
ANSWERS = {
    "Where is shipment 12345?": TurnResult(
        "It is at the Cleveland Sort Facility.", [{"name": "run_sql", "args": {}, "response": {}}], "database", 1.0
    ),
    "What is the escalation threshold for express shipments?": TurnResult(
        "Escalate after 3 days.", [{"name": "run_sql", "args": {}, "response": {}}], "document", 3.0
    ),
}


class FakeSession:
    created = []
    closed = 0

    def __init__(self, cfg, state):
        FakeSession.created.append((cfg, state))

    def ask(self, question):
        if question.startswith("Delete"):
            raise RuntimeError("model down")
        return ANSWERS.get(question, TurnResult("", [], None, 2.0))

    def close(self):
        FakeSession.closed += 1


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSession.created, FakeSession.closed = [], 0


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return path


def test_full_run_writes_the_three_files_and_the_expected_means(cfg_file, tmp_path):
    cfg = load_config(cfg_file, ["agent.retrieval.min_dense_sim=0.55"])
    run_dir = tmp_path / "run"
    metrics = run_eval(cfg, run_dir, session_factory=FakeSession)

    assert sorted(p.name for p in run_dir.iterdir()) == ["config.yaml", "metrics.json", "results.jsonl"]
    assert json.loads((run_dir / "metrics.json").read_text()) == metrics
    # tools: only db_location is right (the empty answers call nothing, refuse_write raised)
    # but refuse_write expects [] and its errored result calls nothing, so it also matches
    assert metrics["n"] == 6 and metrics["errors"] == 1
    assert metrics["metrics"]["tool_sequence_match"] == pytest.approx(2 / 6)
    # answer_contains applies to every case; db_location 1.0, doc_express "2 days" missing -> 0.0,
    # hybrid "Tier 2" 0, conflict 0 of 2, no_policy 0, refuse_write 0
    assert metrics["metrics"]["answer_contains"] == pytest.approx(1 / 6)
    assert "intent_accuracy" not in metrics["metrics"]  # not implemented in this phase: skipped, not an error
    assert metrics["latency_p50_s"] is not None


def test_saved_config_has_the_override_and_reloads(cfg_file, tmp_path):
    cfg = load_config(cfg_file, ["agent.retrieval.min_dense_sim=0.55"])
    run_dir = tmp_path / "run"
    run_eval(cfg, run_dir, session_factory=FakeSession)
    saved = yaml.safe_load((run_dir / "config.yaml").read_text())
    assert saved["agent"]["retrieval"]["min_dense_sim"] == 0.55
    assert load_config(run_dir / "config.yaml") == cfg


def test_results_rows_and_isolation(cfg_file, tmp_path):
    cfg = load_config(cfg_file, ["dataset.case_ids=[db_location, refuse_write]", "eval.repeats=2"])
    run_dir = tmp_path / "run"
    metrics = run_eval(cfg, run_dir, session_factory=FakeSession)
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert [(r["id"], r["repeat"]) for r in rows] == [("db_location", 0), ("db_location", 1), ("refuse_write", 0), ("refuse_write", 1)]
    assert rows[0]["intent"] == "database" and rows[0]["calls"][0]["name"] == "run_sql"
    assert rows[2]["error"] == "RuntimeError: model down" and metrics["errors"] == 2
    assert len(FakeSession.created) == 4 and FakeSession.closed == 4  # one session per case and repeat, all closed
    assert FakeSession.created[0][1] is not FakeSession.created[1][1]  # separate state dicts


def test_a_failing_session_factory_is_counted_not_raised(cfg_file, tmp_path):
    def broken(cfg, state):
        raise RuntimeError("no credentials")

    metrics = run_eval(load_config(cfg_file, ["dataset.limit=2"]), tmp_path / "run", session_factory=broken)
    assert metrics["n"] == 2 and metrics["errors"] == 2 and metrics["latency_p50_s"] is None


def test_limit_and_unknown_case_id(cfg_file, tmp_path):
    metrics = run_eval(load_config(cfg_file, ["dataset.limit=2"]), tmp_path / "a", session_factory=FakeSession)
    assert metrics["n"] == 2
    with pytest.raises(ValueError, match="unknown case id"):
        run_eval(load_config(cfg_file, ["dataset.case_ids=[nope]"]), tmp_path / "b", session_factory=FakeSession)


def test_cli_creates_the_run_folder_and_prints_the_summary(cfg_file, tmp_path, monkeypatch, capsys):
    import evals.__main__ as cli

    monkeypatch.setattr(cli, "run_eval", lambda cfg, run_dir: run_eval(cfg, run_dir, session_factory=FakeSession))
    assert main([str(cfg_file), f"eval.output_dir={(tmp_path / 'runs').as_posix()}", "name=cli"]) == 0
    out = capsys.readouterr().out
    assert "cases: 6" in out and "tool_sequence_match" in out
    [run] = (tmp_path / "runs").iterdir()
    assert run.name.endswith("_cli") and (run / "metrics.json").is_file()
    assert main([]) == 2


def test_format_summary_marks_missing_metrics():
    text = format_summary({"n": 1, "errors": 0, "latency_p50_s": None, "latency_p95_s": None, "metrics": {"a": None, "b": 0.5}})
    assert "a" in text and "-" in text and "0.50" in text
