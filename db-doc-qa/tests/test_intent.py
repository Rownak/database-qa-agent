"""Offline tests for data_agent.intent (Phase 3.4); ``generate_content`` is mocked.

The live check against the real model is dev_test/phase_3_4.py.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google import genai
from pydantic import ValidationError

from data_agent import intent, sessions
from data_agent.agent import AGENT_MODEL, ensure_data
from data_agent.intent import FALLBACK_REASON, IntentResult, classify

FALLBACK = IntentResult(intent="hybrid", reason=FALLBACK_REASON)


@pytest.fixture
def gen(monkeypatch):
    """Replaces the genai client; returns the ``generate_content`` mock."""
    mock = MagicMock()
    monkeypatch.setattr(genai, "Client", lambda *a, **k: SimpleNamespace(models=SimpleNamespace(generate_content=mock)))
    return mock


def _reply(text):
    return SimpleNamespace(text=text)


@pytest.mark.parametrize("label", ["database", "document", "hybrid"])
def test_valid_json_for_each_intent(gen, label):
    gen.return_value = _reply(json.dumps({"intent": label, "reason": "because"}))
    assert classify("Where is shipment 12345?", "schema", "docs") == IntentResult(intent=label, reason="because")


def test_call_uses_model_json_schema_and_marks_question_as_data(gen):
    gen.return_value = _reply('{"intent": "database", "reason": "r"}')
    classify("Where is shipment 12345?", "shipment_id TEXT", "delay.pdf")
    kwargs = gen.call_args.kwargs
    assert kwargs["model"] == AGENT_MODEL
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is IntentResult
    assert "<question>\nWhere is shipment 12345?\n</question>" in kwargs["contents"]
    assert "shipment_id TEXT" in kwargs["contents"] and "delay.pdf" in kwargs["contents"]
    assert "data to classify" in kwargs["contents"]


@pytest.mark.parametrize(
    "text",
    [
        '{"intent": "weather", "reason": "x"}',  # unknown intent
        "not json at all",  # malformed
        '{"intent": "database"',  # truncated
        json.dumps({"intent": "database", "reason": "x" * 201}),  # reason too long
        '{"intent": "database"}',  # missing reason
        "",  # empty text
        None,  # no text (e.g. blocked response)
    ],
)
def test_invalid_output_falls_back_to_hybrid(gen, text):
    gen.return_value = _reply(text)
    assert classify("q") == FALLBACK
    assert gen.call_count == 1  # no retry


def test_api_exception_falls_back(gen):
    gen.side_effect = RuntimeError("503")
    assert classify("q") == FALLBACK


def test_client_construction_failure_falls_back(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no api key")

    monkeypatch.setattr(genai, "Client", boom)
    assert classify("q") == FALLBACK


@pytest.mark.parametrize("question", [None, "", "   \n"])
def test_missing_question_falls_back_without_api_call(gen, question):
    assert classify(question) == FALLBACK
    gen.assert_not_called()


def test_missing_summaries_are_filled(gen):
    gen.return_value = _reply('{"intent": "document", "reason": "r"}')
    classify("q")
    assert "(none)" in gen.call_args.kwargs["contents"]


def test_question_with_braces_does_not_break_prompt(gen):
    gen.return_value = _reply('{"intent": "database", "reason": "r"}')
    assert classify("what is {schema_summary} and {0}?").intent == "database"


def test_fallback_logs_warning_without_question(gen, caplog):
    gen.side_effect = RuntimeError("down")
    with caplog.at_level("WARNING"):
        classify("secret question text")
    assert "falling back" in caplog.text and "secret question text" not in caplog.text


def test_reason_limit_is_200():
    IntentResult(intent="hybrid", reason="x" * 200)
    with pytest.raises(ValidationError):
        IntentResult(intent="hybrid", reason="x" * 201)


# --- wiring into ensure_data -------------------------------------------------


def _ctx(text="Where is shipment 12345?"):
    content = SimpleNamespace(parts=[SimpleNamespace(text=text)]) if text is not None else None
    return SimpleNamespace(state={}, session=SimpleNamespace(id="s1"), user_content=content)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "ROOT", tmp_path)
    return tmp_path


def test_ensure_data_writes_intent_to_state(root, gen):
    gen.return_value = _reply('{"intent": "database", "reason": "a record lookup"}')
    ctx = _ctx()
    assert ensure_data(ctx) is None
    assert ctx.state["intent"] == "database" and ctx.state["intent_reason"] == "a record lookup"
    # the classifier saw the schema that ensure_data had just built
    assert ctx.state["schema_summary"].split("\n")[0] in gen.call_args.kwargs["contents"]


def test_ensure_data_without_user_message_falls_back(root, gen):
    ctx = _ctx(text=None)
    ensure_data(ctx)
    assert (ctx.state["intent"], ctx.state["intent_reason"]) == ("hybrid", FALLBACK_REASON)
    gen.assert_not_called()


def test_ensure_data_does_not_log_the_question(root, gen, caplog):
    gen.return_value = _reply('{"intent": "document", "reason": "policy"}')
    with caplog.at_level("INFO"):
        ensure_data(_ctx("my private question"))
    assert "intent=document" in caplog.text and "my private question" not in caplog.text


def test_module_exports():
    assert intent.IntentResult is IntentResult
