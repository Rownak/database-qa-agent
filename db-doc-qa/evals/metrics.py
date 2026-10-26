"""Pure scoring functions over ``(case, TurnResult)`` (design section 5.3).

Each metric returns a float between 0 and 1, or ``None`` when the case has nothing to
compare (for example no expected sections). :data:`METRICS` maps the metric name used in
the YAML config to its function, so a new metric is one more entry.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable

from data_agent.events import TurnResult
from evals.dataset import EvalCase

# "[shipment_delay_policy.pdf, §3.2 Express Shipments, p. 2]" -> source and section number
_CITATION_RE = re.compile(r"\[\s*([^\[\],§]+?\.\w+)\s*,\s*§\s*([0-9][\w.()\-]*)")


# Same tokenizer as the LakeQuest scorer (benchmark/LakeQuest/scoring_program/score.py).
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_SOURCES_RE = re.compile(r"\n?[ \t]*Sources:.*\Z", re.DOTALL | re.IGNORECASE)
_SQL_TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+[`"\[]?(\w+)', re.IGNORECASE)


def _passages(result: TurnResult) -> list[dict]:
    """The passages returned by every ``search_documents`` call of the turn."""
    out: list[dict] = []
    for call in result.calls:
        response = call.get("response") or {}
        if call.get("name") == "search_documents" and isinstance(response, dict):
            out.extend(p for p in response.get("passages") or [] if isinstance(p, dict))
    return out


def _section_match(wanted: str, actual: str) -> bool:
    """Tells whether two section numbers match exactly or as the prefix of a split section (``3.2`` / ``3.2(a)-(c)``)."""
    if not wanted or not actual:
        return False
    return wanted == actual or actual.startswith(wanted + "(") or wanted.startswith(actual + "(")


def _has_passage(passages: list[dict], source: str, section: str) -> bool:
    return any(
        p.get("source") == source and _section_match(section, str(p.get("section_number") or ""))
        for p in passages
    )


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def tool_sequence_match(case: EvalCase, result: TurnResult) -> float | None:
    """1.0 if the distinct tool names, in first-call order, equal ``expected.tools``."""
    if case.expected.tools is None:
        return None
    seen: list[str] = []
    for call in result.calls:
        if call["name"] not in seen:
            seen.append(call["name"])
    return float(seen == case.expected.tools)


def section_recall(case: EvalCase, result: TurnResult) -> float | None:
    """Share of ``expected.sections`` found among the passages returned in this turn."""
    wanted = case.expected.sections
    if not wanted:
        return None
    passages = _passages(result)
    return sum(_has_passage(passages, w.source, w.section) for w in wanted) / len(wanted)


def answer_contains(case: EvalCase, result: TurnResult) -> float | None:
    """Share of ``expected.answer_contains`` strings in the answer (case-insensitive, whitespace-normalised)."""
    wanted = case.expected.answer_contains
    if not wanted:
        return None
    answer = _normalise(result.answer)
    return sum(_normalise(w) in answer for w in wanted) / len(wanted)


def mrr(case: EvalCase, result: TurnResult) -> float | None:
    """Reciprocal rank (``1/rank``) of the first expected section among the retrieved passages, in ranked order.

    Meant for the retrieval mode, where the passages are the fused ranking. ``0.0`` when
    no expected section was retrieved.
    """
    wanted = case.expected.sections
    if not wanted:
        return None
    for rank, p in enumerate(_passages(result), start=1):
        if any(_has_passage([p], w.source, w.section) for w in wanted):
            return 1.0 / rank
    return 0.0


def citation_precision(case: EvalCase, result: TurnResult) -> float | None:
    """Share of the answer's ``[source, §number ...]`` citations that point to a passage returned in this turn.

    A deterministic grounding check. ``None`` when the answer has no citation.
    """
    cited = _CITATION_RE.findall(result.answer)
    if not cited:
        return None
    passages = _passages(result)
    return sum(_has_passage(passages, src, num) for src, num in cited) / len(cited)


def strip_sources(answer: str | None) -> str:
    """``answer`` without a trailing ``Sources:`` line (the shipments format), stripped."""
    return _SOURCES_RE.sub("", answer or "").strip()


def final_answer(result: TurnResult) -> str:
    """The answer without a trailing ``Sources:`` line (the shipments format), stripped.

    The LakeQuest profile asks for no such line; this guards against the model writing
    one anyway, so the text scored here equals the text written to ``predictions.jsonl``.
    """
    return strip_sources(result.answer)


def predicted_object_ids(result: TurnResult) -> list[str]:
    """Object ids the answer relied on, lower-cased and sorted.

    These are the sources of the passages the answer cites (without ``.md``), or of all
    returned passages when nothing is cited, plus the tables after ``FROM``/``JOIN`` in
    the successful ``run_sql`` calls.
    """
    passages = _passages(result)
    cited = _CITATION_RE.findall(result.answer or "")
    sources = {src for src, num in cited if _has_passage(passages, src, num)}
    if not sources:
        sources = {str(p.get("source")) for p in passages if p.get("source")}
    ids = {s.removesuffix(".md").lower() for s in sources}
    for call in result.calls:
        response = call.get("response")
        if call.get("name") == "run_sql" and isinstance(response, dict) and response.get("status") == "success":
            ids.update(t.lower() for t in _SQL_TABLE_RE.findall(str((call.get("args") or {}).get("query", ""))))
    return sorted(ids)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def _token_f1(reference: str, prediction: str) -> float:
    """Token F1 with the scorer's rules, including its empty-text cases."""
    ref, pred = _tokens(reference), _tokens(prediction)
    if not ref and not pred:
        return 1.0
    if not ref or not pred:
        return 0.0
    overlap = sum((Counter(ref) & Counter(pred)).values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / len(pred), overlap / len(ref)
    return 2 * precision * recall / (precision + recall)


def answer_f1(case: EvalCase, result: TurnResult) -> float | None:
    """Best token F1 of the final answer against ``expected.reference_answers`` (LakeQuest's answer score)."""
    refs = case.expected.reference_answers
    if not refs:
        return None
    answer = final_answer(result)
    return max(_token_f1(r, answer) for r in refs)


def object_f1(case: EvalCase, result: TurnResult) -> float | None:
    """Set F1 between :func:`predicted_object_ids` and ``expected.object_ids``."""
    gold = {g.strip().lower() for g in case.expected.object_ids if g.strip()}
    if not gold:
        return None
    pred = set(predicted_object_ids(result))
    hit = len(gold & pred)
    if hit == 0:
        return 0.0
    precision, recall = hit / len(pred), hit / len(gold)
    return 2 * precision * recall / (precision + recall)


def coverage(case: EvalCase, result: TurnResult) -> float | None:
    """1.0 if the final answer is not empty, else 0.0."""
    return float(bool(final_answer(result)))


METRICS: dict[str, Callable[[EvalCase, TurnResult], float | None]] = {
    "tool_sequence_match": tool_sequence_match,
    "section_recall": section_recall,
    "answer_contains": answer_contains,
    "citation_precision": citation_precision,
    "mrr": mrr,
    "answer_f1": answer_f1,
    "object_f1": object_f1,
    "coverage": coverage,
}

# Which metrics make sense in each ``eval.mode``: retrieval mode has no answer, tools or citations.
AGENT_METRICS = ("tool_sequence_match", "section_recall", "answer_contains", "citation_precision", "answer_f1", "object_f1", "coverage")
RETRIEVAL_METRICS = ("section_recall", "mrr")
