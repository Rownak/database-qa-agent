"""The shared in-process entry point to the agent, used by the Streamlit app and the evaluation.

:class:`AgentSession` wraps one ADK ``Runner`` with its own ``InMemorySessionService``, so
a caller builds the agent from an :class:`~data_agent.config.AgentConfig`, asks questions
and gets a :class:`~data_agent.events.TurnResult` back, without touching ADK. It runs the
same ``ensure_data``, tools and instruction as the deployed agent. :func:`files_state`
builds the session state from files, the way the UI does from uploads.

This module imports ADK, so the UI image (which has none) must not import it at module
level; the app imports it only in local mode.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from data_agent import sessions
from data_agent.agent import build_agent
from data_agent.config import AgentConfig
from data_agent.events import TurnResult, parse_events, turn_timing

APP_NAME = "sheet_qa"
USER_ID = "user"


def files_state(tables: list[Path] | None, docs: list[Path] | None) -> dict:
    """Builds the session state that holds data files and documents given as files.

    Args:
        tables: CSV or XLSX files (up to three), or ``None`` for the bundled sample table.
        docs: Document files, ``None`` for the bundled sample documents, or ``[]`` for no
            documents.

    Returns:
        A dict for ``AgentSession``: ``uploads`` (``[{name, b64}]``) when tables are given,
        and ``docs`` (``[{name, b64}]``) when ``docs`` is not ``None``. The agent builds
        ``data.db`` and ``docs.db`` from it on the first turn.
    """
    state: dict = {}
    if tables is not None:
        state["uploads"] = [
            {"name": Path(p).name, "b64": base64.b64encode(Path(p).read_bytes()).decode()} for p in tables
        ]
    if docs is not None:
        state["docs"] = [
            {"name": Path(p).name, "b64": base64.b64encode(Path(p).read_bytes()).decode()} for p in docs
        ]
    return state


def _state_delta_value(events: list[dict], key: str):
    """Returns the last ``stateDelta[key]`` found in the events, or ``None`` if no event set it."""
    value = None
    for event in events:
        delta = (event.get("actions") or {}).get("stateDelta") or {}
        if key in delta:
            value = delta[key]
    return value


class AgentSession:
    """One isolated in-process agent session: a ``Runner`` plus an in-memory ADK session.

    Attributes:
        session_id: Id of the ADK session; also names the session folder.
        adk: The ``InMemorySessionService`` that holds the session state.
        runner: The ADK ``Runner`` for ``build_agent(cfg)``.
    """

    def __init__(self, cfg: AgentConfig, state: dict, session_id: str | None = None) -> None:
        """Creates the agent and the ADK session.

        Args:
            cfg: Agent settings.
            state: Initial session state (see :func:`files_state`, or the UI's table state).
            session_id: Id to use; a new random id when ``None``.

        Side Effects:
            Creates the in-memory session. The databases are built later, on the first turn.
        """
        self.cfg = cfg
        self.session_id = session_id or uuid.uuid4().hex
        self._state = state
        self.adk = InMemorySessionService()
        self.runner = Runner(agent=build_agent(cfg), app_name=APP_NAME, session_service=self.adk)
        asyncio.run(
            self.adk.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=self.session_id, state=dict(state))
        )

    def ask(self, question: str) -> TurnResult:
        """Asks one question (sync wrapper around :meth:`ask_async`); never raises."""
        try:
            return asyncio.run(self.ask_async(question))
        except Exception as exc:  # e.g. called from inside a running event loop
            return TurnResult("", [], None, 0.0, f"{type(exc).__name__}: {exc}")

    async def ask_async(self, question: str) -> TurnResult:
        """Runs one question through the agent.

        Args:
            question: The user's message.

        Returns:
            A :class:`TurnResult`. An exception during the run becomes ``error``; the
            answer, calls and intent collected before it are kept.
        """
        message = types.Content(role="user", parts=[types.Part(text=question)])
        events: list[dict] = []
        arrivals: list[float] = []  # seconds since the start at which each event arrived (latency_trace only)
        trace = self.cfg.latency_trace
        # perf_counter, not monotonic: on Windows monotonic ticks every ~15.6 ms and would round
        # short stages (a SQL call, a search) to 0 in the trace.
        started = time.perf_counter()
        error: str | None = None
        intent_from_state: str | None = None
        setup: list[dict] | None = None
        try:
            async for ev in self.runner.run_async(user_id=USER_ID, session_id=self.session_id, new_message=message):
                # The same JSON shape the API server sends, so one parser serves both paths.
                events.append(ev.model_dump(mode="json", by_alias=True, exclude_none=True))
                if trace:
                    arrivals.append(time.perf_counter() - started)
            if trace:
                setup = _state_delta_value(events, "turn_timing")
            need_intent = not any("intent" in ((e.get("actions") or {}).get("stateDelta") or {}) for e in events)
            if need_intent or (trace and setup is None):
                # No state delta carried the value: read it from the session state instead.
                session = await self.adk.get_session(
                    app_name=APP_NAME, user_id=USER_ID, session_id=self.session_id
                )
                if need_intent:
                    value = session.state.get("intent") if session is not None else None
                    intent_from_state = value if isinstance(value, str) else None
                if trace and setup is None:
                    setup = session.state.get("turn_timing") if session is not None else None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        parsed = parse_events(events, time.perf_counter() - started)
        timing = turn_timing(events, arrivals, setup or [], parsed.latency_s) if trace else None
        return TurnResult(
            parsed.answer, parsed.calls, parsed.intent or intent_from_state, parsed.latency_s, error, timing
        )

    def close(self, delete_files: bool = True) -> None:
        """Deletes the ADK session and, optionally, the session folder.

        Args:
            delete_files: Also remove the folder with ``data.db`` and ``docs.db``. The app
                passes ``False`` when it replaces the session and keeps the same folder.

        Side Effects:
            Never raises; a failure to delete is ignored.
        """
        try:
            asyncio.run(self.adk.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=self.session_id))
        except Exception:
            pass
        if delete_files:
            db_path = self._state.get("db_path")
            sessions.end_session(Path(db_path).parent if db_path else sessions.ROOT / self.session_id)
