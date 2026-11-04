"""Custom ADK metric: do the tool calls carry the expected argument fragments? (Sprint 10, Phase 10.3).

``tool_trajectory_avg_score`` (configured with ``ignore_args``) checks which tools were called and in what
order. This metric adds the check that the right values reached them, without comparing the whole SQL or
search text, which the model words differently from run to run. It is registered in each
``test_config.json`` under ``custom_metrics`` and is loaded by ADK from its dotted path, so it must stay a
plain function with the signature below.
"""

from google.adk.evaluation.eval_case import ConversationScenario, Invocation, get_all_tool_calls
from google.adk.evaluation.eval_metrics import EvalMetric
from google.adk.evaluation.evaluator import EvalStatus, EvaluationResult, PerInvocationResult
from google.genai import types

DEFAULT_THRESHOLD = 1.0


def _contains(expected_args: dict, actual_args: dict) -> bool:
    """Tells whether every expected value is a case-insensitive substring of the actual argument of the same name."""
    return all(
        key in actual_args and str(want).lower() in str(actual_args[key]).lower()
        for key, want in expected_args.items()
    )


def _invocation_score(expected: list[types.FunctionCall], actual: list[types.FunctionCall]) -> float:
    """Returns the share of golden calls that found a matching actual call.

    The golden calls are taken in order. Each one is matched with the first later actual call of the same
    name whose arguments contain all its fragments; a call of that name with other arguments (for example
    a first SQL attempt that was then corrected) is skipped. An empty golden list scores 1.0: there is
    nothing to check, and calls that should not happen are the trajectory metric's job.
    """
    if not expected:
        return 1.0
    matched, pos = 0, 0
    for want in expected:
        for i in range(pos, len(actual)):
            if actual[i].name == want.name and _contains(want.args or {}, actual[i].args or {}):
                matched += 1
                pos = i + 1
                break
    return matched / len(expected)


def tool_args_contain(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None = None,
) -> EvaluationResult:
    """Scores how many golden tool calls were made with arguments that contain the golden fragments.

    Args:
        eval_metric: The configured metric. ADK clears its ``threshold`` before calling a custom metric, so
            the threshold is read from ``criterion`` (1.0 when absent).
        actual_invocations: The agent's invocations from the eval run.
        expected_invocations: The golden invocations (``intermediate_data.tool_uses`` holds the fragments), or
            ``None`` when the case has no golden trajectory.
        conversation_scenario: Unused; part of ADK's custom-metric signature.

    Returns:
        An ``EvaluationResult`` with exactly one entry per actual invocation (ADK rejects any other count).
        An invocation without a golden counterpart is ``NOT_EVALUATED`` and left out of the overall mean.
        The overall status is ``PASSED`` when the mean reaches the threshold, ``FAILED`` otherwise, and
        ``NOT_EVALUATED`` when nothing could be scored.
    """
    threshold = getattr(eval_metric.criterion, "threshold", None)
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    results: list[PerInvocationResult] = []
    scores: list[float] = []
    for i, actual in enumerate(actual_invocations):
        expected = expected_invocations[i] if expected_invocations and i < len(expected_invocations) else None
        if expected is None:
            results.append(PerInvocationResult(actual_invocation=actual, eval_status=EvalStatus.NOT_EVALUATED))
            continue
        score = _invocation_score(
            get_all_tool_calls(expected.intermediate_data), get_all_tool_calls(actual.intermediate_data)
        )
        scores.append(score)
        results.append(
            PerInvocationResult(
                actual_invocation=actual,
                expected_invocation=expected,
                score=score,
                eval_status=EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED,
            )
        )
    if not scores:
        return EvaluationResult(per_invocation_results=results)
    overall = sum(scores) / len(scores)
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=EvalStatus.PASSED if overall >= threshold else EvalStatus.FAILED,
        per_invocation_results=results,
    )
