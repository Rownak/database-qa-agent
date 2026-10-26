"""Tests for data_agent.events: TurnResult and parse_events (pure JSON, no ADK)."""

import dataclasses

import pytest

from data_agent.events import TurnResult, parse_events


def _call(name, args=None, id=None):
    return {"content": {"parts": [{"functionCall": {"name": name, "args": args or {}, **({"id": id} if id else {})}}]}}


def _resp(name, response, id=None):
    return {"content": {"parts": [{"functionResponse": {"name": name, "response": response, **({"id": id} if id else {})}}]}}


def test_turn_result_fields_and_defaults():
    r = TurnResult("a", [], "hybrid", 1.5)
    assert (r.answer, r.calls, r.intent, r.latency_s, r.error) == ("a", [], "hybrid", 1.5, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.answer = "b"


def test_empty_events_and_call_without_text():
    assert parse_events([]) == TurnResult("", [], None, 0.0)
    r = parse_events([{"content": {"parts": [{"functionCall": {"name": "x"}}]}}], latency_s=2.0)
    assert r.answer == "" and r.calls == [{"name": "x", "args": {}, "response": None}] and r.latency_s == 2.0


def test_answer_is_the_last_text_and_skips_thoughts():
    events = [{"content": {"parts": [{"text": "hmm", "thought": True}, {"text": "It is 1."}]}}, {"actions": {}}]
    assert parse_events(events).answer == "It is 1."


def test_matches_function_responses_by_id():
    events = [
        _call("run_sql", {"query": "a"}, "1"), _call("search_documents", {"query": "b"}, "2"),
        _resp("search_documents", {"status": "success", "passages": []}, "2"),
        _resp("run_sql", {"rows": [[1]]}, "1"),
    ]
    calls = parse_events(events).calls
    assert [(c["name"], c["response"]) for c in calls] == [
        ("run_sql", {"rows": [[1]]}), ("search_documents", {"status": "success", "passages": []}),
    ]


def test_matches_by_name_in_order_without_ids():
    events = [_call("run_sql"), _resp("run_sql", {"n": 1}), _call("run_sql"), _resp("run_sql", {"n": 2})]
    assert [c["response"] for c in parse_events(events).calls] == [{"n": 1}, {"n": 2}]


def test_unanswered_call_and_stray_response_are_tolerated():
    assert parse_events([_call("run_sql"), _resp("search_documents", {"x": 1})]).calls[0]["response"] is None


def test_intent_comes_from_state_delta_and_must_be_a_string():
    events = [{"actions": {"stateDelta": {"intent": "hybrid"}}}, {"content": {"parts": [{"text": "hi"}]}}]
    assert parse_events(events) == TurnResult("hi", [], "hybrid", 0.0)
    assert parse_events([{"actions": {"stateDelta": {"intent": 5}}}]).intent is None
