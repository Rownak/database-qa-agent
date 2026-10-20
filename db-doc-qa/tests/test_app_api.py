"""AppTest UI tests for app.py in API mode (Phase 2.3) with ``ui_client`` replaced by a fake backend."""

import ast
import base64
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui_client
from data_agent import sessions
from tests.conftest import SAMPLES

APP = str(Path(__file__).resolve().parent.parent / "app.py")


class FakeBackend:
    """Stands in for the ADK API server: remembers sessions and can forget them."""

    def __init__(self):
        self.sessions, self.deleted, self.asked = {}, [], []
        self.fail_create = False

    def create_session(self, sid, state):
        if self.fail_create:
            raise ui_client.ApiError("service down")
        self.sessions[sid] = state

    def delete_session(self, sid):
        self.deleted.append(sid)
        if self.sessions.pop(sid, None) is None:
            raise ui_client.SessionNotFound("gone")

    def ask(self, sid, question):
        self.asked.append((sid, question))
        if sid not in self.sessions:
            raise ui_client.SessionNotFound("gone")
        return "There are 50 records.\n\nSource: get_table_info", [{"name": "get_table_info", "args": {}}]


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    for name in ("create_session", "delete_session", "ask"):
        monkeypatch.setattr(ui_client, name, getattr(fake, name))
    return fake


@pytest.fixture
def at(backend):
    app = AppTest.from_file(APP, default_timeout=30).run()
    yield app
    if "session_id" in app.session_state:
        sessions.end_session(sessions.ROOT / app.session_state.session_id)


def test_start_creates_backend_session_with_sample_state(at, backend):
    assert not at.exception and "shipments.csv (sample)" in at.subheader[0].value
    assert backend.sessions == {at.session_state.api_sid: {}}


def test_valid_upload_sends_bytes_and_replaces_old_session(at, backend):
    old = at.session_state.api_sid
    f = SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    new = at.session_state.api_sid
    assert new != old and old in backend.deleted and list(backend.sessions) == [new]
    state = backend.sessions[new]
    assert state["upload_name"] == f.name and base64.b64decode(state["upload_b64"]) == f.read_bytes()


def test_invalid_upload_never_reaches_backend(at, backend):
    old = at.session_state.api_sid
    f = SAMPLES / "duplicate_header.csv"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    assert "Duplicate column name" in at.error[0].value
    assert list(backend.sessions) == [old] and not backend.deleted


def test_backend_failure_on_upload_keeps_current_data(at, backend):
    old = at.session_state.api_sid
    backend.fail_create = True
    f = SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    assert "Could not send the file" in at.error[0].value
    assert at.session_state.api_sid == old and "shipments.csv (sample)" in at.subheader[0].value


def test_question_is_answered_through_the_backend(at, backend):
    at.chat_input[0].set_value("How many records?").run()
    assert not at.exception
    assert [m["role"] for m in at.session_state.messages] == ["user", "assistant"]
    assert "Source:" in at.session_state.messages[1]["content"]
    assert backend.asked == [(at.session_state.api_sid, "How many records?")]


def test_lost_session_is_recreated_from_held_upload_and_question_retried(at, backend):
    f = SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    at.chat_input[0].set_value("first").run()
    lost = at.session_state.api_sid
    backend.sessions.clear()  # the backend instance was replaced
    at.chat_input[0].set_value("second").run()
    new = at.session_state.api_sid
    assert new != lost and backend.sessions[new]["upload_name"] == f.name
    assert "chat was reset" in at.info[0].value
    # The chat holds only the retried question: the earlier history was cleared.
    assert [m["content"] for m in at.session_state.messages if m["role"] == "user"] == ["second"]
    assert backend.asked[-1] == (new, "second")


def test_end_session_deletes_backend_session(at, backend):
    sid = at.session_state.api_sid
    at.button[0].click().run()
    assert sid in backend.deleted and sid not in backend.sessions


def test_unreachable_backend_at_start_shows_error(backend):
    backend.fail_create = True
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert "Could not start a session" in app.error[0].value
    assert "session_id" not in app.session_state


def test_app_has_no_top_level_adk_import():
    """The UI image has no ADK, so app.py may import it only inside functions."""
    tree = ast.parse(Path(APP).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names] + [n.module or "" for n in top if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n.startswith(("google.adk", "google.genai", "data_agent.agent"))]


def test_end_session_after_upload_starts_a_sample_session(at, backend):
    f = SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    at.button[0].click().run()
    assert "shipments.csv (sample)" in at.subheader[0].value
    assert backend.sessions == {at.session_state.api_sid: {}}  # sample state, no upload
