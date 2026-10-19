"""Streamlit front end: upload a CSV/XLSX, then chat with the data (design sections 1 and 7).

Every browser session starts with ``sample_data/sales.csv`` loaded. A valid upload
replaces the data and resets the chat; an invalid one shows an error and changes
nothing. Run with ``uv run streamlit run app.py``.
"""

import asyncio
import time
import uuid

import streamlit as st
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from data_agent import sessions
from data_agent.agent import root_agent
from data_agent.ingest import SAMPLE_FILE, IngestError, IngestResult, load_file, load_sample

APP_NAME = "sheet_qa"
USER_ID = "user"


@st.cache_resource
def _purge_once() -> bool:
    """Purges leftover session folders once per server process."""
    sessions.purge_all()
    return True


def _adk_state(result: IngestResult) -> dict:
    """Builds the ADK session state the agent and tools read."""
    return {
        "db_path": str(result.db_path),
        "column_map": result.column_map,
        "schema_summary": result.schema_summary,
    }


def _set_data(result: IngestResult, name: str) -> None:
    """Remembers what is loaded so the UI can show it on every rerun."""
    st.session_state.data_info = {
        "name": name,
        "rows": result.row_count,
        "cols": result.column_count,
        "preview": result.preview,
        "ignored": result.ignored_sheets,
        "sheet": result.sheet_name,
    }


def _reset_adk_session(result: IngestResult) -> None:
    """Replaces the ADK session with one holding ``result``'s state."""
    ss = st.session_state

    async def go():
        await ss.adk.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=ss.session_id)
        await ss.adk.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=ss.session_id, state=_adk_state(result)
        )

    asyncio.run(go())
    ss.messages = []


def _start_session() -> None:
    """Creates the folder, runner and ADK session, loaded with the sample data."""
    ss = st.session_state
    ss.session_id = uuid.uuid4().hex
    ss.session_dir = sessions.new_session_dir(ss.session_id)
    result = load_sample(ss.session_dir)
    if isinstance(result, IngestError):  # the bundled sample should always load
        st.error(f"Could not load the sample data: {result.message}")
        st.stop()
    ss.adk = InMemorySessionService()
    ss.runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=ss.adk)
    asyncio.run(
        ss.adk.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=ss.session_id, state=_adk_state(result)
        )
    )
    ss.messages = []
    ss.last_upload = None
    ss.upload_error = None
    _set_data(result, SAMPLE_FILE.name + " (sample)")
    ss.last_active = time.time()


def _ensure_session() -> None:
    """Starts a session on first run, expires an idle one, and tidies old folders."""
    ss = st.session_state
    if "session_id" in ss and time.time() - ss.last_active > sessions.MAX_AGE_SECONDS:
        sessions.end_session(ss.session_dir)
        ss.clear()
        st.info("Your previous session expired after 30 minutes of inactivity. Starting fresh.")
    if "session_id" not in ss:
        _start_session()
        return
    ss.last_active = time.time()
    sessions.touch(ss.session_dir)
    sessions.sweep_stale()


def _handle_upload(uploaded) -> None:
    """Loads a newly uploaded file, or records why it was rejected."""
    ss = st.session_state
    if uploaded is None:
        ss.last_upload, ss.upload_error = None, None
        return
    if uploaded.file_id == ss.last_upload:  # same file as the previous rerun
        return
    ss.last_upload = uploaded.file_id
    result = load_file(uploaded.name, uploaded.getvalue(), ss.session_dir)
    if isinstance(result, IngestError):
        ss.upload_error = result.message  # keep data, ADK session and chat as they are
        return
    ss.upload_error = None
    _reset_adk_session(result)
    _set_data(result, uploaded.name)


def _ask(question: str) -> tuple[str, list[dict]]:
    """Runs one question through the agent.

    Args:
        question: The user's question.

    Returns:
        ``(answer_text, tool_calls)`` where each call is ``{"name", "args"}``.
    """
    ss = st.session_state
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls: list[dict] = []
    answer = ""

    async def go():
        nonlocal answer
        async for ev in ss.runner.run_async(
            user_id=USER_ID, session_id=ss.session_id, new_message=message
        ):
            calls.extend({"name": c.name, "args": dict(c.args or {})} for c in ev.get_function_calls())
            if ev.is_final_response() and ev.content and ev.content.parts:
                answer = "".join(p.text or "" for p in ev.content.parts)

    asyncio.run(go())
    return answer or "I could not produce an answer.", calls


def _render_calls(calls: list[dict]) -> None:
    """Shows the tool calls (including SQL) behind an answer."""
    if not calls:
        return
    with st.expander("How this was answered"):
        for c in calls:
            st.markdown(f"**{c['name']}**")
            sql = c["args"].get("query")
            if sql:
                st.code(sql, language="sql")
            elif c["args"]:
                st.json(c["args"])


def main() -> None:
    """Renders the page: data panel and upload on the left, chat on the right."""
    st.set_page_config(page_title="Spreadsheet Q&A", page_icon="📊", layout="wide")
    st.title("Spreadsheet Q&A")
    _purge_once()
    _ensure_session()
    ss = st.session_state
    left, right = st.columns([1, 2])

    # The left side runs first so an upload resets the chat before the right side draws it.
    with left:
        uploaded = st.file_uploader("Replace the data with your own file", type=["csv", "xlsx"])
        _handle_upload(uploaded)
        if ss.upload_error:
            st.error(ss.upload_error)

        info = ss.data_info
        sheet = f" (sheet: {info['sheet']})" if info["sheet"] else ""
        st.subheader(f"Current data: {info['name']}{sheet}")
        st.caption(f"{info['rows']} rows × {info['cols']} columns")
        if info["ignored"]:
            st.warning("Only the first sheet was loaded. Ignored sheets: " + ", ".join(info["ignored"]))
        st.dataframe(info["preview"], width="stretch")

        if st.button("End session"):
            sessions.end_session(ss.session_dir)
            ss.clear()
            st.rerun()

    with right:
        chat_box = st.container(height=600)
        question = st.chat_input("Ask a question about the data")
        with chat_box:
            for m in ss.messages:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])
                    _render_calls(m.get("calls", []))
            if question:
                ss.messages.append({"role": "user", "content": question})
                with st.chat_message("user"):
                    st.markdown(question)
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            answer, calls = _ask(question)
                        except Exception as exc:  # model/network errors should not crash the page
                            answer, calls = f"Sorry, something went wrong: {exc}", []
                    st.markdown(answer)
                    _render_calls(calls)
                ss.messages.append({"role": "assistant", "content": answer, "calls": calls})


main()
