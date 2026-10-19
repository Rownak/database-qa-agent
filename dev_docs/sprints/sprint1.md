# Sprint 1 — Database Q&A Agent MVP

**Goal:** a working local demo, built in **about 2 hours** by following [design.md](design.md), where you upload a CSV or XLSX file and get grounded answers about its data. Backlog items are not part of this sprint.

| Phase | Focus | Time |
|---|---|---|
| 1.1 | uv project and dependency setup | 15 min |
| 1.2 | File ingestion | 25 min |
| 1.3 | Agent tools and SQL safety | 25 min |
| 1.4 | ADK agent | 15 min |
| 1.5 | Default sample database | 15 min |
| 1.6 | Streamlit UI and session lifecycle | 25 min |
| 1.7 | End-to-end verification | 15 min |

---

## Phase 1.1 — uv Project and Dependency Setup (15 min)

**Goal:** a standalone `spreadsheet-qa/` uv project that installs everything, can reach Gemini, and includes sample data.

- [x] Run `uv init --app --no-workspace --python 3.12 spreadsheet-qa` and delete the generated `main.py`. The `--no-workspace` flag keeps the project separate from the course's root `pyproject.toml`.
- [x] Run `uv add google-adk streamlit pandas openpyxl python-dotenv`. The resulting `pyproject.toml` and `uv.lock` replace the `requirements.txt` mentioned in the design.
- [x] Create the folders and empty files from design §3: `app.py`, `data_agent/{__init__,agent,tools,ingest,sessions}.py`, `sample_data/`, and `.streamlit/config.toml` containing `[server] maxUploadSize = 1` (Streamlit only takes whole MB; the 500 KB limit is enforced in `load_file`).
- [x] Add a `.env` file with `GOOGLE_API_KEY`, and add `.env` to `.gitignore`.
- [x] Add sample files to `sample_data/`:
  - [x] `sales.csv`, about 50 rows with columns `order_id`, `order date`, `category`, `sales_amount`, `revenue`
  - [x] `sales.xlsx`, the same data on one sheet
  - [x] `multi_sheet.xlsx`, with 2 sheets
  - [x] invalid files: `notes.txt`, `empty.csv`, `headerless.csv`, `corrupt.xlsx` (a text file renamed to `.xlsx`), `numeric_header.csv`, `duplicate_header.csv`
- [x] Run `uv run python -c "import google.adk, streamlit, pandas, openpyxl"` to confirm the installs.

## Phase 1.2 — File Ingestion (`ingest.py`, 25 min)

**Goal:** a valid file becomes table `data` in a SQLite database for the session. An invalid file gives a clear error and creates no database (design §5).

- [x] Add a `load_file(uploaded_name, file_bytes, session_dir)` function that returns either a result (`db_path`, `row_count`, `column_count`, `column_map`, `schema_summary`, `ignored_sheets`, preview DataFrame) or an error message.
- [x] Add the validation steps in order: extension, 500 KB size limit, readable (CSV with `utf-8-sig`, XLSX with `openpyxl`), not empty after dropping fully empty rows and columns, header row valid (not blank, every header text rather than a number, no duplicate column names; design §5 step 5).
- [x] For XLSX files, load only `sheet_names[0]` and record the other sheet names in `ignored_sheets`.
- [x] Clean the column names: lowercase, non-alphanumeric runs become `_`, `c_` prefix if a name starts with a digit. Keep a map from cleaned to original names.
- [x] Infer types: numeric dtypes stay as they are. For `object` columns, try `to_numeric` first, then `to_datetime` (stored as ISO text and labeled `date`), and otherwise keep `TEXT`.
- [x] Write the table with `df.to_sql("data", ...)` and close the connection. Build `schema_summary` as one line per column: `name (type) — original name`.
- [x] Quick check: run it with `uv run python` on every sample file and confirm the counts and error messages.

## Phase 1.3 — Agent Tools and SQL Safety (`tools.py`, 25 min)

**Goal:** four read-only tools that use the session's database and never raise errors to the agent (design §4 and §6).

- [x] Add a `_connect_ro(tool_context)` helper that opens `file:{db_path}?mode=ro` using `db_path` from `tool_context.state`. Wrap it in `closing(...)` and set a progress handler that stops queries after about 5 seconds.
- [x] Add `get_table_info`, `get_sample_rows(n)` (at most 20 rows), and `get_column_stats(column)`. For an unknown column, return an error that lists the valid columns.
- [x] Add the `run_sql(query)` pre-check:
  - strip one trailing `;`, and reject the query if any other `;` is left
  - the query must start with `SELECT` or `WITH`
  - reject the blocked keywords (`ATTACH`, `PRAGMA`, `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`), matched as whole words
- [x] In `run_sql`, fetch up to `MAX_ROWS + 1` rows and set `truncated` if there are more than `MAX_ROWS`. Cut cell text to 200 characters, and return any `sqlite3.Error` as `{"status": "error", ...}`.
- [x] Quick check: call the tools directly with a fake `tool_context`, a simple object whose `.state` holds `db_path`. Confirm that `DELETE FROM data` and `WITH x AS (SELECT 1) DELETE FROM data` are rejected, and that the row count is unchanged afterwards.
- [x] Add short developer test scripts `spreadsheet-qa/dev_test/phase_1_1.py`, `phase_1_2.py`, `phase_1_3.py` (run with `uv run python -m dev_test.phase_1_N` from `spreadsheet-qa/`) and point each execution log's "Commands to test" at them.

## Phase 1.4 — ADK Agent (`agent.py`, 15 min)

**Goal:** a single `root_agent` that picks the right tool, writes SQLite queries, and names its source.

- [x] Define `AGENT_MODEL = "gemini-3.5-flash-lite"` and `root_agent = Agent(name="data_analyst", ..., tools=[the four tools])`.
- [x] Write the instruction from design §4. It should:
  - include `{schema_summary}` so the agent knows the table's columns
  - tell the agent to prefer a dedicated tool and use one `SELECT` for anything else
  - require double-quoted, cleaned column names
  - forbid made-up numbers and tell the agent to explain any missing data
  - allow one retry when a query fails
  - end every answer with a `Source:` line
- [x] Quick check: write a small script that loads `sales.csv` through `load_file`, creates an ADK session with the result as its state, and asks "How many records are there?" through `Runner.run_async`. Confirm the answer is correct and a tool call appears in the events.
- [x] Add a dev-only `before_agent_callback` on `root_agent` so `adk web` works: if `db_path`, `column_map` or `schema_summary` is missing from `callback_context.state`, load `sample_data/sales.csv` with `load_file` into a temp session folder and set the three keys. It does nothing when the state is already set, so Streamlit (Phase 1.5) is unaffected. Confirm `uv run adk web` answers a question without the `schema_summary` KeyError (see `dev_docs/errors/adk_web_schema_summary_keyerror.md`).
- [x] Run `uv run pytest` from `spreadsheet-qa/` and confirm all tests pass.

## Phase 1.5 — Default Sample Database (`ingest.py`, `agent.py`, 15 min)

**Goal:** every new session (Streamlit or ADK Web) starts with `sample_data/sales.csv` loaded. A valid upload replaces it; an invalid upload keeps it and shows a clear error (design §1 and §7).

- [x] In `ingest.py`, add `load_sample(session_dir)`, which calls `load_file` on `sample_data/sales.csv` and returns its result.
- [x] Make `load_file` replace `data.db` atomically: write to a temp file in `session_dir`, then `os.replace` it over `data.db` only after the write succeeds.
- [x] Confirm validation still runs before any write, so an invalid file leaves the existing `data.db` unchanged.
- [x] Update the `before_agent_callback` in `agent.py` to use `load_sample`.
- [x] Add `dev_test/phase_1_5.py`: load the sample, upload `notes.txt` (error, sample still has 50 rows), then upload `multi_sheet.xlsx` (replaces the sample).
- [x] Run `uv run pytest` from `spreadsheet-qa/` and confirm all tests pass.

## Phase 1.6 — Streamlit UI and Session Lifecycle (`app.py`, `sessions.py`, 25 min)

**Goal:** the upload-and-chat flow works, each session is isolated, and cleanup happens automatically (design §1 and §7).

- [x] In `sessions.py`, add:
  - `purge_all()`, run once per server process through `@st.cache_resource`
  - `new_session_dir(session_id)`
  - `touch(dir)`
  - `sweep_stale(max_age=30 min)`
  - `end_session(dir)`, which runs `rmtree` with `ignore_errors=True`
- [x] On the first run, create `session_id`, the session folder, an `InMemorySessionService`, a `Runner`, and `last_active`, all stored in `st.session_state`. Load the sample with `load_sample` and create the ADK session with its state.
- [x] On every rerun: if the session has been inactive for more than 30 minutes, end it. Otherwise update `last_active`, touch the folder, and sweep stale folders.
- [x] Upload flow: on a new file, call `load_file`. On success, replace the ADK session with one holding the new state, clear the chat, and show the row and column counts, the preview, and the ignored-sheets notice. On error, show `st.error` and keep the current data, ADK session, and chat.
- [x] Chat flow: use `st.chat_input` and run `asyncio.run(...)` over `runner.run_async`. Show the final response, plus an expander labeled "How this was answered" that lists the tool calls and their arguments (the SQL). Chat is available from the start, using the sample data; show which file is currently loaded.
- [x] Add an **End session** button that deletes the folder, clears `st.session_state`, and calls `st.rerun()`.
- [x] Split the page with `st.columns([1, 2])`:
  - left: uploader, upload error, current data (name, counts, ignored-sheets notice, preview) and **End session**
  - right: chat history in a scrollable `st.container(height=...)`, with `st.chat_input` below it
- [x] Run `uv run pytest` and `uv run streamlit run app.py` and confirm upload, chat and **End session** still work in the new layout.
- [x] Run `uv run pytest` from `spreadsheet-qa/` and confirm all tests pass.

## Phase 1.7 — End-to-End Verification (15 min)

**Goal:** every acceptance criterion in project_description.md §6 passes when you run `uv run streamlit run app.py`.

- [x] `sales.csv` and `sales.xlsx` show the correct counts and preview. `multi_sheet.xlsx` lists the ignored sheet.
- [x] `notes.txt`, `empty.csv`, `headerless.csv`, `numeric_header.csv`, `duplicate_header.csv`, and `corrupt.xlsx` each show their specific error, and no `data.db` is created.
- [x] "How many records are there?" returns the correct count.
- [x] "What is the average sales amount?" matches the pandas result.
- [x] "Which category has the highest total revenue?" is correct, and the expander shows a `GROUP BY` query.
- [x] "What is the profit margin?" explains that cost data is missing and gives no number.
- [x] "Delete all rows" is refused, and asking for the count again gives the same number.
- [x] Every answer ends with a `Source:` line.
- [x] After **End session** the folder is gone. Uploading a new file resets the chat. Restarting the app empties `adk_sheet_qa/`.
- [ ] Note any failures, fix them or record them, then commit the working demo.
- [x] Run `uv run pytest` from `spreadsheet-qa/` and confirm all tests pass.

