# Database Q&A Agent — Technical Design (v1)

This design covers the first working demo described in [project_description.md](project_description.md). For each component it picks one simple approach. Anything not listed here is out of scope for v1.

---

## 1. Architecture and Workflow

Everything runs in **one local Python process**: Streamlit serves the UI and calls the ADK `Runner` directly. There is no separate backend.

```
Browser ──upload──▶ app.py ──▶ ingest.py ──▶ <temp>/<session_id>/data.db
                      │            (validate, clean, load)
                      │
        ──question──▶ app.py ──▶ ADK Runner ──▶ LlmAgent (Gemini)
                      ▲                            │ function calls
                      │                            ▼
                      └──── final answer ◀──── tools.py ──read-only──▶ data.db
```

**Session start**

Every new session, in Streamlit or in ADK Web, starts with table `data` loaded from `sample_data/sales.csv` (through the same `load_file`), so the user can ask questions right away. Streamlit does this when it creates the session. ADK Web does it in the agent's `before_agent_callback` (§4), because it creates sessions with empty state.

**Upload flow**

1. `app.py` receives the file from `st.file_uploader`.
2. `ingest.py` validates and parses it into a DataFrame, cleans the column names, infers types, and writes table `data` to the session database.
3. On success, the uploaded data **replaces** the sample (or previous) data: `app.py` creates a new ADK session whose state holds `db_path` and a short `schema_summary`, and the UI shows a summary and a preview. On failure, the existing database and ADK session are **kept unchanged**, and the UI shows the specific error.

**Question flow**

1. `app.py` sends the question to `runner.run_async(...)`, wrapped in `asyncio.run` because Streamlit scripts are synchronous.
2. The agent calls one or more tools. Each tool reads `db_path` from `tool_context.state` and returns a small dict.
3. `app.py` collects the events. It displays the final response and lists the tool calls and SQL in an expander labeled "How this was answered".

## 2. Technology Choices

| Need | Choice | Why |
|---|---|---|
| Agent framework | `google-adk` (`Agent`, `Runner`, `InMemorySessionService`) | Same pattern as the course examples (`session-state-runner/`) |
| Model | `gemini-3.5-flash-lite`, set as one constant | Cheap and fast, and good enough for simple SQL |
| UI | `streamlit` (`file_uploader`, `chat_input`, `chat_message`) | Upload and chat in about 100 lines, with no frontend code |
| Parsing | `pandas` + `openpyxl` | One API for CSV and XLSX, and `DataFrame.to_sql` writes to SQLite |
| Storage | `sqlite3` (standard library) | A single file per session, with a built-in read-only mode |
| Config | `python-dotenv` (`.env` holds `GOOGLE_API_KEY` or the Vertex AI settings) | Matches the existing course code |

`requirements.txt` lists `google-adk`, `streamlit`, `pandas`, `openpyxl`, and `python-dotenv`.

## 3. Project Structure

```
spreadsheet-qa/
├── app.py                 # Streamlit UI, ADK runner wiring, session start/end
├── data_agent/
│   ├── __init__.py
│   ├── agent.py           # root_agent definition + instruction text
│   ├── tools.py           # the four agent tools + SQL guard
│   ├── ingest.py          # validation, column cleaning, type inference, SQLite load
│   └── sessions.py        # temp-dir creation, activity tracking, cleanup
├── sample_data/           # sales.csv, sales.xlsx, multi_sheet.xlsx, and invalid files
├── .streamlit/config.toml # server.maxUploadSize = 1 (whole MB; the 500 KB limit is enforced in ingest)
├── .env
└── requirements.txt
```

`data_agent/agent.py` exposes `root_agent`, which is the usual ADK convention. `app.py` is the only place that knows about Streamlit. The other modules are plain Python and can be tested without the UI or the LLM.

## 4. Agent and Tools

**Agent:** one `Agent(name="data_analyst", model=AGENT_MODEL, instruction=..., tools=[...])`. It has no sub-agents or output schema. Its only callback is a `before_agent_callback` that loads the sample data when the session state has no `db_path`, `column_map`, or `schema_summary` (ADK Web). It does nothing in Streamlit, where the state is always set.

**Instruction (main points):**

- "You answer questions about one table named `data`. Its schema is: `{schema_summary}`." ADK fills in this placeholder from session state.
- Use a dedicated tool when one fits: counts and schema → `get_table_info`, examples → `get_sample_rows`, single-column summaries → `get_column_stats`. For anything else (filters, grouping, ranking, multi-column math) write **one SQLite `SELECT`** and call `run_sql`.
- Use only cleaned column names, and quote them with double quotes.
- Base every number on a tool result. Never estimate or make up values.
- If the needed columns don't exist, say what is missing and list the columns that are available.
- If `run_sql` returns an error, fix the query once. If it fails again, explain the problem.
- End each answer with a short "Source:" line naming the tool, or showing the SQL.

> **Decision — put the schema in the instruction.** The agent knows the columns before its first tool call, so a simple question needs one call instead of two. `get_table_info` is still available for exact counts and types.

**Tools** (plain functions with docstrings and type hints; the last parameter is `tool_context: ToolContext`):

| Tool | Returns |
|---|---|
| `get_table_info()` | `row_count`, `column_count`, list of `{name, original_name, type}` |
| `get_sample_rows(n: int = 5)` | Up to `n` rows (at most 20) |
| `get_column_stats(column: str)` | Numeric: count, nulls, min, max, mean, median. Text: count, nulls, distinct count, top 5 values with counts. |
| `run_sql(query: str)` | `columns`, `rows`, `row_count`, `truncated` |

Every tool returns `{"status": "success", ...}` or `{"status": "error", "error_message": "..."}`. Tools never raise exceptions to the agent. A column name that doesn't exist returns an error listing the valid names.

**How the agent picks a tool:** the agent chooses on its own, based on the instruction and the tool docstrings. There is no planner or router.

## 5. Data Handling

**Validation (in this order; the first failure stops ingestion):**

1. **Extension** is `.csv` or `.xlsx`. Otherwise: "Unsupported file type. Upload a .csv or .xlsx file."
2. **Size** is at most 500 KB. This is checked in `load_file` on the file bytes. Streamlit's `maxUploadSize` only takes whole MB, so it is set to 1 as a coarse backstop.
3. **Readable:**
   - CSV is read with `pd.read_csv(encoding="utf-8-sig")`, which also accepts files with a BOM.
   - XLSX is opened with `pd.ExcelFile(engine="openpyxl")`.
   - A decode or parse error gives "Could not read the file: \<reason\>."
4. **Not empty:** after columns and rows that are completely empty are dropped, there must be at least 1 row and at least 1 column.
5. **Header row present and valid.** The uploaded file must have a header row. After empty columns are dropped, the checks run in this order and the first failure stops ingestion:
   - If *every* header is auto-generated (`Unnamed: N`), the result is "No header row detected."
   - If *some* column has a blank header, the result is "Column N has no header."
   - Every header must be a **string**. A header that is a plain number (for example `1001` or `2024.5`) is rejected with "Column N header must be text, not a number: '1001'." This also catches files whose first row is data.
   - Headers must be **unique**. Two headers are duplicates if they are equal after trimming and cleaning (so `Order ID` and `order_id` clash). The result is "Duplicate column name: 'A' (column i) and 'B' (column j)." Nothing is renamed automatically.

**Excel worksheets:** the app reads `ExcelFile.sheet_names` and loads only `sheet_names[0]`. If there are more sheets, the result includes `ignored_sheets`, and the UI shows: "Loaded sheet 'X'. Ignored: 'Y', 'Z'."

**Table and column naming:**

- The table is always named `data`. A fixed name keeps the agent's prompts and SQL simple.
- Column names are cleaned like this:
  - trim, lowercase, and replace each run of non-alphanumeric characters with `_`
  - strip leading and trailing `_`
  - add a `c_` prefix if the name starts with a digit
  - names are unique by this point (validation step 5 rejects duplicates), so no collision handling is needed
- The map from cleaned names to original names is saved in session state and shown by `get_table_info`.

**Type inference** (one simple pass, per column):

- pandas `int`/`float`/`bool` → SQLite `INTEGER`/`REAL`/`INTEGER`.
- `object` columns: if every non-null value converts with `pd.to_numeric`, the column becomes numeric. Otherwise, if every value converts with `pd.to_datetime`, it is stored as ISO-8601 `TEXT` and labeled `date`. Otherwise it stays `TEXT`.
- Values are not cleaned up further (no currency symbols or locale formats). This is in the backlog.

**Load:** `df.to_sql("data", conn, index=False, dtype=...)` on a normal read-write connection. The connection is closed right after the load.

## 6. SQL Execution

**Read-only access:**

- `run_sql` opens `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`, using the `db_path` from **this** session's state. The agent never supplies a path, so it cannot reach another session's database.
- A light pre-check rejects queries early with clear messages:
  - It strips one trailing `;` and rejects the query if any `;` is left (more than one statement).
  - The query must start with `SELECT` or `WITH`.
  - It rejects queries containing `ATTACH`, `PRAGMA`, `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `CREATE`, matched as whole words.

> **Decision — the read-only connection is the real safeguard.** The pre-check gives friendly errors, but the read-only connection is what actually protects the data. For example, `WITH ... DELETE` passes the "starts with WITH" check, but SQLite refuses to run it on a read-only connection.

**Limits:**

| Limit | Value | Purpose |
|---|---|---|
| `MAX_ROWS` | 100 | Rows fetched with `fetchmany(MAX_ROWS + 1)`; `truncated=True` if there were more |
| Cell text | 200 characters | Keeps tool output small for the model |
| Query time | about 5 seconds | Enforced with `conn.set_progress_handler`; a slow query is stopped and reported as an error |

**Errors:** any `sqlite3.Error` comes back as `{"status": "error", "error_message": str(e)}`. The agent sees the message and can retry once.

**Connections:** each tool call opens and closes its own connection (`with closing(...)`). Nothing stays open between calls, so the database file can always be deleted. This matters on Windows, which won't delete a file that is still open.

## 7. Session Lifecycle

**Isolation:**

- Each Streamlit browser session gets a `session_id` (`uuid4`) in `st.session_state`.
- It also gets its own `InMemorySessionService` and `Runner`, plus a folder at `<tempfile.gettempdir()>/adk_sheet_qa/<session_id>/`. The folder holds `upload.<ext>` and `data.db`.
- The ADK session state holds `db_path`, `schema_summary`, and `column_map`.

**Lifecycle events:**

| Event | Action |
|---|---|
| App process starts | `sessions.purge_all()` deletes all of `adk_sheet_qa/`. It runs once per process through `@st.cache_resource`. |
| First page load | Create `session_id`, the folder, and the runner. Load `sample_data/sales.csv` into `data.db` and create the ADK session with its state. Set `last_active = now`. |
| Valid upload | Replace `data.db` with the new data, and replace the ADK session (`delete_session` + `create_session` with the new state). Clear the chat history. |
| Invalid upload | Keep `data.db`, the ADK session, and the chat unchanged. Show the validation error with `st.error`. |
| Each rerun (any interaction) | If `now - last_active` is more than 30 minutes, run **end session**. Otherwise update `last_active` and touch the folder's modified time. Also delete any *other* session folder whose modified time is older than 30 minutes, which catches tabs that were closed. |
| **End session** button | Delete the folder, then clear `st.session_state`. The next rerun starts a new session. |

> **Decision — replace the database only after validation, and atomically.** `load_file` validates everything before it writes. It then writes to a temporary file in the session folder and swaps it in with `os.replace`, so a failed write never leaves the session without data.

> **Decision — clean up on reruns, with no background thread.** Streamlit can't tell when a tab closes. Purging when the app starts and sweeping old folders on each rerun covers abandoned sessions without a scheduler, which is enough for a local demo.

Deletion uses `shutil.rmtree(..., ignore_errors=True)`. A failed delete is logged and does not block the user.

## 8. Verification Checklist

**Without the LLM** (call the functions directly, or use a few `pytest` cases):

- [ ] `ingest` loads `sales.csv` and `sales.xlsx` with the expected row and column counts and cleaned names.
- [ ] `multi_sheet.xlsx` loads only the first sheet and reports the ignored sheet names.
- [ ] A `.txt` file, an empty CSV, a corrupt `.xlsx`, a headerless CSV, a CSV with a numeric header, and a CSV with duplicate headers each return the expected error, and no `data.db` is created.
- [ ] `run_sql("SELECT COUNT(*) FROM data")` succeeds, and a query returning more than 100 rows comes back with `truncated=True`.
- [ ] `run_sql` rejects `DELETE FROM data`, `WITH x AS (SELECT 1) DELETE FROM data`, and a query with two statements, and the row count is unchanged afterward.

**End to end** (`streamlit run app.py`, using `sales.csv`):

- [ ] A new session (Streamlit or ADK Web) answers "How many records are there?" with 50 before any upload.
- [ ] An invalid upload shows its error, and the previous data can still be queried.
- [ ] "How many records are there?" returns the correct count, citing `get_table_info` or the SQL.
- [ ] "What is the average sales amount?" matches the value pandas computes.
- [ ] "Which category has the highest total revenue?" returns the correct category, and the expander shows a `GROUP BY` query.
- [ ] "What is the profit margin?" gets an answer saying the cost data is missing, with no made-up number.
- [ ] "Delete all rows" is refused, and a follow-up count is unchanged.
- [ ] After **End session** the session folder is gone. After uploading a new file the chat resets and old data can't be queried. Restarting the app empties `adk_sheet_qa/`.

## 9. Assumptions

- A single user runs the demo locally. In-memory ADK sessions are fine, and history is lost when the app restarts.
- CSV files are UTF-8 and comma-delimited. Delimiter detection is not needed for v1.
- Files of 500 KB or less fit comfortably in memory with pandas.
- Gemini access is configured through `.env`, the same way as in the other course folders.

## 10. Future Backlog

- Explicit planning step before running a query
- SQL review and reflection (self-check or a critic step)
- Stronger guardrails (input and output filtering, query cost limits, PII handling)
- Richer validation and type cleanup (currencies, locales, mixed types, choosing a sheet or loading several)
- Hosting as a microservice (separate API and UI)
- Cloud Run deployment using ADK's built-in deployment support
