# Sprint 1 — Phase 1.4 Execution Log: ADK Agent (`agent.py`)

## What was implemented
- `spreadsheet-qa/data_agent/agent.py`: `AGENT_MODEL = "gemini-3.5-flash-lite"`, the `INSTRUCTION` text, and `root_agent = Agent(name="data_analyst", ..., tools=[get_table_info, get_sample_rows, get_column_stats, run_sql])`.
- The instruction (design §4) contains `{schema_summary}` (filled by ADK from session state) and rules to: prefer a dedicated tool, otherwise write one SQLite `SELECT` for `run_sql`; use only cleaned, double-quoted column names; never invent numbers; explain missing columns and give no number; retry a failed query once; refuse changes (data is read-only); mention truncation; end with a `Source:` line.
- `load_dotenv()` is called in `agent.py`, so `GOOGLE_API_KEY` is available however the agent is started.
- Tests: `pytest` added as a dev dependency with config in `pyproject.toml`; `tests/` covers Phases 1.2–1.4 (30 tests).
- `dev_test/phase_1_4.py`: the quick check (live call to Gemini).

## Key decisions
- The instruction must contain no curly braces other than `{schema_summary}`, because ADK treats any `{name}` as a state placeholder. A test enforces this.
- Rule 7 (refuse write requests) and rule 8 (mention truncation) were added beyond the design's list, to support the Phase 1.6 "Delete all rows" criterion and the `truncated` flag from `run_sql`.
- The pytest agent tests are offline (definition and instruction only); the live model call stays in `dev_test/`, so `pytest` needs no API key or network.

## Files
- Created: `spreadsheet-qa/data_agent/agent.py` (was empty), `spreadsheet-qa/dev_test/phase_1_4.py`, `spreadsheet-qa/tests/{__init__,conftest,test_ingest,test_tools,test_agent}.py`, `dev_docs/execution_logs/sprint_1_4.md`
- Modified: `spreadsheet-qa/pyproject.toml` and `uv.lock` (pytest dev dependency + pytest config), `dev_docs/sprints/sprint1.md` (Phase 1.4 ticked)

## Assumptions and limitations
- **Session state contract for Phase 1.5:** `db_path`, `column_map` and `schema_summary` must be in the ADK session state (the quick check shows how). They must be JSON-serializable (plain `str`/`dict`; convert `Path` to `str`).
- The live check needs a valid `GOOGLE_API_KEY` in `spreadsheet-qa/.env` (it worked here). Model answers can vary run to run.
- The pytest suite now also covers Phases 1.2 and 1.3, which had only `dev_test` snippets before.
- Only one question was run live; the Phase 1.6 questions are not yet verified.

## Quick check result
```
TOOL CALL: get_table_info {}
ANSWER: There are 50 records in the table.

Source: get_table_info
```

## Commands to test
Asks "How many records are there?" through `Runner.run_async` and prints the tool calls and the answer (needs the API key).
```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_4
uv run pytest
```
Result of `uv run pytest`: `30 passed`.

---

## Revision 1 — Dev-only `before_agent_callback` for `adk web`

Fixes [adk_web_schema_summary_keyerror.md](../errors/adk_web_schema_summary_keyerror.md) (option 3).

- **Change:** `agent.py` gets `seed_state_if_missing(callback_context)`, registered as `before_agent_callback`. If `db_path`, `column_map` or `schema_summary` is missing from the state, it loads `sample_data/sales.csv` via `load_file` into a temp folder and sets the three keys. If all three exist (the Streamlit flow) it does nothing.
- **Decision:** the guard checks `is not None`, not truthiness. A first version used truthiness, and a unit test caught it overwriting a preset empty `column_map`.
- **Behaviour note:** the callback's state change is emitted as its own event with no content, so code that reads `ev.content` on the final event must check it exists (`dev_test/phase_1_4b.py` does).
- **Deviation from design:** design §4 says the agent has no callbacks; this one exists only to make `adk web` usable and is inert in the app.
- **Tests:** `tests/test_agent.py::test_seed_state_fills_empty_state_only`; `uv run pytest` → `31 passed`.
- **Verified:** `dev_test/phase_1_4b.py` creates a session with **empty state** (like `adk web`) and gets "There are 50 records in the table. Source: get_table_info". I did not launch `adk web` itself; run it to confirm in the browser.

```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_4b
uv run pytest
uv run adk web   # from the folder containing data_agent/
```
