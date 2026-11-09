"""Offline tests of the ``tool_args_contain`` ADK custom metric (Sprint 10, Phase 10.3).

Invocations are built by hand, and one test runs the metric through ADK's own custom-metric evaluator,
built from the real ``test_config.json``, so the registration and signature are checked as ADK uses them.
"""

import asyncio
from pathlib import Path

import pytest
from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
from google.adk.evaluation.eval_metrics import BaseCriterion, EvalMetric
from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
from google.adk.evaluation.evaluator import EvalStatus
from google.genai import types

from evals.adk.metrics import tool_args_contain

ADK_DIR = Path(__file__).resolve().parent.parent / "evals" / "adk"


def inv(*calls: tuple[str, dict]) -> Invocation:
    """An invocation whose tool calls are ``(name, args)`` pairs."""
    return Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text="q")]),
        intermediate_data=IntermediateData(tool_uses=[types.FunctionCall(name=n, args=a) for n, a in calls]),
    )


def metric(threshold: float = 1.0) -> EvalMetric:
    return EvalMetric(
        metric_name="tool_args_contain", threshold=threshold, criterion=BaseCriterion(threshold=threshold)
    )


def run(actual, expected, threshold: float = 1.0):
    return tool_args_contain(metric(threshold), actual, expected)


def test_matching_fragment_passes_case_insensitively():
    result = run(
        [inv(("run_sql", {"query": 'SELECT * FROM "data" WHERE "shipment_id" = 12345'}))],
        [inv(("run_sql", {"query": "SHIPMENT_ID"}))],
    )
    assert result.overall_score == 1.0
    assert result.overall_eval_status is EvalStatus.PASSED
    assert result.per_invocation_results[0].eval_status is EvalStatus.PASSED


def test_missing_fragment_fails():
    result = run([inv(("run_sql", {"query": "SELECT 1"}))], [inv(("run_sql", {"query": "12345"}))])
    assert result.overall_score == 0.0
    assert result.overall_eval_status is EvalStatus.FAILED


def test_missing_argument_name_fails():
    result = run([inv(("search_documents", {"q": "express"}))], [inv(("search_documents", {"query": "express"}))])
    assert result.overall_score == 0.0


def test_missing_call_fails_and_partial_score_is_a_share():
    actual = [inv(("run_sql", {"query": "12345"}))]
    expected = [inv(("run_sql", {"query": "12345"}), ("search_documents", {"query": "express"}))]
    result = run(actual, expected)
    assert result.overall_score == 0.5
    assert result.overall_eval_status is EvalStatus.FAILED


def test_extra_calls_are_allowed_and_a_corrected_retry_matches():
    actual = [
        inv(
            ("search_documents", {"query": "anything"}),
            ("run_sql", {"query": "SELECT bad"}),
            ("run_sql", {"query": "SELECT ... 12345"}),
            ("search_documents", {"query": "express delay"}),
        )
    ]
    expected = [inv(("run_sql", {"query": "12345"}), ("search_documents", {"query": "express"}))]
    assert run(actual, expected).overall_score == 1.0


def test_golden_order_is_kept():
    actual = [inv(("search_documents", {"query": "express"}), ("run_sql", {"query": "12345"}))]
    expected = [inv(("run_sql", {"query": "12345"}), ("search_documents", {"query": "express"}))]
    assert run(actual, expected).overall_score == 0.5  # the search came before the lookup, so it cannot follow it


def test_empty_golden_list_scores_one_whatever_was_called():
    result = run([inv(("run_sql", {"query": "DELETE"}))], [inv()])
    assert result.overall_score == 1.0
    assert result.overall_eval_status is EvalStatus.PASSED


def test_no_golden_invocation_is_not_evaluated_but_still_one_result_per_invocation():
    result = run([inv(("run_sql", {"query": "x"}))], None)
    assert result.overall_score is None
    assert result.overall_eval_status is EvalStatus.NOT_EVALUATED
    assert len(result.per_invocation_results) == 1


def test_threshold_comes_from_the_criterion():
    actual = [inv(("run_sql", {"query": "12345"}))]
    expected = [inv(("run_sql", {"query": "12345"}), ("search_documents", {"query": "express"}))]
    assert run(actual, expected, threshold=0.5).overall_eval_status is EvalStatus.PASSED
    assert run(actual, expected, threshold=1.0).overall_eval_status is EvalStatus.FAILED


@pytest.mark.parametrize("folder", ["unit_routing", "unit_hybrid"])
def test_runs_through_adk_custom_metric_evaluator(folder):
    """The registered dotted path resolves, and ADK clearing the metric's threshold does not break the verdict."""
    config = EvalConfig.model_validate_json((ADK_DIR / folder / "test_config.json").read_text(encoding="utf-8"))
    ours = next(m for m in get_eval_metrics_from_config(config) if m.metric_name == "tool_args_contain")
    assert ours.custom_function_path == "evals.adk.metrics.tool_args_contain"
    evaluator = _CustomMetricEvaluator(ours, ours.custom_function_path)
    good = asyncio.run(
        evaluator.evaluate_invocations(
            [inv(("run_sql", {"query": "12345"}))], [inv(("run_sql", {"query": "12345"}))]
        )
    )
    bad = asyncio.run(
        evaluator.evaluate_invocations([inv(("run_sql", {"query": "1"}))], [inv(("run_sql", {"query": "12345"}))])
    )
    assert good.overall_eval_status is EvalStatus.PASSED
    assert bad.overall_eval_status is EvalStatus.FAILED
