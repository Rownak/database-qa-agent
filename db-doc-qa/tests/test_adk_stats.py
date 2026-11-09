"""Offline tests of the pass@k statistics, case-run classification and verdict rules (Sprint 11, Phase 11.4).

Result files are synthetic (``evals.adk.synthetic``) and expected numbers are computed by hand.
"""

import pytest

from evals.adk import stats
from evals.adk.stats import CaseRun, Tier
from evals.adk.synthetic import case_run, runs_for, write_results

META = {"layer": "unit", "rules": [1], "critical": False}


def runs(*success: bool, case="c") -> list[CaseRun]:
    return [CaseRun(case, True, ok, failing=[] if ok else ["answer_contains"], tools="run_sql") for ok in success]


def stat(case_id, c, n, critical=False, n_requested=None, invalid_extra=0):
    members = [(case_id, runs(*([True] * c + [False] * (n - c)), case=case_id), {**META, "critical": critical})]
    return stats.case_stats(members, n_requested or n, case_id)


# --- formulas, against hand-computed values ---

def test_n5_c1_is_capable_but_not_reliable():
    """The design's example: pass@5 is 1.0 while a user gets 0.2 and three in a row never succeed."""
    assert stats.pass_at_1(1, 5) == pytest.approx(0.2)
    assert stats.pass_pow_k(1, 5) == 0.0  # C(1,3) / C(5,3)
    assert stats.pass_at_k(1, 5) == 1.0  # k = n
    assert stats.pass_at_k(1, 5, k=3) == pytest.approx(0.6)  # 1 - C(4,3)/C(5,3) = 1 - 4/10


@pytest.mark.parametrize("c,n,pow3", [(5, 5, 1.0), (4, 5, 0.4), (3, 5, 0.1), (9, 10, 0.7), (3, 3, 1.0), (2, 3, 0.0)])
def test_pass_pow_k_matches_hand_values(c, n, pow3):
    assert stats.pass_pow_k(c, n) == pytest.approx(pow3)  # C(c,3) / C(n,3), e.g. 4/10, 84/120


def test_formulas_without_valid_runs_and_with_fewer_runs_than_k():
    assert stats.pass_at_1(0, 0) == 0.0 and stats.pass_pow_k(0, 0) == 0.0 and stats.pass_at_k(0, 0) == 0.0
    assert stats.pass_pow_k(2, 2) == 1.0 and stats.pass_pow_k(1, 2) == 0.0  # k falls back to n


@pytest.mark.parametrize("c,n,klass", [(5, 5, "STABLE"), (3, 5, "FLAKY"), (0, 5, "BROKEN"), (0, 0, "NO_RESULT")])
def test_classes(c, n, klass):
    assert stats.classify(c, n) == klass


def test_percentile_and_tier_verdict():
    assert stats.percentile([5, 1, 3, 2, 4], 50) == 3 and stats.percentile([1, 2, 3, 4], 95) == 4
    assert stats.percentile([], 50) is None
    ok, bad, inv = (stats.SuiteResult("s", [], 0, 0, 0, 0, st, []) for st in ("PASS", "FAIL", "INVALID"))
    assert stats.tier_verdict([ok, ok]) == ("PASS", 0)
    assert stats.tier_verdict([ok, bad]) == ("FAIL", 1)
    assert stats.tier_verdict([ok, inv]) == ("INVALID", 2)
    assert stats.tier_verdict([inv, bad]) == ("FAIL", 1), "an observed failure is not hidden by an invalid suite"
    assert stats.tier_verdict([]) == ("INVALID", 2)


# --- case-runs from ADK result files ---

def test_case_run_classification(tmp_path):
    folder = write_results(tmp_path, "a", [
        case_run("ok"), case_run("bad", "fail", failing=("answer_contains",), tools=("search_documents",)),
        case_run("none", "invalid"), case_run("deg", "degraded", tools=("search_documents",), note=f"{stats.DEGRADED_NOTE}; BM25"),
    ])
    got = {r[0].case_id: r[0] for r in stats.collect_runs(folder).values()}
    assert got["ok"].valid and got["ok"].success and got["ok"].tools == "run_sql"
    assert got["bad"].valid and not got["bad"].success and got["bad"].failing == ["answer_contains"]
    assert got["bad"].tools == "search_documents"
    assert not got["none"].valid and got["none"].invalid_reason == "no metric results"
    assert not got["deg"].valid and got["deg"].invalid_reason == "degraded retrieval"


def test_collect_runs_reads_every_result_file_and_groups_by_case(tmp_path):
    write_results(tmp_path, "one", runs_for("a", ["pass", "fail"]), set_id="s1")
    folder = write_results(tmp_path, "two", runs_for("a", ["pass"]) + runs_for("b", ["pass"]), set_id="s2")
    collected = stats.collect_runs(folder)
    assert {k: len(v) for k, v in collected.items()} == {"a": 3, "b": 1}


# --- per-case statistics ---

def test_case_stats_ignore_invalid_runs_and_count_them():
    members = [("c", runs(True, True) + [CaseRun("c", False, False, "no metric results")], META)]
    s = stats.case_stats(members, 3, "c")
    assert (s.c, s.n, s.invalid, s.invalid_first) == (2, 2, 1, 1)
    assert s.klass == "STABLE" and s.pass1 == 1.0


def test_valid_runs_beyond_n_are_ignored():
    s = stats.case_stats([("c", runs(True, True, True, False, False), META)], 3, "c")
    assert (s.c, s.n, s.klass, s.invalid) == (3, 3, "STABLE", 0)


def test_variant_group_takes_its_worst_member():
    members = [
        ("x", runs(True, True, True, case="x"), META),
        ("x__v1", runs(True, False, False, case="x__v1"), META),
        ("x__v2", runs(True, True, False, case="x__v2"), META),
    ]
    s = stats.case_stats(members, 3, "x")
    assert (s.id, s.klass, s.c, s.n) == ("x", "FLAKY", 1, 3)
    assert s.worst_member == "x__v1" and s.members == ["x", "x__v1", "x__v2"]
    assert s.failing == ["answer_contains"]
    broken = stats.case_stats([("x", runs(True, True), META), ("x__v1", runs(False, False), META)], 2, "x")
    assert broken.klass == "BROKEN"


def test_tools_of_the_first_failed_run_are_shown():
    bad = CaseRun("c", True, False, failing=["tool_trajectory_avg_score"], tools="search_documents > run_sql")
    s = stats.case_stats([("c", [CaseRun("c", True, True, tools="run_sql"), bad], META)], 2, "c")
    assert s.tools == "search_documents > run_sql"


# --- suite verdict ---

PR = Tier("pr", n=3, min_success=2)
NIGHTLY = Tier("nightly", n=5, min_success=4, suite_mean_pass1=0.90)


def judge(cases, tier=PR, total=None, **kw):
    return stats.judge_suite("s", cases, tier, total if total is not None else len(cases) * tier.n, **kw)


def test_pass_needs_min_success_for_ordinary_cases():
    assert judge([stat("a", 3, 3), stat("b", 2, 3)]).status == "PASS"  # 2/3 meets pr
    result = judge([stat("a", 3, 3), stat("b", 1, 3)])
    assert result.status == "FAIL" and "b succeeded in 1/3" in result.reasons[0]


def test_critical_case_must_be_stable():
    result = judge([stat("a", 3, 3), stat("crit", 2, 3, critical=True)])
    assert result.status == "FAIL" and "critical case crit is FLAKY" in result.reasons[0]
    assert judge([stat("crit", 3, 3, critical=True)]).status == "PASS"


def test_mean_pass1_threshold_applies_when_set():
    cases = [stat("a", 5, 5), stat("b", 5, 5), stat("c", 4, 5)]  # mean 0.933, each c >= 4
    assert judge(cases, NIGHTLY).status == "PASS"
    low = [stat("a", 5, 5)] + [stat(f"x{i}", 4, 5) for i in range(9)]  # mean 0.82
    result = judge(low, NIGHTLY)
    assert result.status == "FAIL" and "mean pass@1 0.82 is below 0.90" in result.reasons[0]
    assert judge(low, PR).status == "PASS", "the pr tier has no mean threshold"


def test_invalid_share_over_ten_percent_makes_the_suite_invalid():
    cases = [stat(f"c{i}", 3, 3, n_requested=3) for i in range(10)]  # 30 runs
    for c in cases[:3]:
        c.invalid = 1  # 3 of 30 = 10 %: allowed
    assert judge(cases, total=30).status == "PASS"
    cases[3].invalid = 1  # 4 of 30 = 13 %
    result = judge(cases, total=30)
    assert result.status == "INVALID" and "4 of 30 runs invalid" in result.reasons[0]


def test_too_few_valid_runs_for_a_case_makes_the_suite_invalid_not_failed():
    members = [("c", runs(False) + [CaseRun("c", False, False, "no metric results")] * 2, META)]
    result = judge([stat("a", 3, 3), stats.case_stats(members, 3, "c")], total=60)
    assert result.status == "INVALID" and "fewer than 2 valid runs: c" in result.reasons[0]


def test_regressions_are_listed_and_gate_only_when_the_tier_says_so():
    baseline = {"a": "STABLE", "b": "FLAKY", "c": "STABLE"}
    cases = [stat("a", 2, 3), stat("b", 3, 3), stat("c", 3, 3)]
    soft = judge(cases, baseline=baseline)
    assert soft.regressions == ["a"] and soft.fixed == ["b"] and soft.status == "PASS"
    release = Tier("release", n=3, min_success=2, fail_on_regression=True)
    hard = judge(cases, release, baseline=baseline)
    assert hard.status == "FAIL" and "regressed against the baseline: a" in hard.reasons[0]
    assert judge(cases, release).status == "PASS", "no baseline, no regression gate"
