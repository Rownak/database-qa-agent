# Sprint 6 — Multiple Data Files per Session

**Goal:** a session can hold **up to three CSV/XLSX files**. Each file becomes its own table in the session's `data.db`, and the agent can query across them. Document upload (up to 3 documents) is unchanged.

Today a session holds one file: `ingest.load_file` writes a single table `data`, the UI has one uploader that replaces it, and the session state carries one `upload_name` / `upload_b64`. `run_sql` and `search_documents` are the agent's only tools, and the table schema reaches the agent only through `{schema_summary}` in its instruction. Both the ADK agent and the UI already handle multi-table databases for LakeQuest (`bank.db`), so the agent side needs little change.

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`)
- a short script `db-doc-qa/dev_test/phase_6_N.py`, run with `uv run python -m dev_test.phase_6_N`
- an entry in `dev_docs/execution_logs/sprint_6_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 6.1 | Implementation: validation, ingestion, UI, agent |
| 6.2 | Verification (local and API mode) |
| 6.3 | Redeployment and hosted smoke test |

## Design decisions

- **Limit:** at most 3 data files per session (`MAX_TABLES = 3`). The existing 500 KB per-file limit stays.
- **Table name:** the file stem, cleaned with the existing `_clean_name` rule (lower-case, non-alphanumerics to `_`). A stem that starts with a digit gets a `t_` prefix; an empty stem becomes `table`. Examples: `Sales 2024.csv` → `sales_2024`, `2024.csv` → `t_2024`.
- **Collision:** a different file whose name cleans to a name already in use gets `_2`, `_3` (`sales.csv` and `Sales.xlsx` → `sales`, `sales_2`). Names starting with `sqlite_` are prefixed with `t_` too.
- **Replacement:** uploading a file with the **same file name** (case-insensitive) replaces that table in place, keeping its table name and position. It does not count against the limit. A new name at the limit is rejected with "Up to 3 data files per session. Remove one first."
- **Sample data:** a new session starts with the bundled `shipments.csv`, still loaded as table `data`. The sample keeps this generic name because users may upload any kind of table (finance, student_info, …), so nothing in the prompts, evals or tests depends on the sample being about shipments. The naming rules apply to uploads only. The first user upload **replaces** the sample; later uploads add to it. Removing the last file brings the sample back, so a session is never empty.
- **XLSX:** first sheet only, as today. The table is named after the file, not the sheet.
- **Session state:** `uploads: [{name, b64}]` replaces `upload_name` / `upload_b64`, like `docs`. A missing key means the sample. `column_map` becomes `{table: {column: original}}`. The backend rebuilds `data.db` from `uploads` whenever it is missing, as today.
- **Partial failure:** if the backend cannot load one file, it skips it and loads the rest. The skip goes into `load_error`. If no file loads, it uses the sample, as today.
- **Out of scope:** joins that the user must define, cross-file foreign-key detection, files over 500 KB, multi-sheet loading, and any change to the document pipeline.

---

## Phase 6.1 — Implementation

**Goal:** the UI, ingestion and agent work with one to three tables, and documents behave as before.

- [x] **Ingestion** (`data_agent/ingest.py`):
  - split `load_file` into a pure `read_table(name, bytes)` that runs every existing check and returns the cleaned frame and metadata or an `IngestError` (writes nothing), and a writer
  - add `table_name_for(file_name, taken: set[str]) -> str` with the naming and collision rules above
  - add `load_files(files: list[tuple[str, bytes]], session_dir) -> LoadResult | IngestError`: builds all tables in a temp database, then replaces `data.db` atomically (the current `_write_sqlite` pattern). Nothing is touched on failure
  - add `MAX_TABLES = 3` and the limit check in `load_files`
  - `LoadResult` holds `db_path`, `column_map`, `schema_summary` (below) and one `TableInfo` per table (`file_name`, `table`, `rows`, `columns`, `preview`, `sheet`, `ignored_sheets`)
  - `schema_summary` is one block per table: `"table_name" — N rows, from file.csv`, then one `name (type) — original name` line per column
  - `TABLE_NAME = "data"` is renamed `SAMPLE_TABLE` and kept for `load_sample` only. Keep `load_file` as a one-file wrapper only if existing tests need it, otherwise remove it
- [x] **Agent** (`data_agent/agent.py`, `runner.py`):
  - `_build_session_data` reads `uploads` and calls `load_files`; on a per-file failure it skips that file, sets `load_error`, and loads the rest (fall back to the sample when none load)
  - set `column_map` in its new shape; keep `_STATE_KEYS`
  - `INSTRUCTION`: replace "a SQLite table named "data" (shipment records)" with "the SQLite tables listed below", list `{schema_summary}`, and tell the agent to quote table names, join tables only on columns that clearly match in name and meaning, and say when a join key is unclear. Keep the grounding and safety rules. The `lakequest` profile is unchanged
  - `intent.py`: change "a database table of shipment records" to "database tables"
  - `runner.files_state(tables: list[Path] | None, docs)`: build `uploads`
- [x] **Tools** (`data_agent/tools.py`): change `TABLE_NAME` use. `get_table_info`, `get_sample_rows` and `get_column_stats` are not registered on the agent, removed with their tests (they read a hard-coded table). Update the `run_sql` docstring only if it still names one table
- [x] **UI** (`app.py`):
  - keep one uploader; label it "Add or replace a data file (CSV or XLSX, up to 3)"
  - `ss.table_entries`: list of `{name, b64, table, rows, cols, preview, sheet, ignored}`, like `ss.doc_entries`; `ss.tables_custom` is false while the sample is shown
  - `_handle_upload`: validate with `read_table`; same name → replace in place; new name at the limit → `TABLE_LIMIT_MESSAGE`; otherwise add (dropping the sample if it was the only entry). A rejected file changes nothing and keeps the chat
  - on success, call `_replace_session` with the full list (new session, chat cleared), as for documents
  - show "Data files" like "Documents": per file its table name, size and a Remove button, plus a preview of the selected file (selectbox or one expander per file, whichever is smaller). Show the ignored-sheet warning per file
  - `_session_state` sends `uploads` (omitted while the sample is shown); `_adk_state` carries the new `column_map`; the lost-session recovery in `main` passes the list
  - `max_upload_size` in `.streamlit/config.toml` is unchanged
  - update the chat input hint to "Ask about your data or the policy documents"
- [x] **Docs:** update the module docstrings that say "a table" and the README upload section. Add the table-name rules to `db-doc-qa/README.md`
- [x] **Tests:**
  - `test_ingest.py`: the naming rules (spaces, leading digit, empty stem, `sqlite_`, collisions `_2`/`_3`); one, two and three files give that many tables; a fourth is rejected; one bad file returns `IngestError` and leaves an existing `data.db` untouched; `schema_summary` lists every table
  - `test_agent.py`: `uploads` with two files builds two tables; one bad file is skipped with a `load_error` and the other loads; no valid file falls back to the sample; the instruction lists all tables
  - `test_app.py` / `test_app_api.py` (Streamlit `AppTest`, existing pattern): add a second file; replace by the same name (table name and position kept, no limit hit); a fourth new name shows the limit error; Remove brings the sample back; a rejected file keeps the chat; the API-mode session state carries `uploads`
  - `test_app_docs.py` and the document tests pass unchanged (document regression)
  - update existing tests that use `upload_name` or the flat `column_map` (tests that name table `data` stay as they are)
- [x] **Dev script** `phase_6_1.py`: load `sales.csv`, `sales.xlsx` (collision) and a third file from `sample_data/database/`, print the table names, row counts and the `schema_summary`; then ask the live agent one question across two tables and print the SQL it ran
- [x] **Regression:** run `configs/eval/smoke.yaml` and confirm the shipments metrics do not drop (the sample table name is unchanged)

---

## Phase 6.2 — Verification

**Goal:** the multi-file behaviour is checked locally, in both local and API mode, before anything is deployed.

- [x] **Local checks** (`uv run streamlit run app.py`, local mode, using `sample_data/database/`):
  - single file: upload `sales.csv`; the sample is replaced, one table, a query works
  - three files: add two more; three tables, a question that needs a join or a comparison across two of them is answered with a correct SQL result
  - limit: a fourth new file is rejected and nothing changes; replacing an existing file at three files works
  - invalid files: `empty.csv`, `headerless.csv`, `duplicate_header.csv`, `numeric_header.csv`, `corrupt.xlsx`, `notes.txt`, a file over 500 KB; each shows its error and keeps the loaded tables and the chat
  - name collisions: `sales.csv` then `sales.xlsx` gives `sales` and `sales_2`; `Sales.CSV` after `sales.csv` replaces it
  - `multi_sheet.xlsx`: first sheet loaded, the ignored-sheet warning shown
  - Remove: removing one file keeps the others; removing all returns the sample
  - documents: add, replace and remove a document with data files loaded; hybrid question still answered with citations
- [x] **API mode check:** run the ADK API locally (`adk api_server`) and the UI with `API_URL` set; repeat the three-file case once. Confirm the session state sent has `uploads` and the backend builds all tables
- [x] **Full test run:** `uv run pytest` passes, including all earlier phases
- [x] **Fix findings:** any bug found here is fixed in the code with a regression test before Phase 6.3 starts
- [x] **Dev script** `phase_6_2.py`: with the local API server, create a session with two uploads, ask one cross-table question and print the answer and tool calls
- [x] **Execution log** `sprint_6_2.md`: the checks run and their results, the `uv run pytest` command and result, and any known limits

## Phase 6.3 — Redeployment and Hosted Smoke Test

**Goal:** the verified build is live on Cloud Run and works end to end. Starts only when Phase 6.2 passes.

- [x] **Deploy** with the existing workflow in [deploy.md](../deploy/deploy.md), from `db-doc-qa/`:
  - backend: `adk deploy cloud_run … data_agent`, then the required `gcloud run services update data-qa-api --remove-env-vars=…` step; check `/list-apps` returns `["data_agent"]`
  - UI: `uv run python scripts/stage_ui_context.py`, then `gcloud run deploy data-qa-ui --source .ui_build …`. Check that the staged context contains the files the UI now imports
  - keep `--max-instances=1` on both services
- [x] **Smoke test the hosted UI** (browser, public UI URL):
  - page loads with the sample data and sample documents
  - upload one file, ask a question, check the answer and the SQL in "How this was answered"
  - add two more files, ask a cross-table question
  - a fourth file is rejected; an invalid file shows its error
  - replace one file, remove one file, then End session
  - one document question still answers with citations
  - the backend log shows no errors and memory stays under the 512 MiB limit
- [x] **Docs:** record the new revision names, the date and the smoke-test result in `dev_docs/deploy/deploy.md` (new "Redeployed for Sprint 6" entry; note the new `uploads` state key and the replaced `upload_name` / `upload_b64`). Update the "Optional state keys" line
- [x] **Dev script** `phase_6_3.py`: against the hosted API, create a session with two uploads, ask one cross-table question and print the answer and tool calls (needs an ID token, as in `phase_2_6.py`)
- [x] **Execution log** `sprint_6_3.md`: the deployed revisions, the smoke-test outcome, and any known limits

---

## Open Questions

- `get_table_info`, `get_sample_rows` and `get_column_stats` are not registered on the agent. Resolved in Phase 6.1: removed.
