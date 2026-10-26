"""Pure scoring functions over ``(case, TurnResult)`` (design section 5.3).

Each metric returns a float between 0 and 1, or ``None`` when the case has nothing to
compare (for example no expected sections). :data:`METRICS` maps the metric name used in
the YAML config to its function, so a new metric is one more entry.
"""

from __future__ import annotations

import re
from typing import Callable

from data_agent.events import TurnResult
from evals.dataset import EvalCase

# "[shipment_delay_policy.pdf, §3.2 Express Shipments, p. 2]" -> source and section number
_CITATION_RE = re.compile(r"\[\s*([^\[\],§]+?\.\w+)\s*,\s*§\s*([0-9][\w.()\-]*)")


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


def citation_precision(case: EvalCase, result: TurnResult) -> float | None:
    """Share of the answer's ``[source, §number ...]`` citations that point to a passage returned in this turn.

    A deterministic grounding check. ``None`` when the answer has no citation.
    """
    cited = _CITATION_RE.findall(result.answer)
    if not cited:
        return None
    passages = _passages(result)
    return sum(_has_passage(passages, src, num) for src, num in cited) / len(cited)


METRICS: dict[str, Callable[[EvalCase, TurnResult], float | None]] = {
    "tool_sequence_match": tool_sequence_match,
    "section_recall": section_recall,
    "answer_contains": answer_contains,
    "citation_precision": citation_precision,
}
