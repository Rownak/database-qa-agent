"""HTTP client the Streamlit UI uses to talk to the ADK API server (Sprint 2, phase 2.3).

In the deployed setup the agent runs as a separate, private Cloud Run service
(``data-qa-api``). This module wraps the three ADK endpoints the UI needs: create a
session, ask a question (``POST /run``) and delete the session. The base URL comes from
the ``API_URL`` environment variable; when it is unset the UI runs the agent in-process
instead and never imports this module's network functions.

For any host other than localhost the client sends a Google-signed ID token
(``Authorization: Bearer ...``) whose audience is ``API_URL``, which Cloud Run checks
because the backend is deployed with ``--no-allow-unauthenticated``. The module has no
Streamlit dependency, so it can be tested directly.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import requests

APP_NAME = "data_agent"  # the agent folder name, which ADK uses as the app name
USER_ID = "user"
CONNECT_TIMEOUT_S = 10
SESSION_TIMEOUT_S = 30
# Slightly above the backend's 120 s request timeout so its error arrives first.
RUN_TIMEOUT_S = 130
# ID tokens last about an hour; refresh well before that.
TOKEN_TTL_S = 50 * 60
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}  # audience -> (token, fetched_at)


class ApiError(Exception):
    """The backend could not be reached or returned an error."""


class SessionNotFound(ApiError):
    """The backend does not know the session (e.g. its instance was replaced)."""


def api_url() -> str | None:
    """Returns the backend base URL from ``API_URL`` without a trailing slash, or None."""
    return os.environ.get("API_URL", "").strip().rstrip("/") or None


def _is_local(url: str) -> bool:
    """Whether ``url`` points at this machine (no ID token is needed there)."""
    return urlparse(url).hostname in _LOCAL_HOSTS


def _id_token(audience: str) -> str:
    """Returns a cached ID token for ``audience``, fetching a new one when it is old.

    Args:
        audience: The backend URL the token is for.

    Returns:
        The signed ID token.

    Raises:
        ApiError: If no token can be obtained (e.g. no service account credentials).
    """
    with _token_lock:
        cached = _token_cache.get(audience)
        if cached and time.monotonic() - cached[1] < TOKEN_TTL_S:
            return cached[0]
        try:
            # Imported here so local mode and the tests do not need Google credentials.
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token

            token = id_token.fetch_id_token(Request(), audience)
        except Exception as exc:
            raise ApiError(f"Could not get an ID token for the service: {exc}") from exc
        _token_cache[audience] = (token, time.monotonic())
        return token


def _request(method: str, path: str, *, json: dict | None = None, timeout: float) -> requests.Response:
    """Sends one authenticated request to the backend.

    Args:
        method: HTTP method.
        path: Path starting with ``/``.
        json: Optional JSON body.
        timeout: Read timeout in seconds.

    Returns:
        The response, already checked for errors.

    Raises:
        SessionNotFound: On a 404.
        ApiError: On any other HTTP error or network failure.
    """
    base = api_url()
    if base is None:
        raise ApiError("API_URL is not set.")
    headers = {} if _is_local(base) else {"Authorization": f"Bearer {_id_token(base)}"}
    try:
        resp = requests.request(
            method, base + path, json=json, headers=headers, timeout=(CONNECT_TIMEOUT_S, timeout)
        )
    except requests.RequestException as exc:
        raise ApiError(f"Could not reach the service: {exc}") from exc
    if resp.status_code == 404:
        raise SessionNotFound("The session was not found on the service.")
    if resp.status_code >= 400:
        raise ApiError(f"The service returned HTTP {resp.status_code}.")
    return resp


def _session_path(session_id: str) -> str:
    """Path of one ADK session resource."""
    return f"/apps/{APP_NAME}/users/{USER_ID}/sessions/{session_id}"


def create_session(session_id: str, state: dict) -> None:
    """Creates a backend session with the given initial state.

    Args:
        session_id: Id for the new session.
        state: Initial state. ADK takes the request body as the state itself, so for an
            upload this is ``{"upload_name": ..., "upload_b64": ...}``; ``{}`` means
            "use the sample data".

    Raises:
        ApiError: If the backend rejects the request or cannot be reached.
    """
    _request("POST", _session_path(session_id), json=state, timeout=SESSION_TIMEOUT_S)


def delete_session(session_id: str) -> None:
    """Deletes a backend session.

    Args:
        session_id: Id of the session to delete.

    Raises:
        SessionNotFound: If it no longer exists.
        ApiError: If the backend cannot be reached or rejects the request.
    """
    _request("DELETE", _session_path(session_id), timeout=SESSION_TIMEOUT_S)


def ask(session_id: str, question: str) -> tuple[str, list[dict]]:
    """Sends a question to the agent and returns its answer and the tools it called.

    Args:
        session_id: Id of an existing backend session.
        question: The user's question.

    Returns:
        ``(answer_text, tool_calls)`` where each call is ``{"name", "args"}``. The
        answer is empty if the agent produced no text.

    Raises:
        SessionNotFound: If the backend has no such session (HTTP 404).
        ApiError: On any other failure.
    """
    body = {
        "app_name": APP_NAME,
        "user_id": USER_ID,
        "session_id": session_id,
        "new_message": {"role": "user", "parts": [{"text": question}]},
    }
    resp = _request("POST", "/run", json=body, timeout=RUN_TIMEOUT_S)
    try:
        return parse_events(resp.json())
    except ValueError as exc:
        raise ApiError("The service returned an unreadable response.") from exc


def parse_events(events: list[dict]) -> tuple[str, list[dict]]:
    """Extracts the final answer and the tool calls from ``/run`` events.

    Args:
        events: The JSON list returned by ``POST /run``.

    Returns:
        ``(answer_text, tool_calls)``. The answer is the text of the last event that
        has any (model "thought" parts are skipped).
    """
    calls: list[dict] = []
    answer = ""
    for event in events:
        parts = (event.get("content") or {}).get("parts") or []
        for part in parts:
            call = part.get("functionCall")
            if call:
                calls.append({"name": call.get("name", ""), "args": dict(call.get("args") or {})})
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought"))
        if text:
            answer = text
    return answer, calls
