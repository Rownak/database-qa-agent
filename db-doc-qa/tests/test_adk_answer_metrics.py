"""Offline tests of the answer and citation ADK metrics (Sprint 11, Phase 11.2).

Invocations are built by hand. The metrics are also run through ADK's custom-metric evaluator built from the
real ``test_config.json`` files, and compared with the harness functions in ``evals/metrics.py``.
"""

import asyncio
from pathlib import Path

import pytest
from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
from google.adk.evaluation.eval_metrics import BaseCriterion, EvalMetric
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent
from google.adk.evaluation.eval_set import EvalSet
from google.adk.evaluation.evaluator import EvalStatus
from google.adk.evaluation.local_eval_service import LocalEvalService
from google.genai import types

from evals import metrics as harness
from evals.adk import build
from evals.adk import metrics as adk
from evals.adk.metrics import ANSWER_CONTAINS, ANSWER_EXCLUDES, EXPECTED_SECTION

ADK_DIR = Path(__file__).resolve().parent.parent / "evals" / "adk"
PASSAGE = {"source": "shipment_delay_policy.pdf", "section_number": "3.2", "text": "Express: 2 days."}
CITED = "Escalate at 2 days [shipment_delay_policy.pdf, §3.2 Express Shipments, p. 2]."


def actual(answer: str, *passages: dict) -> Invocation:
    """An agent invocation: the answer and, when passages are given, one search_documents call returning them."""
    calls, resps = [], []
    if passages:
        calls = [types.FunctionCall(id="c1", name="search_documents", args={"query": "express"})]
        resps = [
            types.FunctionResponse(
                id="c1", name="search_documents", response={"status": "success", "passages": list(passages)}
            )
        ]
    return Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text="q")]),
        final_response=types.Content(role="model", parts=[types.Part(text=answer)]),
        intermediate_data=IntermediateData(tool_uses=calls, tool_responses=resps),
    )


def golden(**by_kind) -> Invocation:
    """A golden invocation carrying rubrics, e.g. ``golden(ANSWER_CONTAINS=["x"])``."""
    rubrics = [
        Rubric(rubric_id=f"{kind}_{i}", rubric_content=RubricContent(text_property=v), type=kind)
        for kind, values in by_kind.items()
        for i, v in enumerate(values, 1)
    ]
    return Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text="q")]),
        final_response=types.Content(role="model", parts=[types.Part(text="")]),
        intermediate_data=IntermediateData(),
        rubrics=rubrics,
    )


def run(fn, act, exp, threshold: float = 1.0):
    m = EvalMetric(metric_name=fn.__name__, threshold=threshold, criterion=BaseCriterion(threshold=threshold))
    return fn(m, act, exp)


# --- pass and fail ---

def test_answer_contains_pass_fail_and_partial():
    exp = [golden(ANSWER_CONTAINS=["Cleveland", "2 days"])]
    ok = run(adk.answer_contains, [actual("It is in  CLEVELAND; 2 days")], exp)
    assert ok.overall_score == 1.0 and ok.overall_eval_status is EvalStatus.PASSED
    half = run(adk.answer_contains, [actual("Cleveland")], exp)
    assert half.overall_score == 0.5 and half.overall_eval_status is EvalStatus.FAILED
    assert run(adk.answer_contains, [actual("Cleveland")], exp, threshold=0.5).overall_eval_status is EvalStatus.PASSED


def test_answer_excludes_pass_and_fail():
    exp = [golden(ANSWER_EXCLUDES=["Cleveland", "dropped"])]
    assert run(adk.answer_excludes, [actual("It is in Vancouver.")], exp).overall_eval_status is EvalStatus.PASSED
    bad = run(adk.answer_excludes, [actual("Table DROPPED.")], exp)
    assert bad.overall_score == 0.5 and bad.overall_eval_status is EvalStatus.FAILED


def test_section_retrieved_pass_and_fail():
    exp = [golden(EXPECTED_SECTION=["shipment_delay_policy.pdf|3.2"])]
    assert run(adk.section_retrieved, [actual("a", PASSAGE)], exp).overall_eval_status is EvalStatus.PASSED
    other = {**PASSAGE, "section_number": "4.1"}
    assert run(adk.section_retrieved, [actual("a", other)], exp).overall_eval_status is EvalStatus.FAILED
    assert run(adk.section_retrieved, [actual("a")], exp).overall_score == 0.0  # no search at all


def test_citation_grounded_pass_and_fail():
    assert run(adk.citation_grounded, [actual(CITED, PASSAGE)], None).overall_eval_status is EvalStatus.PASSED
    bad = run(adk.citation_grounded, [actual("See [shipment_delay_policy.pdf, §9.9 Fake]", PASSAGE)], None)
    assert bad.overall_score == 0.0 and bad.overall_eval_status is EvalStatus.FAILED
    assert run(adk.citation_grounded, [actual(CITED)], None).overall_eval_status is EvalStatus.FAILED  # cited, never searched


# --- NOT_EVALUATED ---

@pytest.mark.parametrize("fn", [adk.answer_contains, adk.answer_excludes, adk.section_retrieved])
def test_invocation_without_the_rubric_is_not_evaluated(fn):
    result = run(fn, [actual(CITED, PASSAGE)], [golden()])
    assert result.overall_eval_status is EvalStatus.NOT_EVALUATED
    assert [r.eval_status for r in result.per_invocation_results] == [EvalStatus.NOT_EVALUATED]
    assert run(fn, [actual("x")], None).overall_eval_status is EvalStatus.NOT_EVALUATED


def test_citation_grounded_is_not_evaluated_without_a_citation():
    assert run(adk.citation_grounded, [actual("Cleveland.", PASSAGE)], None).overall_eval_status is EvalStatus.NOT_EVALUATED


def test_multi_turn_scores_only_the_turns_with_a_rubric():
    exp = [golden(), golden(ANSWER_CONTAINS=["Tier 2"])]
    result = run(adk.answer_contains, [actual("anything"), actual("Tier 2 applies")], exp)
    assert [r.eval_status for r in result.per_invocation_results] == [EvalStatus.NOT_EVALUATED, EvalStatus.PASSED]
    assert result.overall_eval_status is EvalStatus.PASSED
    assert run(adk.answer_contains, [actual("anything"), actual("nothing")], exp).overall_eval_status is EvalStatus.FAILED


def test_adk_final_status_ignores_not_evaluated():
    """ADK combines the metric statuses of a case: NOT_EVALUATED neither fails nor passes it."""

    def result(status):
        return type("R", (), {"eval_status": status})()

    final = LocalEvalService._generate_final_eval_status
    ne, ok, bad = EvalStatus.NOT_EVALUATED, EvalStatus.PASSED, EvalStatus.FAILED
    assert final(None, [result(ne), result(ok)]) is ok
    assert final(None, [result(ne), result(bad)]) is bad
    assert final(None, [result(ne), result(ne)]) is ne


# --- adapter and harness parity ---

def test_adapter_and_harness_function_give_the_same_score():
    answer = f"Cleveland Sort Facility. {CITED}\nSources: none"
    turn = adk.to_turn_result(actual(answer, PASSAGE))
    assert turn.answer == answer
    assert turn.calls == [
        {"name": "search_documents", "args": {"query": "express"}, "response": {"status": "success", "passages": [PASSAGE]}}
    ]
    exp = golden(ANSWER_CONTAINS=["cleveland", "missing"], EXPECTED_SECTION=["shipment_delay_policy.pdf|3.2", "x.pdf|1"])
    case = adk.to_eval_case(exp)
    for fn, harness_fn in [(adk.answer_contains, harness.answer_contains), (adk.section_retrieved, harness.section_recall)]:
        assert run(fn, [actual(answer, PASSAGE)], [exp]).overall_score == harness_fn(case, turn) == 0.5
    assert run(adk.citation_grounded, [actual(answer, PASSAGE)], None).overall_score == harness.citation_precision(case, turn) == 1.0


def test_adapter_drops_thought_parts_and_keeps_a_call_without_response():
    inv = actual("")
    inv.final_response = types.Content(role="model", parts=[types.Part(text="hidden", thought=True), types.Part(text="shown")])
    inv.intermediate_data = IntermediateData(tool_uses=[types.FunctionCall(id="c9", name="run_sql", args={"query": "1"})])
    turn = adk.to_turn_result(inv)
    assert turn.answer == "shown"
    assert turn.calls == [{"name": "run_sql", "args": {"query": "1"}, "response": None}]


# --- the real config, through ADK ---

@pytest.mark.parametrize("folder", ["unit_routing", "unit_hybrid"])
def test_every_metric_runs_through_adk_custom_metric_evaluator(folder):
    config = EvalConfig.model_validate_json((ADK_DIR / folder / "test_config.json").read_text(encoding="utf-8"))
    by_name = {m.metric_name: m for m in get_eval_metrics_from_config(config)}
    assert set(build.GATING_METRICS) <= set(by_name)
    exp = [golden(ANSWER_CONTAINS=["Tier 2"], ANSWER_EXCLUDES=["Cleveland"], EXPECTED_SECTION=["shipment_delay_policy.pdf|3.2"])]
    good = [actual(f"Tier 2. {CITED}", PASSAGE)]
    bad = [actual("Cleveland. See [shipment_delay_policy.pdf, §9.9 Fake]", {**PASSAGE, "section_number": "4.1"})]
    for name in ("answer_contains", "answer_excludes", "section_retrieved", "citation_grounded"):
        m = by_name[name]
        assert m.custom_function_path == f"evals.adk.metrics.{name}"
        evaluator = _CustomMetricEvaluator(m, m.custom_function_path)
        assert asyncio.run(evaluator.evaluate_invocations(good, exp)).overall_eval_status is EvalStatus.PASSED, name
        assert asyncio.run(evaluator.evaluate_invocations(bad, exp)).overall_eval_status is EvalStatus.FAILED, name


def test_builder_rubrics_are_read_by_the_metrics():
    """A generated eval set's golden invocations feed the metric: the contains_sql value 9 is required."""
    files = build.build().files
    path = next(p for p in files if p.name == "unit_routing.test.json")
    case = next(c for c in EvalSet.model_validate_json(files[path]).eval_cases if c.eval_id == "unit_db_count_by_status")
    assert run(adk.answer_contains, [actual("There are 9 delayed shipments.")], case.conversation).overall_eval_status is EvalStatus.PASSED
    assert run(adk.answer_contains, [actual("There are 4.")], case.conversation).overall_eval_status is EvalStatus.FAILED
