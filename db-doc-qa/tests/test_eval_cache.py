"""Tests for the cache hit-rate aggregation in evals.run (Sprint 9, Phase 9.2): a fake session, no network."""

import json

import yaml

from data_agent.config import load_config
from data_agent.events import TurnResult
from evals.__main__ import format_summary
from evals.run import _cache_counts, run_eval

CASES = [
    ("a", ["pass_unique", "hybrid"], {"response": "miss"}),
    ("b", ["pass_unique", "table"], {"response": "bypass", "reason": "time_relative"}),
    ("c", ["pass_exact", "hybrid"], {"response": "hit_exact", "similarity": 1.0}),
    ("d", ["pass_exact", "table"], {"response": "hit_exact", "similarity": 1.0}),
    ("e", ["pass_paraphrase", "table"], {"response": "miss", "shadow": {"similarity": 0.99, "guard": True, "nearest": "x"}}),
    ("f", ["pass_paraphrase", "table"], {"response": "hit_semantic", "similarity": 0.98}),
]


class CachedSession:
    """Returns the canned cache outcome of the case whose question is asked; ``None`` means no cache."""

    outcomes: dict = {}

    def __init__(self, cfg, state):
        pass

    def ask(self, question):
        return TurnResult("answer", [], "database", 1.0, cache=self.outcomes.get(question))

    def close(self, delete_files=True):
        pass


def _run(tmp_path, outcomes):
    dataset = {
        "name": "d", "version": 1,
        "cases": [{"id": cid, "question": cid, "tags": tags} for cid, tags, _ in CASES],
    }
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(dataset), encoding="utf-8")
    run = {"name": "t", "dataset": {"path": str(tmp_path / "ds.yaml")}, "eval": {"metrics": ["tool_sequence_match"]}}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(run), encoding="utf-8")
    CachedSession.outcomes = outcomes
    run_dir = tmp_path / "out"
    return run_eval(load_config(tmp_path / "run.yaml"), run_dir, session_factory=CachedSession), run_dir


def test_outcomes_are_counted_overall_and_per_tag(tmp_path):
    metrics, run_dir = _run(tmp_path, {cid: cache for cid, _, cache in CASES})
    response = metrics["cache"]["all"]["response"]
    assert response["n"] == 6 and response["hits"] == 3 and response["hit_rate"] == 0.5
    assert response["outcomes"] == {"bypass": 1, "hit_exact": 2, "hit_semantic": 1, "miss": 2}
    by_tag = metrics["cache"]["by_tag"]
    assert by_tag["pass_exact"]["response"]["hit_rate"] == 1.0
    assert by_tag["pass_unique"]["response"]["hits"] == 0 and by_tag["pass_unique"]["response"]["n"] == 2
    assert by_tag["pass_paraphrase"]["response"]["outcomes"] == {"hit_semantic": 1, "miss": 1}
    assert by_tag["table"]["response"]["n"] == 4  # route tags are counted too
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[4]["cache"]["shadow"]["nearest"] == "x"  # the row keeps the whole dict
    assert json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))["cache"] == metrics["cache"]


def test_without_a_cache_the_keys_are_unchanged(tmp_path):
    metrics, run_dir = _run(tmp_path, {})
    assert "cache" not in metrics
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all("cache" not in r for r in rows)
    assert _cache_counts(rows) is None


def test_any_layer_is_counted_generically():
    rows = [
        {"tags": ["t"], "cache": {"response": "miss", "retrieval": "hit", "note": 3}},
        {"tags": ["t"], "cache": {"retrieval": "miss"}},
    ]
    counts = _cache_counts(rows)["all"]
    assert counts["retrieval"] == {"n": 2, "hits": 1, "hit_rate": 0.5, "outcomes": {"hit": 1, "miss": 1}}
    assert counts["response"]["n"] == 1 and "note" not in counts


def test_detail_entries_are_not_layers_and_bypass_reasons_are_counted():
    rows = [
        {"tags": ["t"], "cache": {"response": "bypass", "reason": "time_relative", "entry": "ab12"}},
        {"tags": ["t"], "cache": {"response": "bypass", "reason": "follow_up"}},
        {"tags": ["t"], "cache": {"response": "hit_exact", "entry": "ab12", "intent": "database"}},
        {"tags": ["t"], "cache": {"response": "miss", "error": "RuntimeError"}},
    ]
    counts = _cache_counts(rows)["all"]
    assert set(counts) == {"response"}
    assert counts["response"]["reasons"] == {"follow_up": 1, "time_relative": 1} and counts["response"]["hits"] == 1


def test_the_summary_prints_one_line_per_layer(tmp_path):
    metrics, _ = _run(tmp_path, {cid: cache for cid, _, cache in CASES})
    line = next(x for x in format_summary(metrics).splitlines() if "cache response" in x)
    assert "hit_exact 2" in line and "miss 2" in line and "hit rate 50%" in line
    plain = format_summary({"n": 1, "errors": 0, "latency_p50_s": None, "latency_p95_s": None, "metrics": {}})
    assert "cache" not in plain
