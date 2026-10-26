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


# ---- prebuilt bundle (LakeQuest bank_sample): state_file, config guard, predictions ----

from pathlib import Path  # noqa: E402

from evals.lakequest import prepare  # noqa: E402
from tests.test_lakequest_prepare import SMALL, config as lake_config, lake_zip  # noqa: E402,F401  (fixtures)


class FakeBundleSession:
    created, closed = [], []

    def __init__(self, cfg, state):
        FakeBundleSession.created.append(state)

    def ask(self, question):
        calls = [{"name": "run_sql", "args": {"query": 'SELECT * FROM "client"'}, "response": {"status": "success"}}]
        return TurnResult("Yes.\nSources: SELECT 1", calls, None, 1.0)

    def close(self, delete_files=True):
        FakeBundleSession.closed.append(delete_files)


@pytest.fixture(autouse=True)
def reset_bundle_fake():
    FakeBundleSession.created, FakeBundleSession.closed = [], []


@pytest.fixture
def bundle(lake_zip, lake_config, tmp_path, fake_embed) -> Path:
    out = tmp_path / "bundle"
    prepare(lake_zip, None, None, "bank", lake_config, out, counts=SMALL)
    return out


def bundle_cfg(bundle, *overrides):
    path = bundle / "run.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "bundle",
                "agent": {"profile": "lakequest", "intent_enabled": False, "chunking": {"chunk_words": 60}},
                "dataset": {"path": str(bundle / "dataset.yaml")},
                "eval": {"metrics": ["tool_sequence_match", "answer_f1", "object_f1", "coverage"], "predictions": True},
            }
        ),
        encoding="utf-8",
    )
    return load_config(path, list(overrides))


def test_prebuilt_state_is_loaded_with_absolute_paths_and_kept(bundle, tmp_path):
    run_eval(bundle_cfg(bundle), tmp_path / "run", session_factory=FakeBundleSession)
    assert len(FakeBundleSession.created) == 7  # SMALL: 2 + 3 + 2 cases, one session each
    state = FakeBundleSession.created[0]
    assert Path(state["db_path"]).is_absolute() and Path(state["db_path"]) == (bundle / "bank.db").resolve()
    assert Path(state["docs_db_path"]) == (bundle / "docs.db").resolve()
    assert state["column_map"] == {} and "upload_b64" not in state
    assert (bundle / "bank.db").is_file() and (bundle / "docs.db").is_file()
    assert FakeBundleSession.closed == [False] * 7  # the shared bundle is never deleted


def test_predictions_jsonl_has_one_row_per_case(bundle, tmp_path):
    run_dir = tmp_path / "run"
    run_eval(bundle_cfg(bundle), run_dir, session_factory=FakeBundleSession)
    rows = [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    ids = [c["id"] for c in yaml.safe_load((bundle / "dataset.yaml").read_text(encoding="utf-8"))["cases"]]
    assert [r["qa_id"] for r in rows] == ids
    assert all(r["answer"] == "Yes." for r in rows)  # the "Sources:" line is removed, as answer_f1 scores it
    assert all(isinstance(r["object_ids"], list) and r["object_ids"] == ["client"] for r in rows)
    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert results[0]["object_ids"] == ["client"]


def test_no_predictions_file_by_default(bundle, tmp_path):
    run_dir = tmp_path / "run"
    run_eval(bundle_cfg(bundle, "eval.predictions=false"), run_dir, session_factory=FakeBundleSession)
    assert not (run_dir / "predictions.jsonl").exists()


def test_metrics_have_per_tag_means_and_the_bundle_info(bundle, tmp_path):
    run_dir = tmp_path / "run"
    metrics = run_eval(bundle_cfg(bundle), run_dir, session_factory=FakeBundleSession)
    assert {t: metrics["by_tag"][t]["n"] for t in ("hybrid", "table", "document")} == SMALL
    assert metrics["by_tag"]["table"]["metrics"]["tool_sequence_match"] == 1.0  # run_sql only
    assert metrics["by_tag"]["hybrid"]["metrics"]["tool_sequence_match"] == 0.0
    assert metrics["metrics"]["coverage"] == 1.0 and metrics["data"]["chunking"]["chunk_words"] == 60
    assert json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))["data"]["counts"]["cases"] == 7


def test_a_chunking_mismatch_raises_before_any_question(bundle, tmp_path):
    with pytest.raises(ValueError, match="chunking"):
        run_eval(bundle_cfg(bundle, "agent.chunking.chunk_words=80"), tmp_path / "run", session_factory=FakeBundleSession)
    assert FakeBundleSession.created == []


def test_an_embedding_mismatch_raises(bundle, tmp_path):
    with pytest.raises(ValueError, match="embedding"):
        run_eval(bundle_cfg(bundle, "agent.embedding.dim=256"), tmp_path / "run", session_factory=FakeBundleSession)


def test_a_prebuilt_dataset_cannot_run_in_retrieval_mode(bundle, tmp_path):
    with pytest.raises(ValueError, match="agent mode"):
        run_eval(bundle_cfg(bundle, "eval.mode=retrieval"), tmp_path / "run", session_factory=FakeBundleSession)
