"""Turn results and ADK event parsing shared by the app, the API client and the evaluation.

The ADK server answers ``POST /run`` with a JSON list of events, and the in-process
runner (:mod:`data_agent.runner`) dumps its events to the same JSON shape. This module
turns that list into one :class:`TurnResult`, so every caller reads an answer the same
way. It handles plain dicts only and imports no ADK, so the UI image can use it.

It also derives the optional per-turn latency trace (:func:`turn_timing`) from the same events.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one question to the agent.

    Attributes:
        answer: Text of the final answer; empty if the agent produced none.
        calls: Tool calls in order, each ``{"name", "args", "response"}``; ``response``
            is the matching ``functionResponse`` payload, or ``None`` if none arrived.
        intent: The classified intent (``database``, ``document`` or ``hybrid``), or ``None``.
        latency_s: Wall-clock seconds for the turn (0.0 when not measured).
        error: An exception message when the turn failed; a turn never raises.
        timing: The latency trace (see :func:`turn_timing`), only when ``agent.latency_trace``
            is on; ``None`` otherwise.
    """

    answer: str
    calls: list[dict] = field(default_factory=list)
    intent: str | None = None
    latency_s: float = 0.0
    error: str | None = None
    timing: dict | None = None


def parse_events(events: list[dict], latency_s: float = 0.0) -> TurnResult:
    """Extracts the final answer, the tool calls and the intent from ``/run`` events.

    Args:
        events: The JSON list returned by ``POST /run`` (or the same events dumped from
            ADK ``Event`` objects with aliases, in local mode).
        latency_s: Duration of the turn, stored in the result.

    Returns:
        A :class:`TurnResult`. The answer is the text of the last event that has any
        (model "thought" parts are skipped). Each call's ``response`` is matched by id,
        else by name in call order. ``intent`` is the last ``stateDelta["intent"]``.
    """
    calls: list[dict] = []
    ids: list[str | None] = []
    answer = ""
    intent: str | None = None
    for event in events:
        delta = (event.get("actions") or {}).get("stateDelta") or {}
        if isinstance(delta.get("intent"), str):
            intent = delta["intent"]
        parts = (event.get("content") or {}).get("parts") or []
        for part in parts:
            call = part.get("functionCall")
            if call:
                calls.append({"name": call.get("name", ""), "args": dict(call.get("args") or {}), "response": None})
                ids.append(call.get("id"))
            resp = part.get("functionResponse")
            if resp:
                _attach_response(calls, ids, resp)
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought"))
        if text:
            answer = text
    return TurnResult(answer, calls, intent, latency_s)


def _attach_response(calls: list[dict], ids: list[str | None], resp: dict) -> None:
    """Stores a ``functionResponse`` on the call it answers (by id, else the first open call of that name)."""
    rid, name = resp.get("id"), resp.get("name")
    for call, cid in zip(calls, ids):
        if call["response"] is not None:
            continue
        if (rid and cid == rid) or (not rid and call["name"] == name):
            call["response"] = resp.get("response") if resp.get("response") is not None else {}
            return


SETUP_STAGES = ("data_build", "docs_build")  # ingestion: subtracted from latency_s to get query_s
_TOLERANCE_S = 0.01  # clock jitter allowed before the stages are considered to overlap


def summarize_stages(stages: list[dict], latency_s: float) -> dict:
    """Closes a list of non-overlapping stages into a trace for one turn.

    Args:
        stages: ``[{"stage", "s", ...}]`` for the steps that ran, in order. Only top-level steps
            belong here: a nested step (for example a query embedding inside a tool) would be
            counted twice.
        latency_s: Wall-clock seconds of the whole turn.

    Returns:
        ``{"stages", "other_s", "query_s", "ok"}``. ``other_s`` is the residual: ``latency_s``
        minus the stages, so the parts always add up to the total. ``query_s`` is ``latency_s``
        without the ingestion stages (:data:`SETUP_STAGES`). ``ok`` is false when the residual is
        negative beyond a 10 ms tolerance, which means the stages overlapped; such a trace is
        left out of the aggregates.
    """
    other = latency_s - sum(st["s"] for st in stages)
    setup = sum(st["s"] for st in stages if st["stage"] in SETUP_STAGES)
    return {
        "stages": [{**st, "s": round(st["s"], 4)} for st in stages],
        "other_s": round(other, 4),
        "query_s": round(latency_s - setup, 4),
        "ok": other >= -_TOLERANCE_S,
    }


def turn_timing(events: list[dict], arrivals: list[float], setup: list[dict], latency_s: float) -> dict:
    """Derives the stages of one agent turn from when its events arrived.

    The runner does not stream, so a model event arrives when its model call has finished and a
    function-response event when its tools have. Each event that carries content therefore
    closes one stage, running from the previous such event (or from the end of the session setup)
    to its own arrival: ``llm`` for a model event, ``tool`` for a function response. Only events
    that happened produce a stage, so a turn with no tool call has no ``tool`` entry. Events
    without content (state deltas) and partial streaming chunks are skipped; the time before
    the next stage absorbs them.

    Args:
        events: The turn's events, as :func:`parse_events` reads them.
        arrivals: Seconds since the turn started at which each event arrived (same length).
        setup: ``[{"stage", "s"}]`` that ``ensure_data`` ran before the first model call. Their
            total is taken off the wait for the first model event, since it ran inside it.
        latency_s: Wall-clock seconds of the whole turn.

    Returns:
        The trace of :func:`summarize_stages`. A model stage carries ``in_tok`` / ``out_tok``
        from ``usageMetadata`` when present; a tool stage carries ``names``.
    """
    stages = [{"stage": st["stage"], "s": float(st["s"])} for st in setup]
    cursor = sum(st["s"] for st in stages)
    for event, at in zip(events, arrivals):
        parts = (event.get("content") or {}).get("parts") or []
        if event.get("partial") or not parts:
            continue
        responses = [p["functionResponse"].get("name", "") for p in parts if p.get("functionResponse")]
        if responses:
            stage = {"stage": "tool", "s": max(0.0, at - cursor), "names": responses}
        else:
            stage = {"stage": "llm", "s": max(0.0, at - cursor)}
            usage = event.get("usageMetadata") or {}
            if "promptTokenCount" in usage:
                stage["in_tok"] = usage["promptTokenCount"]
            if "candidatesTokenCount" in usage:
                stage["out_tok"] = usage["candidatesTokenCount"]
        stages.append(stage)
        cursor = max(cursor, at)
    return summarize_stages(stages, latency_s)
