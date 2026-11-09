"""Writes synthetic ADK result files, so the runner and report can be exercised without a model (Sprint 11, 11.4).

The files have the format ``AgentEvaluator`` saves (``EvalSetResult``), built by hand from a compact spec. Used
by the offline tests and ``dev_test/phase_11_4.py``; never by a live run.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_metrics import EvalMetricResult, EvalMetricResultPerInvocation
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult
from google.adk.evaluation.evaluator import EvalStatus
from google.genai import types

from evals.adk.stats import GATING

HISTORY_SUBDIR = Path("data_agent") / ".adk" / "eval_history"


def case_run(
    eval_id: str,
    outcome: str = "pass",
    tools: tuple[str, ...] = ("run_sql",),
    failing: tuple[str, ...] = ("tool_trajectory_avg_score",),
    note: str = "",
) -> EvalCaseResult:
    """One run of one case.

    Args:
        eval_id: The eval case id.
        outcome: ``pass``; ``fail`` (the metrics in ``failing`` fail); ``invalid`` (no metric produced a
            score, as for an API error); or ``degraded`` (all metrics pass but a search response carries
            the BM25-fallback note).
        tools: Tool names the agent called, as one turn.
        failing: Metrics that fail when ``outcome == "fail"``.
        note: Extra text appended to a search response (used for ``degraded``).
    """
    status = {"pass": EvalStatus.PASSED, "degraded": EvalStatus.PASSED, "fail": EvalStatus.FAILED}.get(
        outcome, EvalStatus.NOT_EVALUATED
    )
    results = []
    if outcome != "invalid":
        for name in GATING[:2]:
            bad = outcome == "fail" and name in failing
            results.append(EvalMetricResult(
                metric_name=name, threshold=1.0, score=0.0 if bad else 1.0,
                eval_status=EvalStatus.FAILED if bad else EvalStatus.PASSED,
            ))
        for name in GATING[2:]:
            bad = outcome == "fail" and name in failing
            results.append(EvalMetricResult(
                metric_name=name, threshold=1.0, score=0.0 if bad else None,
                eval_status=EvalStatus.FAILED if bad else EvalStatus.NOT_EVALUATED,
            ))
    calls = [types.FunctionCall(id=str(i), name=t, args={}) for i, t in enumerate(tools)]
    responses = [
        types.FunctionResponse(
            id=str(i), name=t,
            response={"status": "success", "notes": [note or "ok"]} if t == "search_documents" else {"status": "success"},
        )
        for i, t in enumerate(tools)
    ]
    inv = Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text="q")]),
        final_response=types.Content(role="model", parts=[types.Part(text="answer")]),
        intermediate_data=IntermediateData(tool_uses=calls, tool_responses=responses),
    )
    return EvalCaseResult(
        eval_set_id="synthetic", eval_id=eval_id, final_eval_status=status, overall_eval_metric_results=results,
        eval_metric_result_per_invocation=[] if outcome == "invalid" else [EvalMetricResultPerInvocation(actual_invocation=inv)],
        session_id="s",
    )


def write_results(history_root: Path, name: str, runs: list[EvalCaseResult], set_id: str = "synthetic") -> Path:
    """Saves runs as ``<history_root>/data_agent/.adk/eval_history/<name>.evalset_result.json`` and returns the folder."""
    folder = Path(history_root) / HISTORY_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    result = EvalSetResult(
        eval_set_result_id=name, eval_set_result_name=name, eval_set_id=set_id, eval_case_results=runs, creation_timestamp=1.0
    )
    (folder / f"{name}.evalset_result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return folder


def runs_for(eval_id: str, outcomes: list[str], **kwargs) -> list[EvalCaseResult]:
    """Several runs of one case from a list of outcomes, e.g. ``["pass", "pass", "fail"]``."""
    return [case_run(eval_id, o, **kwargs) for o in outcomes]
