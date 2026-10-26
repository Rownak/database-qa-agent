"""Tests for evals.metrics: each metric on a passing and a failing TurnResult, and on a case with nothing to compare."""

from data_agent.events import TurnResult
from evals.dataset import EvalCase, Expected, ExpectedSection
from evals.metrics import METRICS

POLICY = "shipment_delay_policy.pdf"


def case(**expected):
    return EvalCase(id="c", question="q", expected=Expected(**expected))


def passage(source=POLICY, number="3.2"):
    return {"source": source, "section_number": number, "text": "t"}


def search(*passages):
    return {"name": "search_documents", "args": {}, "response": {"status": "success", "passages": list(passages)}}


def sql():
    return {"name": "run_sql", "args": {}, "response": {"status": "success"}}


def result(answer="", calls=()):
    return TurnResult(answer, list(calls), None, 0.1)


def test_metrics_registry():
    assert set(METRICS) == {"tool_sequence_match", "section_recall", "answer_contains", "citation_precision", "mrr"}


def test_tool_sequence_match():
    c = case(tools=["run_sql", "search_documents"])
    f = METRICS["tool_sequence_match"]
    assert f(c, result(calls=[sql(), search(), search()])) == 1.0  # distinct names, first-call order
    assert f(c, result(calls=[search(), sql()])) == 0.0  # wrong order
    assert f(c, result(calls=[sql()])) == 0.0
    assert f(case(tools=[]), result()) == 1.0 and f(case(tools=[]), result(calls=[sql()])) == 0.0


def test_section_recall():
    c = case(sections=[ExpectedSection(source=POLICY, section="3.2"), ExpectedSection(source="other.txt", section="4.2")])
    f = METRICS["section_recall"]
    assert f(c, result(calls=[search(passage(), passage("other.txt", "4.2"))])) == 1.0
    assert f(c, result(calls=[search(passage())])) == 0.5
    assert f(c, result(calls=[search(passage(number="3.3"))])) == 0.0  # right document, wrong section
    assert f(c, result(calls=[search(passage("other.txt", "3.2"))])) == 0.0  # right number, wrong document
    assert f(c, result(calls=[sql()])) == 0.0


def test_section_recall_accepts_a_split_section():
    c = case(sections=[ExpectedSection(source=POLICY, section="3.2")])
    assert METRICS["section_recall"](c, result(calls=[search(passage(number="3.2(a)-(c)"))])) == 1.0
    c33 = case(sections=[ExpectedSection(source=POLICY, section="3.3")])
    assert METRICS["section_recall"](c33, result(calls=[search(passage(number="3.30"))])) == 0.0


def test_answer_contains():
    c = case(answer_contains=["Tier 2", "2 days"])
    f = METRICS["answer_contains"]
    assert f(c, result("Open a  tier 2\ncase after 2 days.")) == 1.0  # case and whitespace are ignored
    assert f(c, result("Open a Tier 2 case.")) == 0.5
    assert f(c, result("Nothing.")) == 0.0


def test_citation_precision():
    f = METRICS["citation_precision"]
    c = case()
    answer = f"Rule [{POLICY}, §3.2 Express Shipments, p. 2]. Other [other.txt, §4.2 Escalation threshold]."
    assert f(c, result(answer, [search(passage(), passage("other.txt", "4.2"))])) == 1.0
    assert f(c, result(answer, [search(passage())])) == 0.5  # the second citation was never retrieved
    assert f(c, result(answer, [sql()])) == 0.0
    assert f(c, result("No citations here.", [search(passage())])) is None


def test_every_metric_is_none_when_expected_is_empty():
    empty, r = case(), result("answer", [sql()])
    for name in METRICS:
        assert METRICS[name](empty, r) is None, name
