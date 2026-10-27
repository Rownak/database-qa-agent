"""Streamlit front end: upload data tables and policy documents, then chat (design sections 1 and 7).

Every browser session starts with ``data_agent/sample_data/default/shipments.csv`` and the
sample policy documents loaded. Up to 3 data files (CSV/XLSX) can be loaded, each as its own
table; the first upload replaces the sample, a file with a loaded file's name replaces it in
place, and removing every file brings the sample back. A valid data-file or document change
(add, replace, remove) starts a new agent session and resets the chat; an invalid one shows
an error and changes nothing. At most 3 documents can be loaded. Each answer's expander shows the
classified intent and every tool call. Run with ``uv run streamlit run app.py``.

Two modes, chosen by the ``API_URL`` environment variable:

* **Local** (unset): the agent runs in-process through :class:`data_agent.runner.AgentSession`.
* **API** (set): the agent runs behind the ADK API server (Cloud Run ``data-qa-api``)
  and this app only talks to it through :mod:`ui_client`. Files are still validated
  and previewed locally; they are then sent to the backend inside the session state,
  which builds its own databases from them. The UI image contains no ADK, so every ADK
  import is inside a local-mode function.
"""

import base64
import time
import uuid
from pathlib import Path

import streamlit as st

import ui_client
from data_agent import doc_ingest, sessions
from data_agent.ingest import (
    MAX_TABLES,
    SAMPLE_DOCS_DIR,
    IngestError,
    LoadResult,
    load_files,
    load_sample,
    read_table,
)

MAX_DOCS = 3
DOC_LIMIT_MESSAGE = "Up to 3 documents per session. Remove one first."
TABLE_LIMIT_MESSAGE = f"Up to {MAX_TABLES} data files per session. Remove one first."


@st.cache_resource
def _purge_once() -> bool:
    """Purges leftover session folders once per server process."""
    sessions.purge_all()
    return True


def _adk_state(result: LoadResult) -> dict:
    """Builds the ADK session state the agent and tools read."""
    return {
        "db_path": str(result.db_path),
        "column_map": result.column_map,
        "schema_summary": result.schema_summary,
    }


def _table_entries(result: LoadResult, uploads: list[dict] | None) -> list[dict]:
    """Builds the entries the UI keeps for the loaded tables.

    Args:
        result: The local load result; its tables are in the same order as ``uploads``.
        uploads: ``[{name, b64}]`` of the data files in effect, or None for the sample.

    Returns:
        One ``{name, b64, table, rows, cols, preview, sheet, ignored}`` per table
        (``b64`` is None for the sample). The bytes are kept so the files can be reloaded.
    """
    return [
        {
            "name": t.file_name,
            "b64": uploads[i]["b64"] if uploads else None,
            "table": t.table,
            "rows": t.row_count,
            "cols": t.column_count,
            "preview": t.preview,
            "sheet": t.sheet_name,
            "ignored": t.ignored_sheets,
        }
        for i, t in enumerate(result.tables)
    ]


def _api_mode() -> bool:
    """Whether the agent is reached through the API server instead of in-process."""
    return ui_client.api_url() is not None


def _upload_state(uploads: list[dict] | None) -> dict:
    """Initial backend session state: the data files, or ``{}`` for the sample data."""
    if uploads is None:
        return {}
    return {"uploads": uploads}


def _doc_entry(name: str, data: bytes, prepared: doc_ingest.PreparedDoc) -> dict:
    """Builds the entry the UI keeps for an accepted document (bytes included, for recovery)."""
    return {
        "name": name,
        "b64": base64.b64encode(data).decode(),
        "type": prepared.doc_type,
        "pages": prepared.pages,
        "chunks": len(prepared.chunks),
    }


@st.cache_data
def _sample_doc_entries() -> list[dict]:
    """The bundled sample documents as UI entries ``{name, b64, type, pages, chunks}``."""
    entries = []
    for path in sorted(SAMPLE_DOCS_DIR.iterdir()):
        data = path.read_bytes()
        prepared = doc_ingest.prepare(path.name, data)
        if not isinstance(prepared, doc_ingest.DocError):
            entries.append(_doc_entry(path.name, data, prepared))
    return entries


def _docs_state(entries: list[dict], custom: bool) -> list[dict] | None:
    """The ``docs`` session-state value; ``None`` (key left out) means the sample documents."""
    if not custom:
        return None
    return [{"name": e["name"], "b64": e["b64"]} for e in entries]


def _session_state(uploads: list[dict] | None, entries: list[dict], custom: bool) -> dict:
    """Initial backend session state: the data files, plus ``docs`` once they were changed."""
    state = _upload_state(uploads)
    docs = _docs_state(entries, custom)
    if docs is not None:
        state["docs"] = docs
    return state


def _api_new_session(uploads: list[dict] | None, entries: list[dict], custom: bool) -> None:
    """Creates a backend session and makes it current.

    A fresh id is used each time so a stale ``data.db`` or ``docs.db`` left on the
    backend under the old id is never reused.

    Args:
        uploads: ``[{"name", "b64"}]`` of the uploaded data files, or None for the sample.
        entries: The documents to load (see :func:`_doc_entry`).
        custom: False while the documents are still the unchanged samples; the backend
            then loads them itself and nothing is sent.

    Raises:
        ui_client.ApiError: If the backend cannot create the session; the current
            session is left unchanged.
    """
    ss = st.session_state
    new_id = uuid.uuid4().hex
    # The first request after the backend scaled to zero can take about 30 seconds.
    with st.spinner("Connecting to the service (the first request after idle can take a minute)..."):
        ui_client.create_session(new_id, _session_state(uploads, entries, custom))
    old_id, ss.api_sid, ss.uploads = ss.get("api_sid"), new_id, uploads
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

    Deletes the backend session (API mode) and the local folder (which holds
    ``data.db`` and ``docs.db``), then empties ``st.session_state``. The file uploaders
    are given a new key, because a widget keeps its file across ``clear()`` and the
    "fresh" session would otherwise load it again.
    """
    ss = st.session_state
    if ss.get("api_sid"):
        _api_delete(ss.api_sid)
    sessions.end_session(ss.session_dir)
    generation = ss.get("uploader_generation", 0) + 1
    ss.clear()
    ss.uploader_generation = generation


def _new_agent_session(state: dict, session_id: str):
    """Opens an in-process ``AgentSession`` configured by ``DATA_AGENT_CONFIG`` (defaults if unset).

    Imported here so the UI image does not need ADK (see the module docstring).
    """
    from data_agent.config import load_agent_config
    from data_agent.runner import AgentSession

    return AgentSession(load_agent_config(), state, session_id)


def _local_new_session(table_state: dict, entries: list[dict], custom: bool) -> None:
    """Replaces the in-process ADK session (same id and folder) with one holding the new state.

    Args:
        table_state: ``db_path``, ``column_map`` and ``schema_summary`` of the loaded table.
        entries: The documents to load.
        custom: Whether ``docs`` is put in the state (else the agent loads the samples).

    Side Effects:
        Deletes the folder's old ``docs.db``, so the agent builds a fresh one on the
        next question. ``data.db`` stays.
    """
    ss = st.session_state
    state = dict(table_state)
    docs = _docs_state(entries, custom)
    if docs is not None:
        state["docs"] = docs
    (ss.session_dir / doc_ingest.DOCS_DB_FILENAME).unlink(missing_ok=True)
    ss.agent.close(delete_files=False)  # keep the folder: data.db stays, docs.db is rebuilt
    ss.agent = _new_agent_session(state, ss.session_id)


def _replace_session(
    result: LoadResult | None, uploads: list[dict] | None, entries: list[dict], custom: bool
) -> None:
    """Starts a new agent session for a changed data-file or document list and clears the chat.

    Args:
        result: The new local load result (in-process state), or None to keep the tables.
        uploads: ``[{"name", "b64"}]`` of the data files now in effect (None = sample).
        entries: The documents now in effect.
        custom: Whether the documents differ from the samples.

    Raises:
        ui_client.ApiError: In API mode, if the backend rejects the new session; nothing
            changes in that case.
    """
    ss = st.session_state
    if _api_mode():
        _api_new_session(uploads, entries, custom)
    else:
        table_state = _adk_state(result) if result is not None else ss.table_state
        _local_new_session(table_state, entries, custom)
        ss.table_state, ss.uploads = table_state, uploads
    ss.messages = []


def _start_session() -> None:
    """Creates the folder and the agent session, loaded with the sample table and documents."""
    ss = st.session_state
    ss.session_id = uuid.uuid4().hex
    ss.session_dir = sessions.new_session_dir(ss.session_id)
    result = load_sample(ss.session_dir)
    if isinstance(result, IngestError):  # the bundled sample should always load
        st.error(f"Could not load the sample data: {result.message}")
        st.stop()
    entries = _sample_doc_entries()
    if _api_mode():
        try:
            _api_new_session(None, entries, False)
        except ui_client.ApiError as exc:
            sessions.end_session(ss.session_dir)
            ss.clear()
            st.error(f"Could not start a session on the service: {exc}")
            st.stop()
    else:
        ss.table_state, ss.uploads = _adk_state(result), None
        ss.agent = _new_agent_session(dict(ss.table_state), ss.session_id)
    ss.messages = []
    ss.doc_entries, ss.docs_custom = entries, False
    ss.last_upload = ss.last_doc_upload = None
    ss.upload_error = ss.doc_error = None
    ss.table_entries, ss.tables_custom = _table_entries(result, None), False
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


def _apply_tables(entries: list[dict], custom: bool) -> bool:
    """Makes ``entries`` the session's data files (new agent session, chat cleared).

    The files are reloaded together, so table names always follow the list order.

    Args:
        entries: The complete new list (see :func:`_table_entries`); ignored when ``custom``
            is false.
        custom: False to go back to the bundled sample.

    Returns:
        True if applied. Otherwise the message is stored in ``upload_error``, and the data
        files and chat stay as they were.
    """
    ss = st.session_state
    uploads = [{"name": e["name"], "b64": e["b64"]} for e in entries] if custom else None
    if custom:
        result = load_files([(u["name"], base64.b64decode(u["b64"])) for u in uploads], ss.session_dir)
    else:
        result = load_sample(ss.session_dir)
    if isinstance(result, IngestError):
        ss.upload_error = result.message  # keep data, ADK session and chat as they are
        return False
    try:
        _replace_session(result, uploads, ss.doc_entries, ss.docs_custom)
    except ui_client.ApiError as exc:
        ss.upload_error = f"Could not send the file to the service: {exc}"
        return False  # data, backend session and chat stay as they were
    ss.table_entries, ss.tables_custom, ss.upload_error = _table_entries(result, uploads), custom, None
    return True


def _handle_upload(uploaded) -> None:
    """Validates a newly uploaded data file and adds or replaces it, or records why it was rejected.

    The first upload replaces the sample. A file with the same name (ignoring case) as a
    loaded file replaces it, keeping its place. A rejected file leaves the data files, the
    agent session and the chat unchanged.
    """
    ss = st.session_state
    if uploaded is None:
        ss.last_upload, ss.upload_error = None, None
        return
    if uploaded.file_id == ss.last_upload:  # same file as the previous rerun
        return
    ss.last_upload = uploaded.file_id
    data = uploaded.getvalue()
    parsed = read_table(uploaded.name, data)
    if isinstance(parsed, IngestError):
        ss.upload_error = parsed.message
        return
    entry = {"name": parsed.file_name, "b64": base64.b64encode(data).decode()}
    current = ss.table_entries if ss.tables_custom else []
    same = [i for i, e in enumerate(current) if e["name"].lower() == entry["name"].lower()]
    if same:
        _apply_tables([entry if i == same[0] else e for i, e in enumerate(current)], True)
    elif len(current) >= MAX_TABLES:
        ss.upload_error = TABLE_LIMIT_MESSAGE
    else:
        _apply_tables([*current, entry], True)


def _apply_docs(entries: list[dict]) -> bool:
    """Makes ``entries`` the session's documents (new agent session, chat cleared).

    Args:
        entries: The complete new document list.

    Returns:
        True if applied. On a backend error the message is stored in ``doc_error``, the
        documents and chat stay as they were, and False is returned.
    """
    ss = st.session_state
    try:
        _replace_session(None, ss.get("uploads"), entries, True)
    except ui_client.ApiError as exc:
        ss.doc_error = f"Could not send the documents to the service: {exc}"
        return False
    ss.doc_entries, ss.docs_custom, ss.doc_error = entries, True, None
    return True


def _handle_doc_upload(uploaded) -> None:
    """Validates a newly uploaded document and adds it, or records why it was rejected.

    A file with the same name as a loaded document replaces it (keeping its place in
    the list). A rejected file leaves the documents, the agent session and the chat
    unchanged.
    """
    ss = st.session_state
    if uploaded is None:
        ss.last_doc_upload = None
        return
    if uploaded.file_id == ss.last_doc_upload:  # same file as the previous rerun
        return
    ss.last_doc_upload = uploaded.file_id
    data = uploaded.getvalue()
    prepared = doc_ingest.prepare(uploaded.name, data)
    if isinstance(prepared, doc_ingest.DocError):
        ss.doc_error = prepared.message
        return
    name = Path(uploaded.name).name
    entry = _doc_entry(name, data, prepared)
    replacing = any(e["name"] == name for e in ss.doc_entries)
    if not replacing and len(ss.doc_entries) >= MAX_DOCS:
        ss.doc_error = DOC_LIMIT_MESSAGE
        return
    entries = [entry if e["name"] == name else e for e in ss.doc_entries] if replacing else [*ss.doc_entries, entry]
    if _apply_docs(entries):
        st.rerun()  # the document list is drawn above this uploader, so it needs another pass


def _ask(question: str) -> tuple[str, list[dict], str | None]:
    """Runs one question through the agent.

    Args:
        question: The user's question.

    Returns:
        ``(answer_text, tool_calls, intent)``; each call is ``{"name", "args",
        "response"}`` (see :func:`ui_client.parse_events`). ``intent`` is the classified
        intent, or None if it is unavailable.
    """
    ss = st.session_state
    if _api_mode():
        answer, calls, intent = ui_client.ask(ss.api_sid, question)
        return answer or "I could not produce an answer.", calls, intent

    result = ss.agent.ask(question)
    if result.error:  # AgentSession never raises; show the failure like any other exception
        raise RuntimeError(result.error)
    return result.answer or "I could not produce an answer.", result.calls, result.intent


def _render_calls(calls: list[dict], intent: str | None = None) -> None:
    """Shows the classified intent and the tool calls behind an answer.

    ``run_sql`` is shown as its SQL; ``search_documents`` as its query, filters and the
    citations of the passages it returned.
    """
    if not calls and not intent:
        return
    with st.expander("How this was answered"):
        if intent:
            st.markdown(f"**Intent:** {intent}")
        for c in calls:
            st.markdown(f"**{c['name']}**")
            args, response = c.get("args") or {}, c.get("response") or {}
            if c["name"] == "run_sql" and args.get("query"):
                st.code(args["query"], language="sql")
                if response.get("status") == "error":
                    st.caption(f"Error: {response.get('error_message', 'unknown')}")
            elif c["name"] == "search_documents":
                _render_search(args, response)
            elif args:
                st.json(args)


def _render_search(args: dict, response: dict) -> None:
    """Shows one ``search_documents`` call: its query, filters and returned citations."""
    st.markdown(f"Query: `{args.get('query', '')}`")
    filters = {k: args[k] for k in ("source", "section") if args.get(k)}
    if filters:
        st.caption("Filters: " + ", ".join(f"{k} = {v}" for k, v in filters.items()))
    if response.get("status") == "error":
        st.caption(f"Error: {response.get('error_message', 'unknown')}")
    passages = response.get("passages") or []
    if passages:
        st.markdown("Passages returned:\n" + "\n".join(f"- {ui_client.format_citation(p)}" for p in passages))
    elif response.get("status") == "success":
        st.caption("No passages returned.")
    if response.get("note"):
        st.caption(response["note"])


def _render_documents() -> None:
    """Lists the loaded documents, each with a Remove button that is handled right away."""
    ss = st.session_state
    st.subheader("Documents")
    if not ss.doc_entries:
        st.caption("No documents loaded. Policy questions cannot be answered.")
    for e in ss.doc_entries:
        size = f"{e['pages']} page{'s' if e['pages'] != 1 else ''}, " if e.get("type") == "pdf" else ""
        cols = st.columns([4, 1])
        cols[0].markdown(f"**{e['name']}**  \n{size}{e['chunks']} chunks")
        if cols[1].button("Remove", key=f"remove_{e['name']}"):
            if _apply_docs([x for x in ss.doc_entries if x["name"] != e["name"]]):
                st.rerun()


def _render_tables() -> None:
    """Lists the loaded data files (table name and size, Remove for uploads) and previews one."""
    ss = st.session_state
    st.subheader("Data files")
    for e in ss.table_entries:
        cols = st.columns([4, 1])
        sample = "" if ss.tables_custom else " (sample)"
        cols[0].markdown(f"**{e['name']}**{sample}  \ntable `{e['table']}` · {e['rows']} rows × {e['cols']} columns")
        if ss.tables_custom and cols[1].button("Remove", key=f"remove_table_{e['name']}"):
            if _apply_tables([x for x in ss.table_entries if x["name"] != e["name"]], bool(len(ss.table_entries) > 1)):
                st.rerun()
        if e["ignored"]:
            st.warning(f"{e['name']}: only the first sheet was loaded. Ignored sheets: " + ", ".join(e["ignored"]))
    tables = [e["table"] for e in ss.table_entries]
    shown = st.selectbox("Preview table", tables, key="preview_table") if len(tables) > 1 else tables[0]
    preview = next((e["preview"] for e in ss.table_entries if e["table"] == shown), None)
    if preview is not None:
        st.dataframe(preview, width="stretch")


def main() -> None:
    """Renders the page: data, documents and uploads on the left, chat on the right."""
    st.set_page_config(page_title="Database & Document Q&A", page_icon="📊", layout="wide")
    st.title("Database & Document Q&A")
    _purge_once()
    _ensure_session()
    ss = st.session_state
    left, right = st.columns([1, 2])

    # The left side runs first so an upload resets the chat before the right side draws it.
    with left:
        uploaded = st.file_uploader(
            f"Add or replace a data file (CSV or XLSX, up to {MAX_TABLES})",
            type=["csv", "xlsx"],
            key=f"uploader_{ss.get('uploader_generation', 0)}",
        )
        _handle_upload(uploaded)
        if ss.upload_error:
            st.error(ss.upload_error)

        _render_tables()

        _render_documents()
        doc_file = st.file_uploader(
            "Add a document (PDF, TXT or Markdown)",
            type=["pdf", "txt", "md"],
            key=f"doc_uploader_{ss.get('uploader_generation', 0)}",
        )
        _handle_doc_upload(doc_file)
        if ss.doc_error:
            st.error(ss.doc_error)

        if st.button("End session"):
            _end_agent_session()
            st.rerun()

    with right:
        chat_box = st.container(height=600)
        typed = st.chat_input("Ask about your data or the policy documents")
        # A question held over from a session reset is answered on the rerun that follows it.
        retrying = typed is None and "pending_question" in ss
        question = typed or ss.pop("pending_question", None)
        if ss.pop("reset_notice", False):
            st.info("The service restarted, so the chat was reset. Your files are still loaded.")
        with chat_box:
            for m in ss.messages:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])
                    _render_calls(m.get("calls", []), m.get("intent"))
            if question:
                ss.messages.append({"role": "user", "content": question})
                with st.chat_message("user"):
                    st.markdown(question)
                lost_session = False
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            answer, calls, intent = _ask(question)
                        except ui_client.SessionNotFound:
                            # The backend instance was replaced and forgot this session.
                            lost_session = not retrying
                            answer, calls, intent = "Sorry, the session could not be restored.", [], None
                        except Exception as exc:  # model/network errors should not crash the page
                            answer, calls, intent = f"Sorry, something went wrong: {exc}", [], None
                    if lost_session:
                        try:
                            _api_new_session(ss.uploads, ss.doc_entries, ss.docs_custom)
                        except ui_client.ApiError as exc:
                            lost_session, answer = False, f"Sorry, something went wrong: {exc}"
                    if not lost_session:
                        st.markdown(answer)
                        _render_calls(calls, intent)
                if lost_session:
                    ss.messages, ss.pending_question, ss.reset_notice = [], question, True
                    st.rerun()
                ss.messages.append({"role": "assistant", "content": answer, "calls": calls, "intent": intent})


main()
