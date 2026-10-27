"""Tests for evals.metrics: each metric on a passing and a failing TurnResult, and on a case with nothing to compare."""

import importlib.util
from pathlib import Path

import pytest

from data_agent.events import TurnResult
from evals.dataset import EvalCase, Expected, ExpectedSection
from evals.metrics import METRICS, final_answer, predicted_object_ids

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
    assert set(METRICS) == {
        "tool_sequence_match", "section_recall", "answer_contains", "citation_precision", "mrr",
        "answer_f1", "object_f1", "coverage", "exact_match", "squad_f1",
    }


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
        if name != "coverage":  # coverage needs no expectation: it only checks that an answer exists
            assert METRICS[name](empty, r) is None, name


def sql_call(query, status="success"):
    return {"name": "run_sql", "args": {"query": query}, "response": {"status": status}}


def test_final_answer_removes_the_sources_line():
    assert final_answer(result("Yes.\nSources: SELECT 1; chunk_id 3")) == "Yes."
    assert final_answer(result("Yes. [a.md, §1 T]")) == "Yes. [a.md, §1 T]"
    assert final_answer(result("")) == ""


def test_predicted_object_ids_reads_cited_sources_and_sql_tables():
    calls = [
        sql_call('SELECT * FROM "Client" c JOIN "transaction" t ON t.client_id = c.client_id'),
        sql_call("SELECT * FROM cheque", status="error"),  # a failed query names no table
        search(passage("Policy-A.md", "5"), passage("policy-b.md", "2")),
    ]
    r = result("Yes. [Policy-A.md, §5 Safeguards]", calls)
    assert predicted_object_ids(r) == ["client", "policy-a", "transaction"]
    # nothing cited: every returned passage counts
    assert predicted_object_ids(result("Yes.", calls)) == ["client", "policy-a", "policy-b", "transaction"]


def test_answer_f1():
    c = case(reference_answers=["Yes, approved", "The request is approved for the client"])
    f = METRICS["answer_f1"]
    assert f(c, result("Yes, approved")) == 1.0
    assert f(c, result("Declined")) == 0.0
    assert 0 < f(c, result("Yes, approved today.\nSources: none")) < 1  # the Sources line is not scored
    assert f(case(), result("Yes")) is None


def test_object_f1():
    c = case(object_ids=["policy-a", "client"])
    f = METRICS["object_f1"]
    assert f(c, result("ok [policy-a.md, §1 T]", [search(passage("policy-a.md", "1")), sql_call("SELECT * FROM client")])) == 1.0
    assert f(c, result("ok", [sql_call("SELECT * FROM cheque")])) == 0.0
    assert f(c, result("ok [policy-a.md, §1 T]", [search(passage("policy-a.md", "1"))])) == 2 / 3  # precision 1, recall 0.5
    assert f(case(), result("ok")) is None


def test_coverage():
    f = METRICS["coverage"]
    assert f(case(), result("An answer")) == 1.0
    assert f(case(), result("  \nSources: none")) == 0.0
    assert f(case(), result("")) == 0.0


SCORE_PY = Path(__file__).resolve().parents[2] / "benchmark" / "LakeQuest" / "scoring_program" / "score.py"


@pytest.mark.skipif(not SCORE_PY.is_file(), reason="benchmark/LakeQuest/scoring_program/score.py is missing")
def test_answer_f1_matches_the_official_scorer():
    spec = importlib.util.spec_from_file_location("lakequest_score", SCORE_PY)
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    pairs = [
        ("Yes, the transfer is allowed.", "Yes, the transfer is allowed"),
        ("42", "The balance is 42 USD."),
        ("Approve the request after identity checks", "Decline"),
        ("a a b", "a b b c"),
        ("Mark Johnson (client_id: 748)", ""),
    ]
    for ref, pred in pairs:
        c = case(reference_answers=[ref])
        assert METRICS["answer_f1"](c, result(pred)) == pytest.approx(official._token_f1(ref, pred))


# ---- short-answer metrics (HybridQA-style exact match and SQuAD F1) ----

import collections  # noqa: E402
import re  # noqa: E402
import string  # noqa: E402

from evals.metrics import short_answer  # noqa: E402


def refs(*answers):
    return case(reference_answers=list(answers))


def test_exact_match_ignores_case_punctuation_and_articles():
    f = METRICS["exact_match"]
    assert f(refs("beatles"), result("The Beatles.")) == 1.0
    assert f(refs("The Beatles"), result("a BEATLES!")) == 1.0
    assert f(refs("Paris"), result("Paris, France")) == 0.0
    assert f(refs("Paris", "Lyon"), result("lyon")) == 1.0  # best over the references


def test_squad_f1_is_partial_credit_on_tokens():
    f = METRICS["squad_f1"]
    assert f(refs("New York City"), result("New York")) == pytest.approx(2 * 1.0 * (2 / 3) / (1.0 + 2 / 3))
    assert f(refs("Paris"), result("Berlin")) == 0.0
    assert f(refs("Paris", "New York"), result("York")) == pytest.approx(2 * 1.0 * 0.5 / 1.5)


def test_empty_texts_follow_the_squad_scorer():
    assert METRICS["squad_f1"](refs("the"), result("a")) == 1.0  # both normalise to nothing
    assert METRICS["squad_f1"](refs("Paris"), result("")) == 0.0
    assert METRICS["exact_match"](refs("Paris"), result("")) == 0.0


def test_short_answer_scores_only_the_first_line():
    r = result("\nParis\nThe capital, see [France.md].\nSources: x")
    assert short_answer(r) == "Paris"
    assert METRICS["exact_match"](refs("Paris"), r) == 1.0
    assert METRICS["squad_f1"](refs("Paris"), r) == 1.0
    assert short_answer(result("")) == ""


def test_short_answer_metrics_are_none_without_references():
    assert METRICS["exact_match"](case(), result("x")) is None
    assert METRICS["squad_f1"](case(), result("x")) is None


# copied from benchmark/HybridQA/evaluate_script.py (MIT) to pin our values to the official formulas
def _official_normalize(s):
    s = "".join(ch for ch in s.lower() if ch not in set(string.punctuation))
    return " ".join(re.sub(re.compile(r"\b(a|an|the)\b", re.UNICODE), " ", s).split())


def _official_exact(a_gold, a_pred):
    return int(_official_normalize(a_gold) == _official_normalize(a_pred))


def _official_f1(a_gold, a_pred):
    gold, pred = _official_normalize(a_gold).split(), _official_normalize(a_pred).split()
    num_same = sum((collections.Counter(gold) & collections.Counter(pred)).values())
    if len(gold) == 0 or len(pred) == 0:
        return int(gold == pred)
    if num_same == 0:
        return 0
    precision, recall = 1.0 * num_same / len(pred), 1.0 * num_same / len(gold)
    return (2 * precision * recall) / (precision + recall)


@pytest.mark.parametrize(
    "gold, pred",
    [
        ("The United States", "United States of America"),
        ("1999", "In 1999."),
        ("Mount Everest", "mount everest"),
        ("a an the", "the"),
        ("Saint-Denis, France", "Saint Denis"),
    ],
)
def test_values_equal_the_official_hybridqa_formulas(gold, pred):
    c, r = refs(gold), result(pred)
    assert METRICS["exact_match"](c, r) == _official_exact(gold, pred)
    assert METRICS["squad_f1"](c, r) == pytest.approx(_official_f1(gold, pred))
