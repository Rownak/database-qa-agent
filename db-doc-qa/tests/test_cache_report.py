"""Tests for the run comparison and threshold sweep dev scripts (Sprint 9, Phase 9.2): canned rows, no network."""

import json

from data_agent import cache
from dev_test import phase_9_2 as compare
from dev_test import phase_9_2_sweep as sweep


def row(cid, tags, latency, f1=1.0, cache_=None, stages=None, error=None):
    r = {"id": cid, "tags": tags, "latency_s": latency, "error": error, "metrics": {"answer_f1": f1}}
    if cache_ is not None:
        r["cache"] = cache_
    if stages is not None:
        r["timing"] = {"stages": stages}
    return r


BASE = [
    row("a", ["pass_unique", "hybrid"], 4.0, 0.8, stages=[{"stage": "intent", "s": 1}, {"stage": "llm", "s": 3, "in_tok": 1000, "out_tok": 10}]),
    row("a-exact", ["pass_exact", "hybrid"], 5.0, 0.7, stages=[{"stage": "llm", "s": 3, "in_tok": 500}]),
]
ARM = [
    row("a", ["pass_unique", "hybrid"], 4.1, 0.5, {"response": "miss"}, [{"stage": "cache_lookup", "s": 0.002}, {"stage": "llm", "s": 3, "in_tok": 1000}]),
    row("a-exact", ["pass_exact", "hybrid"], 0.1, 0.8, {"response": "hit_exact"}, [{"stage": "cache_lookup", "s": 0.001}]),
]


def test_hits_and_usage_are_counted_per_pass():
    exact = compare.pass_summary(compare.of_pass(ARM, "pass_exact"))
    assert exact["hits"] == 1 and exact["usage"]["llm_calls"] == 0 and exact["latency"][0] == 0.1
    unique = compare.pass_summary(compare.of_pass(ARM, "pass_unique"))
    assert unique["hits"] == 0 and unique["usage"]["llm_calls"] == 1 and unique["usage"]["in_tok"] == 1000
    assert compare.pass_summary(compare.of_pass(BASE, "pass_unique"))["usage"]["intent_calls"] == 1


def test_errored_rows_are_left_out_of_latency_but_counted():
    s = compare.pass_summary([row("x", ["t"], 9.0, error="boom"), row("y", ["t"], 1.0)])
    assert s["n"] == 2 and s["errors"] == 1 and s["latency"] == (1.0, 1.0, 1.0)
    assert compare.pass_summary([])["latency"] is None


def test_quality_delta_flags_drops_beyond_the_tolerance():
    q = compare.quality_delta(BASE, ARM, "pass_unique")
    assert q["worse"] == ["a"] and round(q["max_drop"], 2) == 0.3
    q = compare.quality_delta(BASE, ARM, "pass_exact")
    assert q["worse"] == [] and round(q["mean_delta"], 2) == 0.1


def test_the_table_has_one_row_per_pass_and_never_pools():
    text = compare.table(BASE, ARM)
    assert "pass_unique" in text and "pass_exact" in text and "pass_stale" not in text
    assert "cache_lookup stage: n=2" in text


def test_load_run_reads_results(tmp_path):
    (tmp_path / "results.jsonl").write_text("\n".join(json.dumps(r) for r in ARM) + "\n", encoding="utf-8")
    assert [r["id"] for r in compare.load_run(tmp_path)] == ["a", "a-exact"]


def test_sweep_counts_right_and_wrong_hits_with_and_without_the_guard():
    questions = {
        "c1": "What is the limit for client 252?",
        "c1-para": "Tell me the limit of client 252.",
        "c1-near": "What is the limit for client 253?",
    }
    by_entry = {cache.text_hash(q)[:12]: cid for cid, q in questions.items()}
    nearest = cache.text_hash(questions["c1"])[:12]
    para = [row("c1-para", ["pass_paraphrase"], 1, cache_={"response": "miss", "shadow": {"similarity": 0.98, "guard": True, "nearest": nearest}})]
    near = [row("c1-near", ["pass_near_miss"], 1, cache_={"response": "miss", "shadow": {"similarity": 0.99, "guard": False, "nearest": nearest}})]
    p = sweep.hits_at(para, questions, by_entry, 0.97, sweep.canon)
    assert p["off"] == {"served": 1, "right": 1} and p["guard"] == {"served": 1, "right": 1}
    n = sweep.hits_at(near, questions, by_entry, 0.97, lambda rid: None)
    assert n["off"]["served"] == 1 and n["guard"]["served"] == 0  # the guard blocks 252 -> 253
    assert sweep.hits_at(para, questions, by_entry, 0.99, sweep.canon)["off"]["served"] == 0  # below the threshold
    assert sweep.canon("tbl-1-near") == "tbl-1" and sweep.canon("tbl-1") == "tbl-1"
