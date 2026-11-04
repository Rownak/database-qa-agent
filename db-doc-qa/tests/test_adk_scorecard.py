"""Offline tests of the eval-history scorecard (``dev_test/phase_10_3.py``), on hand-built result files."""

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_metrics import EvalMetricResult, EvalMetricResultPerInvocation
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult
from google.adk.evaluation.evaluator import EvalStatus
from google.genai import types

from dev_test import phase_10_3 as card

P, F = EvalStatus.PASSED, EvalStatus.FAILED


def _metric(name, score, status):
    return EvalMetricResult(metric_name=name, threshold=1.0, score=score, eval_status=status)


def _run(eval_id, status, traj, args, tools):
    """One run of one case; ``traj`` / ``args`` are scores or ``None`` (not produced)."""
    inv = Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text="q")]),
        intermediate_data=IntermediateData(tool_uses=[types.FunctionCall(name=t, args={}) for t in tools]),
    )
    metrics = [
        _metric(n, s, P if s == 1.0 else F)
        for n, s in (("tool_trajectory_avg_score", traj), ("tool_args_contain", args))
        if s is not None
    ]
    return EvalCaseResult(
        eval_set_id="s1", eval_id=eval_id, final_eval_status=status, overall_eval_metric_results=metrics,
        eval_metric_result_per_invocation=[EvalMetricResultPerInvocation(actual_invocation=inv)], session_id="x",
    )


def _write(folder, name, set_id, ts, runs):
    result = EvalSetResult(
        eval_set_result_id=name, eval_set_result_name=name, eval_set_id=set_id, eval_case_results=runs,
        creation_timestamp=ts,
    )
    (folder / f"{name}.evalset_result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")


def test_scorecard_rows_show_the_worst_run_and_what_the_agent_did(tmp_path):
    runs = [
        _run("ok_case", P, 1.0, 1.0, ["run_sql"]),
        _run("ok_case", P, 1.0, 1.0, ["run_sql"]),
        _run("flaky", P, 1.0, 1.0, ["run_sql", "search_documents"]),
        _run("flaky", F, 0.0, 1.0, ["search_documents"]),  # one bad run of three
        _run("bad_args", F, 1.0, 0.5, ["run_sql"]),
        _run("no_scores", F, None, None, []),
    ]
    _write(tmp_path, "a", "s1", 10.0, runs)
    data = {r["case"]: r for r in card.rows(card.load_latest(tmp_path))}

    assert data["ok_case"]["pass"] and data["ok_case"]["runs"] == 2
    assert data["flaky"]["pass"] is False
    assert data["flaky"]["scores"]["tool_trajectory_avg_score"] == 0.0  # the worst of the two runs
    assert data["flaky"]["tools"] == "search_documents"  # the failed run, not the first
    assert data["bad_args"]["note"] == "tools right, arguments wrong"
    assert data["no_scores"]["note"].startswith("no scores")
    assert data["no_scores"]["tools"] == "(none)"


def test_only_the_newest_file_of_each_eval_set_is_used(tmp_path):
    _write(tmp_path, "old", "s1", 1.0, [_run("c", F, 0.0, 0.0, [])])
    _write(tmp_path, "new", "s1", 2.0, [_run("c", P, 1.0, 1.0, ["run_sql"])])
    _write(tmp_path, "other", "s2", 0.5, [_run("d", P, 1.0, 1.0, ["run_sql"])])
    results = card.load_latest(tmp_path)
    assert [r.eval_set_id for r in results] == ["s1", "s2"]
    assert results[0].eval_set_result_id == "new"


def test_table_and_main(tmp_path, capsys):
    _write(tmp_path, "a", "s1", 1.0, [_run("c", P, 1.0, 1.0, ["run_sql"])])
    text = card.table(card.rows(card.load_latest(tmp_path)))
    assert "trajectory" in text and "args_contain" in text and "PASS" in text and "run_sql" in text
    assert card.main([str(tmp_path)]) == 0
    assert "1 of 1 cases passed in every run." in capsys.readouterr().out


def test_main_without_results_explains_and_returns_1(tmp_path, capsys):
    assert card.main([str(tmp_path / "missing")]) == 1
    assert "No eval results" in capsys.readouterr().out
