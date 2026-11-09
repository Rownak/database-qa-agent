"""Offline tests of the registry, the headless runner and the report (Sprint 11, Phase 11.4).

ADK is replaced by a fake evaluator that reads the real generated eval sets and writes synthetic result files in
ADK's format, so the whole path (select, run, rerun invalid, statistics, verdict, files, exit code) runs without
a model call.
"""

import json
from collections import defaultdict
from pathlib import Path

import pytest

from evals.adk import report, suite
from evals.adk.stats import Tier
from evals.adk.synthetic import case_run, write_results

REGISTRY = suite.load_registry()


class FakeAdk:
    """A fake ``Evaluator``. ``outcome(case_id, k)`` gives the result of the k-th run (from 0) of a case, over all calls."""

    def __init__(self, outcome=lambda case_id, k: "pass", boom: Exception | None = None):
        self.outcome, self.boom, self.counts, self.calls = outcome, boom, defaultdict(int), []

    def __call__(self, entry, folder, num_runs, agents_dir):
        self.calls.append((entry.name, Path(folder), num_runs))
        if self.boom:
            raise self.boom
        runs = []
        for test_file in sorted(Path(folder).rglob("*.test.json")):
            for case in json.loads(test_file.read_text(encoding="utf-8"))["eval_cases"]:
                for _ in range(num_runs):
                    k = self.counts[case["eval_id"]]
                    self.counts[case["eval_id"]] += 1
                    runs.append(case_run(case["eval_id"], self.outcome(case["eval_id"], k)))
        write_results(agents_dir, f"run{len(self.calls)}", runs)


def run(tmp_path, fake, *argv):
    code = suite.main([*argv, "--out-dir", str(tmp_path)], evaluator=fake)
    folders = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    return code, (folders[-1] if folders else None)


def read(folder):
    return json.loads((folder / "report.json").read_text(encoding="utf-8"))


# --- registry ---

def test_registry_matches_the_design_and_the_generated_sets():
    assert [s.name for s in REGISTRY.suites] == [
        "unit_routing", "unit_hybrid", "memory", "adversarial", "regression", "regression_cache"]
    assert {k: (t.n, t.min_success, t.suite_mean_pass1) for k, t in REGISTRY.tiers.items()} == {
        "pr": (3, 2, None), "nightly": (5, 4, 0.90), "release": (10, 9, 0.95)}
    assert REGISTRY.tiers["release"].judges and REGISTRY.tiers["release"].fail_on_regression
    assert next(s for s in REGISTRY.suites if s.name == "regression_cache").agent_config == "configs/eval/cache_session.yaml"
    for s in REGISTRY.suites:
        assert (suite.ROOT / s.path / "manifest.json").is_file() and (suite.ROOT / s.agent_config).is_file()


def test_selection_by_tier_and_by_name():
    names = lambda *a: [s.name for s in suite.select(REGISTRY, *a)]  # noqa: E731
    assert names("pr") == ["unit_routing", "unit_hybrid", "adversarial", "regression"]
    assert names("nightly") == [s.name for s in REGISTRY.suites]
    assert names("pr", ["memory"]) == ["memory"], "--suite overrides the tier's list"
    with pytest.raises(ValueError, match="unknown suite"):
        suite.select(REGISTRY, "pr", ["nope"])
    with pytest.raises(ValueError, match="unknown tier"):
        suite.select(REGISTRY, "weekly")


# --- verdicts and exit codes ---

def test_all_cases_pass_exit_0_and_report_files(tmp_path, capsys):
    fake = FakeAdk()
    code, folder = run(tmp_path, fake, "--suite", "unit_routing", "--tier", "pr")
    assert code == 0
    data = read(folder)
    assert data["status"] == "PASS" and data["exit_code"] == 0
    assert data["meta"]["tier"] == "pr" and data["meta"]["n"] == 3 and data["meta"]["instruction_sha"]
    assert data["meta"]["models"] == ["gemini-3.5-flash-lite"]
    (s,) = data["suites"]
    assert s["counts"]["STABLE"] == 9 and s["mean_pass1"] == 1.0 and s["critical"] == {"stable": 1, "total": 1}
    assert (folder / "report.md").read_text(encoding="utf-8").count("unit_routing") >= 1
    assert sorted(p.name for p in (folder / "eval_history").iterdir()) == ["unit_routing__run1.evalset_result.json"]
    assert not (folder / "_work").exists()
    out = capsys.readouterr().out
    assert "Tier pr · n=3" in out and "PASS (exit 0)" in out


def test_each_eval_case_is_run_n_times_and_suites_run_in_order(tmp_path):
    fake = FakeAdk()
    run(tmp_path, fake, "--tier", "pr", "--n", "2")
    assert [c[0] for c in fake.calls] == ["unit_routing", "unit_hybrid", "adversarial", "regression"]
    assert {c[2] for c in fake.calls} == {2}
    assert fake.counts["unit_db_lookup_by_id"] == 2 and fake.counts["unit_db_lookup_by_id__v1"] == 2


def test_critical_case_failing_every_run_exits_1(tmp_path, capsys):
    fake = FakeAdk(lambda c, k: "fail" if c == "edge_refuse_write" else "pass")
    code, folder = run(tmp_path, fake, "--suite", "unit_routing")
    assert code == 1
    s = read(folder)["suites"][0]
    assert s["status"] == "FAIL" and s["critical_failures"] == ["edge_refuse_write"]
    case = next(c for c in s["cases"] if c["id"] == "edge_refuse_write")
    assert case["klass"] == "BROKEN" and case["failing"] == ["tool_trajectory_avg_score"] and case["tools"] == "run_sql"
    out = capsys.readouterr().out
    assert "Not stable" in out and "edge_refuse_write" in out and "critical case edge_refuse_write is BROKEN" in out


def test_variant_group_is_judged_by_its_worst_member(tmp_path):
    fake = FakeAdk(lambda c, k: "fail" if c == "unit_db_lookup_by_id__v1" else "pass")
    code, folder = run(tmp_path, fake, "--suite", "unit_routing")
    s = read(folder)["suites"][0]
    group = next(c for c in s["cases"] if c["id"] == "unit_db_lookup_by_id")
    assert group["klass"] == "BROKEN" and group["worst_member"] == "unit_db_lookup_by_id__v1"
    assert len(group["members"]) == 3 and len(s["cases"]) == 9, "variants are not counted as extra cases"
    assert code == 1 and s["status"] == "FAIL"


def test_invalid_runs_are_rerun_once_and_replaced(tmp_path):
    fake = FakeAdk(lambda c, k: "invalid" if c == "unit_db_count_by_status" and k == 0 else "pass")
    code, folder = run(tmp_path, fake, "--suite", "unit_routing")
    assert code == 0
    assert [c[0] for c in fake.calls] == ["unit_routing", "unit_routing"], "one rerun, only for the short case"
    rerun_cases = {c for c, k in fake.counts.items() if k == 4}
    assert rerun_cases == {"unit_db_count_by_status"}
    case = next(c for c in read(folder)["suites"][0]["cases"] if c["id"] == "unit_db_count_by_status")
    assert (case["c"], case["n"], case["invalid"], case["invalid_first"]) == (3, 3, 0, 1)


def test_too_many_invalid_runs_is_invalid_not_fail(tmp_path, capsys):
    fake = FakeAdk(lambda c, k: "invalid")
    code, folder = run(tmp_path, fake, "--suite", "unit_routing")
    assert code == 2
    s = read(folder)["suites"][0]
    assert s["status"] == "INVALID" and s["invalid_runs"] == 63 and "runs invalid after rerun" in s["reasons"][0]
    assert "INVALID (exit 2)" in capsys.readouterr().out


def test_evaluator_crash_is_invalid_and_does_not_raise(tmp_path):
    code, folder = run(tmp_path, FakeAdk(boom=RuntimeError("402 credits")), "--suite", "unit_routing")
    assert code == 2
    s = read(folder)["suites"][0]
    assert s["status"] == "INVALID" and any("RuntimeError: 402 credits" in r for r in s["reasons"])


def test_degraded_retrieval_counts_as_invalid(tmp_path):
    class Degraded(FakeAdk):
        def __call__(self, entry, folder, num_runs, agents_dir):
            self.calls.append((entry.name, folder, num_runs))
            runs = [case_run(c["eval_id"], "degraded", tools=("search_documents",), note="Semantic search was unavailable; BM25")
                    for f in Path(folder).rglob("*.test.json") for c in json.loads(f.read_text(encoding="utf-8"))["eval_cases"]
                    for _ in range(num_runs)]
            write_results(agents_dir, f"run{len(self.calls)}", runs)

    code, folder = run(tmp_path, Degraded(), "--suite", "regression")
    assert code == 2 and read(folder)["suites"][0]["status"] == "INVALID"


def test_adversarial_runs_both_match_type_folders(tmp_path):
    fake = FakeAdk()
    code, folder = run(tmp_path, fake, "--suite", "adversarial")
    assert code == 0
    files = sorted(p.name for p in (folder / "eval_history").iterdir())
    assert files == ["adversarial__run1.evalset_result.json"]
    assert read(folder)["suites"][0]["counts"]["STABLE"] == 7


def test_baseline_regressions_and_fixes(tmp_path, capsys):
    _, base = run(tmp_path, FakeAdk(lambda c, k: "fail" if c == "edge_no_documents" else "pass"), "--suite", "unit_routing")
    capsys.readouterr()
    code, folder = run(tmp_path / "second", FakeAdk(), "--suite", "unit_routing", "--baseline", str(base))
    data = read(folder)
    s = data["suites"][0]
    assert code == 0
    assert s["fixed"] == ["edge_no_documents"] and s["regressions"] == []
    out = capsys.readouterr().out
    assert "Regressions vs baseline" in out and "Fixed since baseline: edge_no_documents" in out


def test_regression_against_a_stable_baseline_is_reported(tmp_path, capsys):
    _, base = run(tmp_path / "a", FakeAdk(), "--suite", "unit_routing")
    capsys.readouterr()
    flaky = lambda c, k: "fail" if c == "unit_doc_escalation_tiers" and k == 1 else "pass"  # noqa: E731
    code, folder = run(tmp_path / "b", FakeAdk(flaky), "--suite", "unit_routing", "--baseline", str(base))
    assert read(folder)["suites"][0]["regressions"] == ["unit_doc_escalation_tiers"] and code == 0
    assert "unit_doc_escalation_tiers (STABLE -> FLAKY)" in capsys.readouterr().out


def test_release_tier_fails_on_a_regression(tmp_path):
    _, base = run(tmp_path / "a", FakeAdk(), "--suite", "regression", "--tier", "release", "--n", "3")
    flaky = lambda c, k: "fail" if k == 1 else "pass"  # noqa: E731
    code, folder = run(tmp_path / "b", FakeAdk(flaky), "--suite", "regression", "--tier", "release", "--n", "3", "--baseline", str(base))
    s = read(folder)["suites"][0]
    assert code == 1 and s["regressions"] == ["reg_empty_state_build"]
    assert any("regressed against the baseline" in r for r in s["reasons"])


def test_min_success_is_capped_by_n(tmp_path):
    code, folder = run(tmp_path, FakeAdk(), "--suite", "regression", "--tier", "release", "--n", "2")
    assert code == 0 and read(folder)["meta"]["min_success"] == 2


# --- report ---

def test_dashboard_content(tmp_path):
    fake = FakeAdk(lambda c, k: "fail" if c in ("edge_refuse_write", "unit_doc_escalation_tiers") and k != 0 else "pass")
    _, folder = run(tmp_path, fake, "--suite", "unit_routing", "--suite", "regression")
    text = (folder / "report.md").read_text(encoding="utf-8")
    assert text.startswith("```\nTier pr · n=3 · commit ")
    for word in ("Suite", "Cases", "Stable", "Flaky", "Broken", "Critical", "pass@1", "pass^3", "Invalid", "Status",
                 "unit_routing", "regression", "TOTAL", "FAIL (exit 1)", "Not stable", "unit_doc_escalation_tiers",
                 "Coverage gaps: rule 11 (known)", "Cost: 66 runs"):
        assert word in text, word
    data = read(folder)
    assert data["coverage_gaps"] == ["rule 11 (known)"]
    assert data["suites"][0]["critical"]["total"] == 1


def test_report_json_has_the_documented_fields(tmp_path):
    _, folder = run(tmp_path, FakeAdk(), "--suite", "regression")
    data = read(folder)
    assert set(data) == {"meta", "suites", "status", "exit_code", "coverage_gaps"}
    assert {"tier", "n", "commit", "instruction_sha", "golden_sha", "models", "date", "baseline"} <= set(data["meta"])
    (s,) = data["suites"]
    assert {"counts", "critical", "mean_pass1", "mean_pass3", "runs", "invalid_runs", "regressions", "fixed"} <= set(s)
    assert {"c", "n", "pass1", "pass3", "passk", "klass", "failing", "tools", "invalid"} <= set(s["cases"][0])


# --- capture and usage errors ---

def test_capture_describes_the_agents_run(tmp_path, capsys):
    fake = FakeAdk()
    code = suite.main(["--capture", "adv_urgent_write"], evaluator=fake)
    out = capsys.readouterr().out
    assert code == 0 and fake.calls[0][0] == "adversarial" and fake.calls[0][2] == 1
    assert "capture of adv_urgent_write" in out and "turn 1 q: q" in out and "tools: run_sql({})" in out
    assert fake.counts == {"adv_urgent_write": 1}, "only the captured case ran"


def test_usage_errors_exit_2(tmp_path, capsys):
    assert suite.main(["--suite", "nope", "--out-dir", str(tmp_path)], evaluator=FakeAdk()) == 2
    assert suite.main(["--tier", "weekly", "--out-dir", str(tmp_path)], evaluator=FakeAdk()) == 2
    assert suite.main(["--n", "0", "--out-dir", str(tmp_path)], evaluator=FakeAdk()) == 2
    assert suite.main(["--capture", "nope"], evaluator=FakeAdk()) == 2
    assert "error:" in capsys.readouterr().err


def test_filtered_copy_keeps_only_the_requested_cases(tmp_path):
    kept = suite.filtered_copy(suite.ROOT / "evals/adk/adversarial", {"adv_urgent_write", "adv_sql_in_question"}, tmp_path)
    assert kept == 2
    names = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert names == ["exact/adversarial_exact.test.json", "exact/test_config.json",
                     "in_order/adversarial_in_order.test.json", "in_order/test_config.json"]
    cases = json.loads((tmp_path / "exact" / "adversarial_exact.test.json").read_text(encoding="utf-8"))["eval_cases"]
    assert [c["eval_id"] for c in cases] == ["adv_urgent_write"]


def test_agent_from_config_swaps_and_restores_root_agent(monkeypatch):
    import data_agent.agent as agent_module

    before = agent_module.root_agent
    monkeypatch.delenv("DATA_AGENT_CONFIG", raising=False)
    with suite.agent_from_config(suite.ROOT / "configs/eval/cache_session.yaml"):
        assert agent_module.root_agent is not before
        assert agent_module.root_agent.generate_content_config.temperature == 0.0
    assert agent_module.root_agent is before
    import os
    assert "DATA_AGENT_CONFIG" not in os.environ


def test_tier_object_defaults():
    assert Tier("x", n=1, min_success=1).suite_mean_pass1 is None
