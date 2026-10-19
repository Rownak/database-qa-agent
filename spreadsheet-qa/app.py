"""Streamlit front end: upload a CSV/XLSX, then chat with the data (design sections 1 and 7).

Every browser session starts with ``data_agent/sample_data/sales.csv`` loaded. A valid upload
replaces the data and resets the chat; an invalid one shows an error and changes
nothing. Run with ``uv run streamlit run app.py``.

Two modes, chosen by the ``API_URL`` environment variable:

* **Local** (unset): the agent runs in-process through an ADK ``Runner``.
* **API** (set): the agent runs behind the ADK API server (Cloud Run ``data-qa-api``)
  and this app only talks to it through :mod:`ui_client`. The file is still validated
  and previewed locally; it is then sent to the backend inside the session state, which
  builds its own database from it. The UI image contains no ADK, so every ADK import
  is inside a local-mode function.
"""

import asyncio
import base64
import time
import uuid

import streamlit as st

import ui_client
from data_agent import sessions
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


def _api_mode() -> bool:
    """Whether the agent is reached through the API server instead of in-process."""
    return ui_client.api_url() is not None


def _upload_state(upload: dict | None) -> dict:
    """Initial backend session state: the upload, or ``{}`` for the sample data."""
    if upload is None:
        return {}
    return {"upload_name": upload["name"], "upload_b64": upload["b64"]}


def _api_new_session(upload: dict | None) -> None:
    """Creates a backend session and makes it current.

    A fresh id is used each time so a stale ``data.db`` left on the backend under the
    old id is never reused.

    Args:
        upload: ``{"name", "b64"}`` of the uploaded file, or None for the sample.

    Raises:
        ui_client.ApiError: If the backend cannot create the session; the current
            session is left unchanged.
    """
    ss = st.session_state
    new_id = uuid.uuid4().hex
    # The first request after the backend scaled to zero can take about 30 seconds.
    with st.spinner("Connecting to the service (the first request after idle can take a minute)..."):
        ui_client.create_session(new_id, _upload_state(upload))
    old_id, ss.api_sid, ss.upload = ss.get("api_sid"), new_id, upload
    if old_id:
        _api_delete(old_id)


def _api_delete(api_sid: str) -> None:
    """Deletes a backend session, ignoring errors (it may already be gone)."""
    try:
        ui_client.delete_session(api_sid)
    except ui_client.ApiError:
        pass


def _end_agent_session() -> None:
    """Ends the current session and clears the page state.

    Deletes the backend session (API mode) and the local folder, then empties
    ``st.session_state``. The file uploader is given a new key, because a widget keeps
    its file across ``clear()`` and the "fresh" session would otherwise load it again.
    """
    ss = st.session_state
    if ss.get("api_sid"):
        _api_delete(ss.api_sid)
    sessions.end_session(ss.session_dir)
    generation = ss.get("uploader_generation", 0) + 1
    ss.clear()
    ss.uploader_generation = generation


def _reset_adk_session(result: IngestResult, uploaded) -> None:
    """Replaces the agent session with one holding the new upload.

    Args:
        result: The local ingest result (used for the in-process state).
        uploaded: The Streamlit upload, whose bytes are sent to the backend in API mode.

    Raises:
        ui_client.ApiError: In API mode, if the backend rejects the new session.
    """
    ss = st.session_state
    if _api_mode():
        upload = {"name": uploaded.name, "b64": base64.b64encode(uploaded.getvalue()).decode()}
        _api_new_session(upload)
        ss.messages = []
        return

    async def go():
        await ss.adk.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=ss.session_id)
        await ss.adk.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=ss.session_id, state=_adk_state(result)
        )

    asyncio.run(go())
    ss.messages = []


def _start_session() -> None:
    """Creates the folder and the agent session, loaded with the sample data."""
    ss = st.session_state
    ss.session_id = uuid.uuid4().hex
    ss.session_dir = sessions.new_session_dir(ss.session_id)
    result = load_sample(ss.session_dir)
    if isinstance(result, IngestError):  # the bundled sample should always load
        st.error(f"Could not load the sample data: {result.message}")
        st.stop()
    if _api_mode():
        try:
            _api_new_session(None)
        except ui_client.ApiError as exc:
            sessions.end_session(ss.session_dir)
            ss.clear()
            st.error(f"Could not start a session on the service: {exc}")
            st.stop()
    else:
        # Imported here so the UI image does not need ADK (see the module docstring).
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        from data_agent.agent import root_agent

        ss.adk = InMemorySessionService()
        ss.runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=ss.adk)
        asyncio.run(
            ss.adk.create_session(
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=ss.session_id,
                state=_adk_state(result),
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
        _end_agent_session()
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
    try:
        _reset_adk_session(result, uploaded)
    except ui_client.ApiError as exc:
        ss.upload_error = f"Could not send the file to the service: {exc}"
        return  # data, backend session and chat stay as they were
    ss.upload_error = None
    _set_data(result, uploaded.name)


def _ask(question: str) -> tuple[str, list[dict]]:
    """Runs one question through the agent.

    Args:
        question: The user's question.

    Returns:
        ``(answer_text, tool_calls)`` where each call is ``{"name", "args"}``.
    """
    ss = st.session_state
    if _api_mode():
        answer, calls = ui_client.ask(ss.api_sid, question)
        return answer or "I could not produce an answer.", calls

    from google.genai import types

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
        uploaded = st.file_uploader(
            "Replace the data with your own file",
            type=["csv", "xlsx"],
            key=f"uploader_{ss.get('uploader_generation', 0)}",
        )
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
            _end_agent_session()
            st.rerun()

    with right:
        chat_box = st.container(height=600)
        typed = st.chat_input("Ask a question about the data")
        # A question held over from a session reset is answered on the rerun that follows it.
        retrying = typed is None and "pending_question" in ss
        question = typed or ss.pop("pending_question", None)
        if ss.pop("reset_notice", False):
            st.info("The service restarted, so the chat was reset. Your file is still loaded.")
        with chat_box:
            for m in ss.messages:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])
                    _render_calls(m.get("calls", []))
            if question:
                ss.messages.append({"role": "user", "content": question})
                with st.chat_message("user"):
                    st.markdown(question)
                lost_session = False
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            answer, calls = _ask(question)
                        except ui_client.SessionNotFound:
                            # The backend instance was replaced and forgot this session.
                            lost_session = not retrying
                            answer, calls = "Sorry, the session could not be restored.", []
                        except Exception as exc:  # model/network errors should not crash the page
                            answer, calls = f"Sorry, something went wrong: {exc}", []
                    if lost_session:
                        try:
                            _api_new_session(ss.upload)
                        except ui_client.ApiError as exc:
                            lost_session, answer = False, f"Sorry, something went wrong: {exc}"
                    if not lost_session:
                        st.markdown(answer)
                        _render_calls(calls)
                if lost_session:
                    ss.messages, ss.pending_question, ss.reset_notice = [], question, True
                    st.rerun()
                ss.messages.append({"role": "assistant", "content": answer, "calls": calls})


main()
