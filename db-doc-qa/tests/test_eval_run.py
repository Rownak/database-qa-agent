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


# ---- per-case bundles (dataset-agnostic: one prebuilt state per case) ----


def make_bundle(root: Path, name: str) -> Path:
    """A tiny prebuilt bundle (empty db files; the fake session never opens them); returns its state.json."""
    folder = root / "tables" / name
    folder.mkdir(parents=True)
    (folder / "data.db").write_bytes(b"")
    (folder / "docs.db").write_bytes(b"")
    state = {"db_path": "data.db", "docs_db_path": "docs.db", "column_map": {}, "schema_summary": name, "docs_summary": name}
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return folder / "state.json"


def per_case_dataset(root: Path, chunk_words: int = 60, dataset_state: str | None = None) -> Path:
    """Two cases with their own bundles and a root-level prepare.json; returns the run config path."""
    make_bundle(root, "a")
    make_bundle(root, "b")
    (root / "prepare.json").write_text(
        json.dumps({"chunking": {"chunk_words": chunk_words, "fallback_overlap": 0.15},
                    "embedding": {"model": "gemini-embedding-001", "dim": 768, "batch": 100}}),
        encoding="utf-8",
    )
    cases = [
        {"id": "qa", "question": "qa?", "tags": ["x"], "state_file": "tables/a/state.json"},
        {"id": "qb", "question": "qb?", "tags": ["y"], "state_file": "tables/b/state.json"},
    ]
    ds = {"name": "pc", "version": 1, "cases": cases}
    if dataset_state:
        ds["state_file"] = dataset_state
    (root / "dataset.yaml").write_text(yaml.safe_dump(ds), encoding="utf-8")
    cfg = {
        "name": "pc",
        "agent": {"chunking": {"chunk_words": 60}},
        "dataset": {"path": str(root / "dataset.yaml")},
        "eval": {"metrics": ["coverage"]},
    }
    (root / "run.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return root / "run.yaml"


def test_each_case_receives_its_own_bundle_and_the_bundles_survive(tmp_path):
    cfg = load_config(per_case_dataset(tmp_path / "ds"))
    metrics = run_eval(cfg, tmp_path / "run", session_factory=FakeBundleSession)
    assert [s["schema_summary"] for s in FakeBundleSession.created] == ["a", "b"]
    assert Path(FakeBundleSession.created[1]["db_path"]) == (tmp_path / "ds" / "tables" / "b" / "data.db").resolve()
    assert FakeBundleSession.closed == [False, False]
    assert (tmp_path / "ds" / "tables" / "a" / "data.db").is_file() and (tmp_path / "ds" / "tables" / "b" / "docs.db").is_file()
    assert metrics["data"]["chunking"]["chunk_words"] == 60  # prepare.json found next to dataset.yaml
    assert set(metrics["by_tag"]) == {"x", "y"}


def test_a_dataset_level_state_file_still_works_and_the_case_wins(tmp_path):
    root = tmp_path / "ds"
    make_bundle(root, "d")
    cfg = load_config(per_case_dataset(root, dataset_state="tables/d/state.json"))
    run_eval(cfg, tmp_path / "run", session_factory=FakeBundleSession)
    assert [s["schema_summary"] for s in FakeBundleSession.created] == ["a", "b"]  # the case wins

    (root / "dataset.yaml").write_text(
        yaml.safe_dump({"name": "pc", "version": 1, "state_file": "tables/d/state.json",
                        "cases": [{"id": "q", "question": "q?"}]}),
        encoding="utf-8",
    )
    FakeBundleSession.created, FakeBundleSession.closed = [], []
    run_eval(cfg, tmp_path / "run2", session_factory=FakeBundleSession)
    assert [s["schema_summary"] for s in FakeBundleSession.created] == ["d"] and FakeBundleSession.closed == [False]


def test_a_shared_prepare_json_mismatch_raises_before_any_question(tmp_path):
    cfg = load_config(per_case_dataset(tmp_path / "ds", chunk_words=80))
    with pytest.raises(ValueError, match="chunking"):
        run_eval(cfg, tmp_path / "run", session_factory=FakeBundleSession)
    assert FakeBundleSession.created == []


def test_per_case_bundles_cannot_run_in_retrieval_mode(tmp_path):
    cfg = load_config(per_case_dataset(tmp_path / "ds"), ["eval.mode=retrieval"])
    with pytest.raises(ValueError, match="agent mode"):
        run_eval(cfg, tmp_path / "run", session_factory=FakeBundleSession)


def test_prediction_answer_first_line_writes_the_first_line_and_default_the_full_answer(tmp_path):
    path = per_case_dataset(tmp_path / "ds")

    def read(run_dir):
        return [json.loads(x)["answer"] for x in (run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]

    class Multi(FakeBundleSession):
        def ask(self, question):
            return TurnResult("\nParis\nThe capital [France.md].", [], None, 1.0)

    run_eval(load_config(path, ["eval.predictions=true"]), tmp_path / "full", session_factory=Multi)
    assert read(tmp_path / "full") == ["Paris\nThe capital [France.md].", "Paris\nThe capital [France.md]."]
    run_eval(load_config(path, ["eval.predictions=true", "eval.prediction_answer=first_line"]), tmp_path / "fl", session_factory=Multi)
    assert read(tmp_path / "fl") == ["Paris", "Paris"]


def test_a_bundle_built_with_another_embedding_provider_is_rejected(tmp_path):
    path = per_case_dataset(tmp_path / "ds")
    with pytest.raises(ValueError, match="embedding"):
        run_eval(load_config(path, ["agent.embedding.provider=ollama"]), tmp_path / "run", session_factory=FakeBundleSession)
    assert FakeBundleSession.created == []
    # the server URL does not change the index, and a bundle without the new keys reads as the defaults
    run_eval(load_config(path, ["agent.embedding.base_url=http://other:1"]), tmp_path / "run2", session_factory=FakeBundleSession)
    assert len(FakeBundleSession.created) == 2
