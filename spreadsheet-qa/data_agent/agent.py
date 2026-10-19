"""The ADK agent that answers questions about the uploaded table.

Defines ``root_agent``, a single ``LlmAgent`` with no sub-agents (design section 4).
It sits between the Streamlit app, which supplies the session state (``db_path``,
``column_map``, ``schema_summary``) and runs it through an ADK ``Runner``, and the
read-only functions in :mod:`data_agent.tools`, which are its only way to touch data.
"""

import tempfile
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext

from data_agent.ingest import IngestResult, load_sample
from data_agent.tools import (
    get_column_stats,
    get_sample_rows,
    get_table_info,
    run_sql,
)

# Loaded here so the key is available however the agent is started (Streamlit, scripts).
load_dotenv()

AGENT_MODEL = "gemini-3.5-flash-lite"

# ADK replaces {schema_summary} with the value in session state, so this text must
# not contain any other curly braces.
INSTRUCTION = """\
You answer questions about one SQLite table named "data", built from a file the user uploaded.
Its columns (cleaned name, type, original name) are:
{schema_summary}

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
"""

_STATE_KEYS = ("db_path", "column_map", "schema_summary")


def seed_state_if_missing(callback_context: CallbackContext) -> None:
    """Loads the sample file into the session state when none was loaded (dev only).

    ``adk web`` starts sessions with empty state, so the ``{schema_summary}``
    placeholder in the instruction and the tools' ``db_path`` would be missing. The
    Streamlit app always sets all three keys before the first run, so this is a
    no-op there.

    Args:
        callback_context: ADK callback context; its ``state`` is read and updated.

    Returns:
        None, so the agent run continues normally.

    Side Effects:
        If any key is missing, creates a temp session folder holding ``data.db``
        built from ``sample_data/sales.csv`` and sets the three state keys.
    """
    state = callback_context.state
    if all(state.get(k) is not None for k in _STATE_KEYS):
        return None
    result = load_sample(Path(tempfile.mkdtemp()) / "session")
    if isinstance(result, IngestResult):
        state["db_path"] = str(result.db_path)
        state["column_map"] = result.column_map
        state["schema_summary"] = result.schema_summary
    return None


root_agent = Agent(
    name="data_analyst",
    model=AGENT_MODEL,
    description="Answers questions about an uploaded spreadsheet using read-only SQL tools.",
    instruction=INSTRUCTION,
    before_agent_callback=seed_state_if_missing,
    tools=[get_table_info, get_sample_rows, get_column_stats, run_sql],
)
