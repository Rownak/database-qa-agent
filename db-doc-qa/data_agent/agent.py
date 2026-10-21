"""The ADK agent that answers questions about the uploaded table.

Defines ``root_agent``, a single ``LlmAgent`` with no sub-agents (design section 4).
It sits between the Streamlit app, which supplies the session state (``db_path``,
``column_map``, ``schema_summary``) and runs it through an ADK ``Runner``, and the
read-only functions in :mod:`data_agent.tools`, which are its only way to touch data.
"""

import base64
import binascii
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext

from data_agent import intent, sessions
from data_agent.ingest import IngestError, IngestResult, load_file, load_sample
from data_agent.logs import get_logger
from data_agent.tools import (
    get_column_stats,
    get_sample_rows,
    get_table_info,
    run_sql,
)

# Loaded here so the key is available however the agent is started (Streamlit, scripts).
load_dotenv()

AGENT_MODEL = "gemini-3.5-flash-lite"

# ADK replaces {schema_summary} with the value in session state, and {load_error?} with
# the upload-failure notice (or nothing), so this text must not contain any other curly
# braces. When a notice is present, rule 10 tells the agent to relay it.
INSTRUCTION = """\
You answer questions about one SQLite table named "data", built from a file the user uploaded.
Its columns (cleaned name, type, original name) are:
{schema_summary}
{load_error?}

Rules:
1. Prefer a dedicated tool when one fits: get_table_info for row counts, column counts and the schema; get_sample_rows for example rows; get_column_stats for a summary of a single column.
2. For anything else (filters, grouping, ranking, math across columns), write ONE SQLite SELECT query and call run_sql.
3. Use only the cleaned column names listed above, and wrap each in double quotes in SQL.
4. Base every number and fact on a tool result. Never estimate, guess or invent values.
5. If the columns needed to answer do not exist, say what is missing and list the columns that are available. Do not give a number in that case.
6. If run_sql returns an error, fix the query and try once more. If it fails again, explain the problem plainly.
7. Never try to change data. Refuse requests to delete, update, insert or alter anything, and say the data is read-only.
8. If run_sql reports truncated results, say that only the first rows are shown.
9. End EVERY answer, including refusals and explanations of missing data, with a final line starting with "Source:" that names the tool used or shows the SQL query. If no data was queried, write "Source: none (no data queried)".
10. If a line above says the user's file could not be loaded, begin your first answer by telling the user that, and that the sample data is being used instead.
"""

_STATE_KEYS = ("db_path", "column_map", "schema_summary")

logger = get_logger(__name__)


def ensure_data(callback_context: CallbackContext) -> None:
    """Makes sure this session's ``data.db`` exists on this instance (before-agent callback).

    The Streamlit UI (or ``adk web``) creates the ADK session; the database itself is
    built here, on whichever instance serves the request. Cloud Run's ``/tmp`` is
    in-memory and lost when an instance is replaced, so the file is rebuilt from the
    session state whenever it is missing: from the base64 ``upload_b64`` /
    ``upload_name`` the UI stored, or from the bundled sample when there is no upload.

    Args:
        callback_context: ADK callback context; its ``state`` is read and updated and
            its ``session.id`` names the session folder.

    Returns:
        None, so the agent run continues normally.

    Side Effects:
        May create ``<tmp>/adk_sheet_qa/<session_id>/data.db`` and set ``db_path``,
        ``column_map`` and ``schema_summary`` in the state. If the upload cannot be
        loaded, falls back to the sample and sets ``load_error`` so the agent can
        explain it. Always touches the session folder and deletes folders idle for
        longer than ``sessions.MAX_AGE_SECONDS``.
    """
    state = callback_context.state
    session_id = callback_context.session.id
    db_path = state.get("db_path")

    if all(state.get(k) is not None for k in _STATE_KEYS) and Path(db_path).is_file():
        sessions.touch(Path(db_path).parent)
    else:
        _build_session_data(state, session_id)
    _classify_turn(callback_context)
    _sweep()
    return None


def _classify_turn(callback_context: CallbackContext) -> None:
    """Classifies the current user message and stores ``intent`` / ``intent_reason``.

    Runs after the databases are ready so the classifier sees the schema and document
    summary. Never raises: :func:`intent.classify` falls back to ``hybrid``.

    Args:
        callback_context: ADK callback context; ``user_content`` holds the message.

    Side Effects:
        Updates the state and logs the intent (never the question).
    """
    content = getattr(callback_context, "user_content", None)
    parts = getattr(content, "parts", None) or []
    question = " ".join(p.text for p in parts if getattr(p, "text", None)) or None
    state = callback_context.state
    result = intent.classify(question, state.get("schema_summary"), state.get("docs_summary"))
    state["intent"], state["intent_reason"] = result.intent, result.reason
    logger.info("session %s: intent=%s (%s)", callback_context.session.id, result.intent, result.reason)


def _build_session_data(state, session_id: str) -> None:
    """Loads the upload (or the sample) into the session folder and fills the state.

    Args:
        state: The ADK session state (mutable mapping).
        session_id: Id of the session; names the folder under ``sessions.ROOT``.

    Side Effects:
        Writes ``data.db``, updates ``state`` and logs the outcome. Never logs file
        contents.
    """
    started = time.monotonic()
    session_dir = sessions.new_session_dir(session_id, sessions.ROOT)  # explicit so tests can redirect ROOT
    name, encoded = state.get("upload_name"), state.get("upload_b64")
    source = "upload" if name and encoded else "sample"

    if source == "upload":
        result = _load_upload(name, encoded, session_dir)
        if isinstance(result, IngestError):
            logger.warning(
                "session %s: upload rejected (%s); using the sample", session_id, result.message
            )
            state["load_error"] = f"Your file could not be loaded: {result.message}"
            source, result = "sample", load_sample(session_dir)
    else:
        result = load_sample(session_dir)

    if isinstance(result, IngestError):  # the bundled sample should always load
        logger.error("session %s: sample failed to load: %s", session_id, result.message)
        return
    state["db_path"] = str(result.db_path)
    state["column_map"] = result.column_map
    state["schema_summary"] = result.schema_summary
    logger.info(
        "session %s: data built from %s, %d rows, %.2fs",
        session_id, source, result.row_count, time.monotonic() - started,
    )


def _load_upload(name: str, encoded: str, session_dir: Path) -> IngestResult | IngestError:
    """Decodes a base64 upload from the state and loads it.

    Args:
        name: Original file name (its extension picks the parser).
        encoded: Base64 text of the file bytes.
        session_dir: Folder that will hold ``data.db``.

    Returns:
        The :func:`load_file` result, or an :class:`IngestError` if the base64 is invalid.
    """
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return IngestError("The uploaded file data was corrupted in transfer.")
    return load_file(name, data, session_dir)


def _sweep() -> None:
    """Deletes stale session folders and logs how many were removed."""
    removed = sessions.sweep_stale(root=sessions.ROOT)
    if removed:
        logger.info("swept %d stale session folder(s)", len(removed))


root_agent = Agent(
    name="data_analyst",
    model=AGENT_MODEL,
    description="Answers questions about an uploaded spreadsheet using read-only SQL tools.",
    instruction=INSTRUCTION,
    before_agent_callback=ensure_data,
    tools=[get_table_info, get_sample_rows, get_column_stats, run_sql],
)
