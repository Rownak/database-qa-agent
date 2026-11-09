"""Custom ADK metric: do the tool calls carry the expected argument fragments? (Sprint 10, Phase 10.3).

``tool_trajectory_avg_score`` (configured with ``ignore_args``) checks which tools were called and in what
order. This metric adds the check that the right values reached them, without comparing the whole SQL or
search text, which the model words differently from run to run. It is registered in each
``test_config.json`` under ``custom_metrics`` and is loaded by ADK from its dotted path, so it must stay a
plain function with the signature below.

Sprint 11, Phase 11.2 adds the answer and citation metrics (design section 6): ``answer_contains``,
``answer_excludes``, ``section_retrieved`` and ``citation_grounded``. They read the typed rubrics the builder
writes on the golden invocations and score the agent's invocation with the same pure functions as the harness
(``evals/metrics.py``), through a small adapter that turns an ADK ``Invocation`` into a ``TurnResult``.
"""

from collections.abc import Callable

from google.adk.evaluation.eval_case import (
    ConversationScenario,
    Invocation,
    get_all_tool_calls,
    get_all_tool_calls_with_responses,
)
from google.adk.evaluation.eval_metrics import EvalMetric
from google.adk.evaluation.evaluator import EvalStatus, EvaluationResult, PerInvocationResult
from google.genai import types

from data_agent.events import TurnResult
from evals import metrics as harness
from evals.dataset import EvalCase, Expected, ExpectedSection

DEFAULT_THRESHOLD = 1.0

# Rubric types written by evals/adk/build.py; the value is in ``rubric_content.text_property``.
ANSWER_CONTAINS = "ANSWER_CONTAINS"
ANSWER_EXCLUDES = "ANSWER_EXCLUDES"
EXPECTED_SECTION = "EXPECTED_SECTION"  # value: "<source>|<section number>"


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


# --- answer and citation metrics (Sprint 11, Phase 11.2) ---


def _rubric_values(invocation: Invocation | None, kind: str) -> list[str]:
    """Returns the ``text_property`` of every rubric of this type on an invocation."""
    return [
        r.rubric_content.text_property
        for r in (invocation.rubrics if invocation and invocation.rubrics else [])
        if r.type == kind and r.rubric_content.text_property
    ]


def to_turn_result(invocation: Invocation) -> TurnResult:
    """Turns an ADK invocation into the ``TurnResult`` the harness metrics score.

    Args:
        invocation: The agent invocation: final response plus tool calls and their responses.

    Returns:
        The answer text (thought parts left out) and the calls as ``{name, args, response}``; ``response``
        is ``None`` for a call whose response is missing.
    """
    parts = invocation.final_response.parts if invocation.final_response else None
    answer = "".join(p.text or "" for p in parts or [] if p.text and not p.thought)
    calls = [
        {"name": call.name, "args": dict(call.args or {}), "response": dict(resp.response or {}) if resp else None}
        for call, resp in get_all_tool_calls_with_responses(invocation.intermediate_data)
    ]
    return TurnResult(answer=answer, calls=calls)


def to_eval_case(expected: Invocation | None) -> EvalCase:
    """Turns the golden invocation rubrics into the ``EvalCase`` expectations of the harness functions.

    ``ANSWER_CONTAINS`` becomes ``answer_contains`` and ``EXPECTED_SECTION`` (``source|section``) becomes
    ``sections``. ``ANSWER_EXCLUDES`` has no harness counterpart and is read by :func:`answer_excludes`.
    """
    sections = []
    for value in _rubric_values(expected, EXPECTED_SECTION):
        source, _, section = value.partition("|")
        sections.append(ExpectedSection(source=source, section=section))
    return EvalCase(
        id=expected.invocation_id if expected else "",
        question="",
        expected=Expected(answer_contains=_rubric_values(expected, ANSWER_CONTAINS), sections=sections),
    )


def _evaluate(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    score: Callable[[Invocation, Invocation | None], float | None],
) -> EvaluationResult:
    """Scores each invocation with ``score`` and builds ADK's result.

    ``score`` returns ``None`` when the invocation has nothing to check; that invocation is ``NOT_EVALUATED``
    and left out of the overall mean. The threshold is read from ``criterion`` because ADK clears
    ``eval_metric.threshold`` before calling a custom metric. One result per actual invocation is returned,
    as ADK requires.
    """
    threshold = getattr(eval_metric.criterion, "threshold", None)
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    results: list[PerInvocationResult] = []
    scores: list[float] = []
    for i, actual in enumerate(actual_invocations):
        expected = expected_invocations[i] if expected_invocations and i < len(expected_invocations) else None
        value = score(actual, expected)
        if value is None:
            results.append(
                PerInvocationResult(
                    actual_invocation=actual, expected_invocation=expected, eval_status=EvalStatus.NOT_EVALUATED
                )
            )
            continue
        scores.append(value)
        results.append(
            PerInvocationResult(
                actual_invocation=actual,
                expected_invocation=expected,
                score=value,
                eval_status=EvalStatus.PASSED if value >= threshold else EvalStatus.FAILED,
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


def answer_contains(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None = None,
) -> EvaluationResult:
    """Scores the share of ``ANSWER_CONTAINS`` fragments found in the final answer.

    Uses ``evals.metrics.answer_contains`` (case-insensitive, whitespace-normalised). An invocation whose
    golden counterpart has no such rubric is ``NOT_EVALUATED``.

    Args:
        eval_metric: The configured metric; the threshold is read from its ``criterion``.
        actual_invocations: The agent invocations.
        expected_invocations: The golden invocations carrying the rubrics.
        conversation_scenario: Unused; part of ADK's custom-metric signature.

    Returns:
        One result per actual invocation.
    """

    def score(actual: Invocation, expected: Invocation | None) -> float | None:
        case = to_eval_case(expected)
        return harness.answer_contains(case, to_turn_result(actual)) if case.expected.answer_contains else None

    return _evaluate(eval_metric, actual_invocations, expected_invocations, score)


def answer_excludes(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None = None,
) -> EvaluationResult:
    """Scores the share of ``ANSWER_EXCLUDES`` fragments that are absent from the final answer.

    The same normalisation as ``answer_contains``. An invocation without such a rubric is ``NOT_EVALUATED``.
    Arguments and return value as in :func:`answer_contains`.
    """

    def score(actual: Invocation, expected: Invocation | None) -> float | None:
        forbidden = _rubric_values(expected, ANSWER_EXCLUDES)
        if not forbidden:
            return None
        answer = harness._normalise(to_turn_result(actual).answer)
        return sum(harness._normalise(f) not in answer for f in forbidden) / len(forbidden)

    return _evaluate(eval_metric, actual_invocations, expected_invocations, score)


def section_retrieved(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None = None,
) -> EvaluationResult:
    """Scores the share of ``EXPECTED_SECTION`` sections among the passages this turn's searches returned.

    Uses ``evals.metrics.section_recall``. An invocation without such a rubric is ``NOT_EVALUATED``.
    Arguments and return value as in :func:`answer_contains`.
    """

    def score(actual: Invocation, expected: Invocation | None) -> float | None:
        case = to_eval_case(expected)
        return harness.section_recall(case, to_turn_result(actual)) if case.expected.sections else None

    return _evaluate(eval_metric, actual_invocations, expected_invocations, score)


def citation_grounded(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None = None,
) -> EvaluationResult:
    """Scores the share of ``[source, §n]`` citations in the answer that match a passage returned this turn.

    Uses ``evals.metrics.citation_precision``. It needs no rubric, since every answer may cite only what it
    retrieved; an invocation whose answer cites nothing is ``NOT_EVALUATED``. Arguments and return value as
    in :func:`answer_contains`.
    """

    def score(actual: Invocation, expected: Invocation | None) -> float | None:
        return harness.citation_precision(EvalCase(id="", question=""), to_turn_result(actual))

    return _evaluate(eval_metric, actual_invocations, expected_invocations, score)
