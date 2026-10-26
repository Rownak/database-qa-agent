"""AppTest UI tests for the document panel and answer expander (Phase 3.6); no LLM or network calls."""

import base64
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui_client
from data_agent import sessions
from tests.conftest import DB_SAMPLES, DOC_SAMPLES, end_session_button
from tests.test_app_api import FakeBackend

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SAMPLES = ["customer_communication_guidelines.pdf", "international_shipping_procedures.txt", "shipment_delay_policy.pdf"]
DOC_UPLOADER = 1  # the table uploader is first


def _names(state) -> list[str]:
    return [d["name"] for d in state["docs"]]


def _remove(at, name):
    at.button(key=f"remove_{name}").click().run()


def _add(at, path: Path, name: str | None = None, data: bytes | None = None):
    at.file_uploader[DOC_UPLOADER].upload(name or path.name, data if data is not None else path.read_bytes()).run()


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


def _current(at, backend):
    return backend.sessions[at.session_state.api_sid]


def test_start_lists_the_sample_documents_and_sends_no_docs_key(at, backend):
    assert not at.exception
    assert [e["name"] for e in at.session_state.doc_entries] == SAMPLES
    assert _current(at, backend) == {}  # missing key = the backend loads the samples
    text = " ".join(m.value for m in at.markdown)
    assert all(n in text for n in SAMPLES) and "chunks" in text
    assert [b.label for b in at.button].count("Remove") == 3


def test_remove_a_document_starts_a_new_session_with_the_rest(at, backend):
    old = at.session_state.api_sid
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _remove(at, SAMPLES[1])
    new = at.session_state.api_sid
    assert new != old and old in backend.deleted and list(backend.sessions) == [new]
    assert _names(backend.sessions[new]) == [SAMPLES[0], SAMPLES[2]]
    assert base64.b64decode(backend.sessions[new]["docs"][0]["b64"])  # the bytes travel with the state
    assert at.session_state.messages == []  # the chat was reset
    assert [b.label for b in at.button].count("Remove") == 2


def test_remove_every_document_sends_an_empty_list(at, backend):
    for name in SAMPLES:
        _remove(at, name)
    assert _current(at, backend)["docs"] == []
    assert "No documents loaded" in " ".join(c.value for c in at.caption)


def test_add_a_document(at, backend):
    _remove(at, SAMPLES[0])
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _add(at, DOC_SAMPLES / "structured.md")
    assert not at.error and at.session_state.messages == []
    assert _names(_current(at, backend)) == [SAMPLES[1], SAMPLES[2], "structured.md"]
    assert [e["name"] for e in at.session_state.doc_entries][-1] == "structured.md"


def test_same_name_replaces_the_document_in_place(at, backend):
    _remove(at, SAMPLES[0])
    _add(at, DOC_SAMPLES / "structured.md")
    first = next(e for e in at.session_state.doc_entries if e["name"] == "structured.md")
    longer = (DOC_SAMPLES / "structured.md").read_bytes() + b"\n## Extra\n\nMore text for the second version.\n"
    _add(at, DOC_SAMPLES / "structured.md", data=longer)
    entries = at.session_state.doc_entries
    assert [e["name"] for e in entries] == [SAMPLES[1], SAMPLES[2], "structured.md"]  # still 3, same position
    assert entries[-1]["b64"] != first["b64"] and not at.error
    assert base64.b64decode(_current(at, backend)["docs"][-1]["b64"]) == longer


def test_replacing_works_when_three_documents_are_loaded(at, backend):
    """The limit counts distinct names, so a same-name file is not a fourth document."""
    data = (DOC_SAMPLES / "structured.md").read_bytes()
    _add(at, DOC_SAMPLES / "structured.md", name=SAMPLES[1], data=data)  # Markdown text under a .txt name is valid
    assert not at.error and len(at.session_state.doc_entries) == 3
    assert base64.b64decode(_current(at, backend)["docs"][1]["b64"]) == data


def test_fourth_document_is_rejected_and_nothing_changes(at, backend):
    sid = at.session_state.api_sid
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _add(at, DOC_SAMPLES / "structured.md")
    assert at.error[0].value == "Up to 3 documents per session. Remove one first."
    assert at.session_state.api_sid == sid and list(backend.sessions) == [sid] and not backend.deleted
    assert len(at.session_state.doc_entries) == 3 and len(at.session_state.messages) == 1


@pytest.mark.parametrize("name", ["scanned.pdf", "empty.txt", "latin1.txt", "encrypted.pdf", "oversize_pages.pdf"])
def test_rejected_file_shows_an_error_and_changes_nothing(at, backend, name):
    _remove(at, SAMPLES[0])  # room for a fourth, so the failure is the file's
    sid, deleted = at.session_state.api_sid, list(backend.deleted)
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _add(at, DOC_SAMPLES / name)
    assert len(at.error) == 1 and "Up to 3" not in at.error[0].value
    assert at.session_state.api_sid == sid and list(backend.sessions) == [sid] and backend.deleted == deleted
    assert len(at.session_state.doc_entries) == 2 and len(at.session_state.messages) == 1


def test_error_clears_after_a_good_change(at, backend):
    _add(at, DOC_SAMPLES / "structured.md")  # fourth: rejected
    assert at.error
    _remove(at, SAMPLES[0])
    assert not at.error


def test_backend_failure_on_a_document_change_keeps_everything(at, backend):
    sid = at.session_state.api_sid
    backend.fail_create = True
    _remove(at, SAMPLES[0])
    assert "Could not send the documents" in at.error[0].value
    assert at.session_state.api_sid == sid and len(at.session_state.doc_entries) == 3


def test_table_upload_after_document_change_keeps_the_documents(at, backend):
    _remove(at, SAMPLES[0])
    f = DB_SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    state = _current(at, backend)
    assert state["upload_name"] == f.name and _names(state) == [SAMPLES[1], SAMPLES[2]]


def test_document_change_keeps_the_table_upload(at, backend):
    f = DB_SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    _remove(at, SAMPLES[0])
    state = _current(at, backend)
    assert state["upload_name"] == f.name and _names(state) == [SAMPLES[1], SAMPLES[2]]


def test_lost_backend_session_is_recreated_with_the_current_documents(at, backend):
    _remove(at, SAMPLES[0])
    at.chat_input[0].set_value("first").run()
    backend.sessions.clear()  # the backend instance was replaced
    at.chat_input[0].set_value("second").run()
    assert _names(_current(at, backend)) == [SAMPLES[1], SAMPLES[2]]
    assert backend.asked[-1][1] == "second"


def test_end_session_resets_documents_to_the_samples(at, backend):
    _remove(at, SAMPLES[0])
    end_session_button(at).click().run()
    assert [e["name"] for e in at.session_state.doc_entries] == SAMPLES
    assert backend.sessions == {at.session_state.api_sid: {}}


def test_document_uploader_is_not_reloaded_after_end_session(at, backend):
    _remove(at, SAMPLES[0])
    _add(at, DOC_SAMPLES / "structured.md")
    end_session_button(at).click().run()
    assert "structured.md" not in [e["name"] for e in at.session_state.doc_entries]


# --- expander ---


def test_expander_shows_intent_sql_and_retrieval_with_citations(at, backend):
    passage = {
        "source": "shipment_delay_policy.pdf", "section_number": "3.2",
        "section_path": "3 Rules > 3.2 Express Shipments", "page_start": 1, "page_end": 2,
    }
    calls = [
        {"name": "run_sql", "args": {"query": "SELECT 1"}, "response": {"status": "success"}},
        {
            "name": "search_documents",
            "args": {"query": "express delay", "section": "express"},
            "response": {"status": "success", "passages": [passage], "note": "keyword only"},
        },
    ]
    backend.ask = lambda sid, q: ("answer", calls, "hybrid")
    ui_client.ask = backend.ask
    at.chat_input[0].set_value("q").run()
    assert not at.exception
    text = " ".join(m.value for m in at.expander[0].markdown)
    assert "Intent:** hybrid" in text and "search_documents" in text and "express delay" in text
    assert "shipment_delay_policy.pdf, §3.2 Express Shipments, pp. 1-2" in text
    assert at.expander[0].code[0].value == "SELECT 1"
    captions = " ".join(c.value for c in at.expander[0].caption)
    assert "section = express" in captions and "keyword only" in captions


def test_expander_shows_a_search_error_and_empty_results(at, backend):
    calls = [
        {"name": "search_documents", "args": {"query": "a"}, "response": {"status": "error", "error_message": "No documents are loaded"}},
        {"name": "search_documents", "args": {"query": "b"}, "response": {"status": "success", "passages": []}},
    ]
    ui_client.ask = lambda sid, q: ("answer", calls, None)
    at.chat_input[0].set_value("q").run()
    captions = " ".join(c.value for c in at.expander[0].caption)
    assert "No documents are loaded" in captions and "No passages returned" in captions


def test_no_expander_without_calls_or_intent(at, backend):
    ui_client.ask = lambda sid, q: ("hello", [], None)
    at.chat_input[0].set_value("q").run()
    assert not at.expander


# --- local (in-process) mode ---


@pytest.fixture
def local():
    app = AppTest.from_file(APP, default_timeout=30).run()
    yield app
    sessions.end_session(sessions.ROOT / app.session_state.session_id)


def _adk_state(app):
    sid = app.session_state.session_id
    return app.session_state.agent.adk.sessions["sheet_qa"]["user"][sid].state


def test_local_mode_document_change_resets_session_and_deletes_old_docs_db(local):
    folder = sessions.ROOT / local.session_state.session_id
    (folder / "docs.db").write_bytes(b"stale")
    local.session_state.messages = [{"role": "user", "content": "hi"}]
    _remove(local, SAMPLES[0])
    assert not (folder / "docs.db").exists() and (folder / "data.db").is_file()
    assert local.session_state.messages == [] and len(local.session_state.doc_entries) == 2
    state = _adk_state(local)
    assert [d["name"] for d in state["docs"]] == [SAMPLES[1], SAMPLES[2]] and state["db_path"]


def test_local_mode_sample_state_has_no_docs_key(local):
    assert "docs" not in _adk_state(local)


def test_local_mode_collects_tool_responses_and_intent_from_events(local):
    from google.adk.events import Event
    from google.adk.events.event_actions import EventActions
    from google.genai import types

    def event(*parts, delta=None):
        content = types.Content(role="model", parts=list(parts)) if parts else None
        return Event(author="qa_orchestrator", content=content, actions=EventActions(state_delta=delta or {}))

    class FakeRunner:
        async def run_async(self, user_id, session_id, new_message):
            yield event(delta={"intent": "document"})
            yield event(types.Part(function_call=types.FunctionCall(id="1", name="search_documents", args={"query": "late"})))
            yield event(types.Part(function_response=types.FunctionResponse(
                id="1", name="search_documents", response={"status": "success", "passages": []})))
            yield event(types.Part(text="Final answer."))

    local.session_state.agent.runner = FakeRunner()
    local.chat_input[0].set_value("q").run()
    assert not local.exception
    message = local.session_state.messages[-1]
    assert message["content"] == "Final answer." and message["intent"] == "document"
    assert message["calls"] == [
        {"name": "search_documents", "args": {"query": "late"}, "response": {"status": "success", "passages": []}}
    ]


def test_local_mode_reads_the_intent_from_the_session_when_no_delta_has_it(local):
    from google.adk.events import Event
    from google.genai import types

    class FakeRunner:
        async def run_async(self, user_id, session_id, new_message):
            yield Event(author="a", content=types.Content(role="model", parts=[types.Part(text="Hi")]))

    _adk_state(local)["intent"] = "database"
    local.session_state.agent.runner = FakeRunner()
    local.chat_input[0].set_value("q").run()
    assert local.session_state.messages[-1]["intent"] == "database"
