"""Tests for data_agent.runner (no network: the ADK Runner is replaced by canned events)."""

import base64

import pytest
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

from data_agent import runner
from data_agent.config import AgentConfig
from data_agent.runner import AgentSession, files_state
from tests.conftest import DB_SAMPLES, DOC_SAMPLES, SALES


def _event(*parts, delta=None):
    content = types.Content(role="model", parts=list(parts)) if parts else None
    return Event(author="qa_orchestrator", content=content, actions=EventActions(state_delta=delta or {}))


class FakeRunner:
    def __init__(self, events=(), exc=None, **_):
        self.events, self.exc = list(events), exc

    async def run_async(self, user_id, session_id, new_message):
        for ev in self.events:
            yield ev
        if self.exc:
            raise self.exc


@pytest.fixture
def patched(monkeypatch):
    """Patches the Runner class with one that yields ``events`` (set on ``patched.events``)."""
    box = type("Box", (), {"events": [], "exc": None})()
    monkeypatch.setattr(runner, "Runner", lambda **kw: FakeRunner(box.events, box.exc))
    return box


def test_files_state_variants():
    assert files_state(None, None) == {}
    assert files_state(None, [])["docs"] == []
    state = files_state(SALES, [next(DOC_SAMPLES.iterdir())])
    assert state["upload_name"] == "sales.csv"
    assert base64.b64decode(state["upload_b64"]) == SALES.read_bytes()
    assert [d["name"] for d in state["docs"]] == [next(DOC_SAMPLES.iterdir()).name]


def test_ask_returns_a_turn_result(patched):
    patched.events = [
        _event(delta={"intent": "document"}),
        _event(types.Part(function_call=types.FunctionCall(id="1", name="search_documents", args={"query": "late"}))),
        _event(types.Part(function_response=types.FunctionResponse(id="1", name="search_documents", response={"status": "success"}))),
        _event(types.Part(text="Final answer.")),
    ]
    session = AgentSession(AgentConfig(), {})
    result = session.ask("q")
    assert result.answer == "Final answer." and result.intent == "document" and result.error is None
    assert result.calls == [{"name": "search_documents", "args": {"query": "late"}, "response": {"status": "success"}}]
    assert result.latency_s >= 0


def test_intent_falls_back_to_the_session_state(patched):
    patched.events = [_event(types.Part(text="Hi"))]
    session = AgentSession(AgentConfig(), {"intent": "database"})
    assert session.ask("q").intent == "database"


def test_an_exception_becomes_error_and_does_not_raise(patched):
    patched.events = [_event(types.Part(text="partial"))]
    patched.exc = RuntimeError("boom")
    result = AgentSession(AgentConfig(), {}).ask("q")
    assert result.error == "RuntimeError: boom" and result.answer == "partial"


def test_close_deletes_the_session_and_optionally_the_folder(patched, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.sessions, "ROOT", tmp_path)
    folder = tmp_path / "abc"
    folder.mkdir()
    session = AgentSession(AgentConfig(), {}, session_id="abc")
    assert session.adk.sessions[runner.APP_NAME][runner.USER_ID]["abc"]
    session.close(delete_files=False)
    assert folder.exists() and not session.adk.sessions[runner.APP_NAME][runner.USER_ID]
    session.close()
    assert not folder.exists()


def test_each_session_is_isolated(patched):
    a, b = AgentSession(AgentConfig(), {}), AgentSession(AgentConfig(), {})
    assert a.session_id != b.session_id and a.adk is not b.adk
