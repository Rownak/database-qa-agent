"""HTTP client the Streamlit UI uses to talk to the ADK API server (Sprint 2, phase 2.3).

In the deployed setup the agent runs as a separate, private Cloud Run service
(``data-qa-api``). This module wraps the ADK endpoints the UI needs: create a
session, ask a question (``POST /run``), read a session's state and delete the session. The base URL comes from
the ``API_URL`` environment variable; when it is unset the UI runs the agent in-process
instead and never imports this module's network functions.

For any host other than localhost the client sends a Google-signed ID token
(``Authorization: Bearer ...``) whose audience is ``API_URL``, which Cloud Run checks
because the backend is deployed with ``--no-allow-unauthenticated``. The module has no
Streamlit dependency, so it can be tested directly.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests

from data_agent.events import TurnResult, parse_events as _parse_events  # noqa: F401  (TurnResult re-exported)

APP_NAME = "data_agent"  # the agent folder name, which ADK uses as the app name
USER_ID = "user"
CONNECT_TIMEOUT_S = 10
# A cold backend instance (scale-to-zero) took ~32 s to answer its first request in the
# phase 2.6 measurements, so session calls get a generous read timeout. A timeout here
# would make the UI fail at startup with "Could not start a session".
SESSION_TIMEOUT_S = 90
# Slightly above the backend's 120 s request timeout so its error arrives first.
RUN_TIMEOUT_S = 130
# ID tokens last about an hour; refresh well before that.
TOKEN_TTL_S = 50 * 60
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# Warnings go to stderr (Cloud Logging on Cloud Run). Only methods, paths and status
# codes are logged, never request bodies, so uploaded data and tokens stay out of logs.
logger = logging.getLogger(__name__)

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
            logger.warning("could not get an ID token for %s: %s", audience, exc.__class__.__name__)
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
        logger.warning("backend unreachable: %s %s (%s)", method, path, exc.__class__.__name__)
        raise ApiError(f"Could not reach the service: {exc}") from exc
    if resp.status_code == 404:
        logger.warning("backend session not found: %s %s", method, path)
        raise SessionNotFound("The session was not found on the service.")
    if resp.status_code >= 400:
        logger.warning("backend error: %s %s -> HTTP %s", method, path, resp.status_code)
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
            upload this is ``{"upload_name": ..., "upload_b64": ...}``, and changed
            documents add ``"docs": [{"name": ..., "b64": ...}]``; ``{}`` means "use the
            sample table and the sample documents".

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


def ask(session_id: str, question: str) -> tuple[str, list[dict], str | None]:
    """Sends a question to the agent and returns its answer, tool calls and intent.

    Args:
        session_id: Id of an existing backend session.
        question: The user's question.

    Returns:
        ``(answer_text, tool_calls, intent)``. See :func:`parse_events`. The answer is
        empty if the agent produced no text. If the events carry no intent, it is read
        from the session state; it is ``None`` when that fails too.

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
        answer, calls, intent = parse_events(resp.json())
    except ValueError as exc:
        raise ApiError("The service returned an unreadable response.") from exc
    return answer, calls, intent or get_intent(session_id)


def get_intent(session_id: str) -> str | None:
    """Reads the classified intent from a backend session's state.

    Used when the ``/run`` events did not include it. Never raises: the intent is only
    shown to the user, so any failure just means it is not displayed.

    Args:
        session_id: Id of an existing backend session.

    Returns:
        The ``intent`` state value, or ``None`` if it is missing or the call fails.
    """
    try:
        state = _request("GET", _session_path(session_id), timeout=SESSION_TIMEOUT_S).json().get("state")
        value = (state or {}).get("intent")
    except (ApiError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def parse_events(events: list[dict]) -> tuple[str, list[dict], str | None]:
    """Extracts ``(answer_text, tool_calls, intent)`` from ``/run`` events.

    A tuple view of :func:`data_agent.events.parse_events`, which holds the parsing and
    returns the full :class:`~data_agent.events.TurnResult`.

    Args:
        events: The JSON list returned by ``POST /run``.

    Returns:
        ``(answer_text, tool_calls, intent)``; see :class:`~data_agent.events.TurnResult`.
    """
    result = _parse_events(events)
    return result.answer, result.calls, result.intent


def format_citation(passage: dict) -> str:
    """Formats a ``search_documents`` passage like the agent's citations.

    Args:
        passage: One entry of the tool's ``passages`` list.

    Returns:
        ``source, §number title, p. N`` (the page only for PDFs; ``pp. a-b`` for a range).
    """
    number = passage.get("section_number") or ""
    title = str(passage.get("section_path") or "").split(" > ")[-1]
    if number and title.startswith(number):  # headings already begin with their number
        title = title[len(number):].strip()
    section = f"§{number} {title}".strip() if number else title
    start, end = passage.get("page_start"), passage.get("page_end")
    if start is None:
        page = ""
    elif end is None or end == start:
        page = f"p. {start}"
    else:
        page = f"pp. {start}-{end}"
    return ", ".join(x for x in (passage.get("source"), section, page) if x)
