"""Tests for the retrieval evaluation mode and the mrr metric (no network: fake_embed)."""

import json

import pytest
import yaml

from data_agent.config import load_config
from data_agent.events import TurnResult
from evals.dataset import EvalCase, Expected, ExpectedSection
from evals.metrics import METRICS
from evals.run import run_eval

POLICY = "shipment_delay_policy.pdf"
QUESTION = "Express Shipments escalation threshold"


def dataset(tmp_path, section, extra_cases=()):
    path = tmp_path / "ds.yaml"
    cases = [
        {"id": "express", "question": QUESTION, "expected": {"sections": [{"source": POLICY, "section": section}]}},
        {"id": "no_sections", "question": "Where is shipment 12345?", "expected": {"tools": ["run_sql"]}},
        *extra_cases,
    ]
    path.write_text(yaml.safe_dump({"name": "d", "version": 1, "cases": cases}), encoding="utf-8")
    return path


def config(tmp_path, ds, *overrides):
    path = tmp_path / "run.yaml"
    path.write_text(
        yaml.safe_dump({"name": "r", "dataset": {"path": str(ds)}, "eval": {"mode": "retrieval"}}), encoding="utf-8"
    )
    return load_config(path, ["agent.retrieval.mode=bm25", *overrides])


def passages(*sections):
    return TurnResult("", [{"name": "search_documents", "args": {}, "response": {
        "passages": [{"source": s, "section_number": n} for s, n in sections]}}], None, 0.0)


def test_mrr_is_the_reciprocal_rank_of_the_first_expected_section():
    case = EvalCase(id="c", question="q", expected=Expected(sections=[ExpectedSection(source=POLICY, section="3.2")]))
    f = METRICS["mrr"]
    assert f(case, passages((POLICY, "3.2"))) == 1.0
    assert f(case, passages((POLICY, "2.4"), ("x.txt", "3.2"), (POLICY, "3.2(a)-(c)"))) == pytest.approx(1 / 3)
    assert f(case, passages((POLICY, "2.4"))) == 0.0
    assert f(EvalCase(id="c", question="q"), passages((POLICY, "3.2"))) is None


def test_retrieval_mode_finds_the_expected_section(tmp_path, fake_embed):
    cfg = config(tmp_path, dataset(tmp_path, "3.2"))
    run_dir = tmp_path / "run"
    metrics = run_eval(cfg, run_dir, session_factory=lambda *a: pytest.fail("no agent session in retrieval mode"))
    assert metrics["n"] == 1 and metrics["errors"] == 0  # the case without sections is skipped
    assert metrics["metrics"] == {"section_recall": 1.0, "mrr": 1.0}
    row = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
    assert row["id"] == "express" and row["answer"] == "" and row["calls"][0]["name"] == "search_documents"
    assert [t for _, t in fake_embed] == ["RETRIEVAL_DOCUMENT"]  # chunks embedded once; bm25 embeds no query


def test_scores_drop_when_the_expected_section_is_wrong(tmp_path, fake_embed):
    metrics = run_eval(config(tmp_path, dataset(tmp_path, "9.9")), tmp_path / "run")
    assert metrics["metrics"] == {"section_recall": 0.0, "mrr": 0.0}


def test_hybrid_mode_embeds_each_query(tmp_path, fake_embed):
    cfg = config(tmp_path, dataset(tmp_path, "3.2"), "agent.retrieval.mode=hybrid")
    run_eval(cfg, tmp_path / "run")
    assert [t for _, t in fake_embed] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def test_agent_only_metrics_are_ignored_in_retrieval_mode(tmp_path, fake_embed):
    cfg = config(tmp_path, dataset(tmp_path, "3.2"), "eval.metrics=[tool_sequence_match, mrr]")
    assert list(run_eval(cfg, tmp_path / "run")["metrics"]) == ["mrr"]


def test_bm25_only_example_config_loads():
    cfg = load_config("configs/eval/bm25_only.yaml")
    assert cfg.agent.retrieval.mode == "bm25" and cfg.eval.mode == "retrieval"
    assert cfg.eval.metrics == ["section_recall", "mrr"]
