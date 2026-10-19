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
    answer, calls = ui_client.ask("s1", "what?")
    assert answer == "It is 1.\n\nSource: SELECT 1"
    assert calls == [{"name": "run_sql", "args": {"query": "SELECT 1"}}]
    req = sent[0]
    assert req["url"] == "http://localhost:8000/run" and req["timeout"][1] == ui_client.RUN_TIMEOUT_S
    assert req["json"] == {
        "app_name": "data_agent", "user_id": "user", "session_id": "s1",
        "new_message": {"role": "user", "parts": [{"text": "what?"}]},
    }


def test_parse_events_without_text_returns_empty_answer():
    assert ui_client.parse_events([]) == ("", [])
    assert ui_client.parse_events([{"content": {"parts": [{"functionCall": {"name": "x"}}]}}]) == (
        "", [{"name": "x", "args": {}}],
    )


def test_unreadable_run_response_is_api_error(monkeypatch, sent):
    monkeypatch.setenv("API_URL", "http://localhost:8000")
    sent.response = FakeResponse(payload=ValueError("not json"))
    with pytest.raises(ui_client.ApiError, match="unreadable"):
        ui_client.ask("s1", "hi")
