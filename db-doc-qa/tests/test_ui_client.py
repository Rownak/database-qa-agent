"""Tests for ui_client (Phase 2.3) with ``requests`` mocked: URLs, auth headers, 404 handling, /run parsing."""

import pytest
import requests

import ui_client

CLOUD = "https://data-qa-api-abc.a.run.app"


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload if payload is not None else []

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def sent(monkeypatch):
    """Records every request; set ``sent.response`` to change what comes back."""

    class Recorder(list):
        response = FakeResponse()

    rec = Recorder()

    def fake_request(method, url, json=None, headers=None, timeout=None):
        rec.append({"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(rec.response, Exception):
            raise rec.response
        return rec.response

    monkeypatch.setattr(ui_client.requests, "request", fake_request)
    monkeypatch.setattr(ui_client, "_token_cache", {})
    return rec


def test_api_url_strips_slash_and_empty_is_none(monkeypatch):
    monkeypatch.setenv("API_URL", " http://localhost:8000/ ")
    assert ui_client.api_url() == "http://localhost:8000"
    monkeypatch.setenv("API_URL", "")
    assert ui_client.api_url() is None
    monkeypatch.delenv("API_URL")
    assert ui_client.api_url() is None


def test_localhost_sends_no_token(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    ui_client.create_session("s1", {"upload_name": "a.csv", "upload_b64": "QQ=="})
    req = sent[0]
    assert req["method"] == "POST" and req["headers"] == {}
    assert req["url"] == "http://localhost:8000/apps/data_agent/users/user/sessions/s1"
    assert req["json"] == {"upload_name": "a.csv", "upload_b64": "QQ=="}  # flat state, not wrapped


def test_remote_sends_bearer_token_and_caches_it(monkeypatch, sent):
    fetched = []
    monkeypatch.setattr(
        "google.oauth2.id_token.fetch_id_token",
        lambda request, audience: fetched.append(audience) or f"tok{len(fetched)}",
    )
    monkeypatch.setenv("API_URL", CLOUD)
    ui_client.create_session("s1", {})
    ui_client.delete_session("s1")
    assert [r["headers"] for r in sent] == [{"Authorization": "Bearer tok1"}] * 2
    assert fetched == [CLOUD] and sent[1]["method"] == "DELETE"


def test_token_refreshed_after_ttl(monkeypatch, sent):
    n = []
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", lambda r, a: n.append(1) or f"t{len(n)}")
    monkeypatch.setenv("API_URL", CLOUD)
    now = [1000.0]
    monkeypatch.setattr(ui_client.time, "monotonic", lambda: now[0])
    ui_client.create_session("s1", {})
    now[0] += ui_client.TOKEN_TTL_S + 1
    ui_client.create_session("s2", {})
    assert [r["headers"]["Authorization"] for r in sent] == ["Bearer t1", "Bearer t2"]


def test_token_failure_is_api_error(monkeypatch, sent):
    def boom(request, audience):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", boom)
    monkeypatch.setenv("API_URL", CLOUD)
    with pytest.raises(ui_client.ApiError, match="ID token"):
        ui_client.create_session("s1", {})
    assert not sent


def test_404_is_session_not_found_and_other_errors_are_api_errors(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    sent.response = FakeResponse(404)
    with pytest.raises(ui_client.SessionNotFound):
        ui_client.ask("s1", "hi")
    sent.response = FakeResponse(403)
    with pytest.raises(ui_client.ApiError, match="403") as info:
        ui_client.ask("s1", "hi")
    assert not isinstance(info.value, ui_client.SessionNotFound)
    sent.response = requests.ConnectionError("down")
    with pytest.raises(ui_client.ApiError, match="Could not reach"):
        ui_client.delete_session("s1")


def test_ask_posts_run_body_and_parses_events(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    sent.response = FakeResponse(payload=[
        {"author": "data_analyst", "actions": {}},  # no content
        {"content": {"parts": [{"functionCall": {"name": "run_sql", "args": {"query": "SELECT 1"}}}]}},
        {"content": {"parts": [{"functionResponse": {"name": "run_sql", "response": {}}}]}},
        {"content": {"parts": [{"text": "hmm", "thought": True}, {"text": "It is 1.\n\nSource: SELECT 1"}]}},
    ])
    answer, calls, intent = ui_client.ask("s1", "what?")
    assert answer == "It is 1.\n\nSource: SELECT 1"
    assert calls == [{"name": "run_sql", "args": {"query": "SELECT 1"}, "response": {}}]
    assert intent is None  # no stateDelta, and the mocked session read has no state
    req = sent[0]
    assert req["url"] == "http://localhost:8000/run" and req["timeout"][1] == ui_client.RUN_TIMEOUT_S
    assert req["json"] == {
        "app_name": "data_agent", "user_id": "user", "session_id": "s1",
        "new_message": {"role": "user", "parts": [{"text": "what?"}]},
    }


def test_parse_events_without_text_returns_empty_answer():
    assert ui_client.parse_events([]) == ("", [], None)
    assert ui_client.parse_events([{"content": {"parts": [{"functionCall": {"name": "x"}}]}}]) == (
        "", [{"name": "x", "args": {}, "response": None}], None,
    )


def test_unreadable_run_response_is_api_error(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    sent.response = FakeResponse(payload=ValueError("not json"))
    with pytest.raises(ui_client.ApiError, match="unreadable"):
        ui_client.ask("s1", "hi")


def test_errors_are_logged_without_bodies(monkeypatch, sent, caplog):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    with caplog.at_level("WARNING", logger="ui_client"):
        sent.response = FakeResponse(404)
        with pytest.raises(ui_client.SessionNotFound):
            ui_client.ask("s1", "secret question text")
        sent.response = FakeResponse(500)
        with pytest.raises(ui_client.ApiError):
            ui_client.create_session("s2", {"upload_b64": "SECRETDATA"})
        sent.response = requests.ConnectionError("down")
        with pytest.raises(ui_client.ApiError):
            ui_client.delete_session("s3")
    text = caplog.text
    assert "session not found: POST /run" in text and "HTTP 500" in text and "unreachable: DELETE" in text
    assert "secret question" not in text and "SECRETDATA" not in text


def test_session_calls_allow_for_a_cold_backend_start(monkeypatch, sent):
    """A cold backend took ~32 s to answer in phase 2.6; the read timeout must exceed that."""
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    ui_client.create_session("s1", {})
    ui_client.delete_session("s1")
    assert all(r["timeout"][1] >= 60 for r in sent)


# --- Phase 3.6: tool responses, intent, citations ---


def _call(name, args=None, id=None):
    return {"content": {"parts": [{"functionCall": {"name": name, "args": args or {}, **({"id": id} if id else {})}}]}}


def _resp(name, response, id=None):
    return {"content": {"parts": [{"functionResponse": {"name": name, "response": response, **({"id": id} if id else {})}}]}}


def test_parse_events_matches_function_responses_by_id():
    events = [
        _call("run_sql", {"query": "a"}, "1"), _call("search_documents", {"query": "b"}, "2"),
        _resp("search_documents", {"status": "success", "passages": []}, "2"),
        _resp("run_sql", {"rows": [[1]]}, "1"),
    ]
    _, calls, _ = ui_client.parse_events(events)
    assert [(c["name"], c["response"]) for c in calls] == [
        ("run_sql", {"rows": [[1]]}), ("search_documents", {"status": "success", "passages": []}),
    ]


def test_parse_events_matches_by_name_in_order_without_ids():
    events = [_call("run_sql", {"query": "a"}), _resp("run_sql", {"n": 1}), _call("run_sql", {"query": "b"}), _resp("run_sql", {"n": 2})]
    _, calls, _ = ui_client.parse_events(events)
    assert [c["response"] for c in calls] == [{"n": 1}, {"n": 2}]


def test_parse_events_unanswered_call_and_stray_response_are_tolerated():
    _, calls, _ = ui_client.parse_events([_call("run_sql"), _resp("search_documents", {"x": 1})])
    assert calls[0]["response"] is None


def test_parse_events_takes_the_intent_from_state_delta():
    events = [{"actions": {"stateDelta": {"intent": "hybrid", "intent_reason": "r"}}}, {"content": {"parts": [{"text": "hi"}]}}]
    assert ui_client.parse_events(events) == ("hi", [], "hybrid")
    assert ui_client.parse_events([{"actions": {"stateDelta": {"intent": 5}}}])[2] is None  # not a string


def test_ask_reads_the_intent_from_the_session_when_events_lack_it(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    replies = iter([
        FakeResponse(payload=[{"content": {"parts": [{"text": "ok"}]}}]),  # POST /run
        FakeResponse(payload={"state": {"intent": "document"}}),  # GET session
    ])
    monkeypatch.setattr(ui_client.requests, "request", lambda method, url, **kw: sent.append({"method": method, "url": url}) or next(replies))
    assert ui_client.ask("s1", "q") == ("ok", [], "document")
    assert sent[1]["method"] == "GET" and sent[1]["url"].endswith("/apps/data_agent/users/user/sessions/s1")


def test_get_intent_never_raises(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    sent.response = FakeResponse(404)
    assert ui_client.get_intent("s1") is None
    sent.response = FakeResponse(payload=ValueError("bad json"))
    assert ui_client.get_intent("s1") is None
    sent.response = FakeResponse(payload={"state": None})
    assert ui_client.get_intent("s1") is None


def test_format_citation():
    p = {"source": "policy.pdf", "section_number": "3.2", "section_path": "3 Rules > 3.2 Express Shipments", "page_start": 1, "page_end": 2}
    assert ui_client.format_citation(p) == "policy.pdf, §3.2 Express Shipments, pp. 1-2"
    assert ui_client.format_citation({**p, "page_end": 1}) == "policy.pdf, §3.2 Express Shipments, p. 1"
    assert ui_client.format_citation({**p, "page_start": None, "page_end": None}) == "policy.pdf, §3.2 Express Shipments"
    no_number = {"source": "notes.txt", "section_number": "", "section_path": "Notes", "page_start": None, "page_end": None}
    assert ui_client.format_citation(no_number) == "notes.txt, Notes"
