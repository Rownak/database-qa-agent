# Sprint 6 — Phase 6.1 Execution Log: Multiple Data Files per Session

## Summary
A session can now hold **up to three CSV/XLSX files**, each stored as its own table in the session's `data.db`. The ingestion, session state, agent instruction, UI and tests were changed for this. Document upload is untouched. `uv run pytest`: **333 passed**. The live cross-table question (`dev_test/phase_6_1.py`) and the shipments smoke eval (`configs/eval/smoke.yaml`) both ran against the real model. Nothing was deployed (Phase 6.3).

## What was done
- **Ingestion** (`data_agent/ingest.py`):
  - `read_table(name, bytes)` runs every existing check and returns a `ParsedTable` or an `IngestError`. It writes nothing.
  - `table_name_for(file_name, taken)`: the file stem lower-cased, runs of non-alphanumerics to `_`. A stem that starts with a digit or `sqlite_` gets `t_`. A stem that is empty becomes `table`. A name already taken gets `_2`, `_3`.
  - `load_files(files, session_dir)` parses and loads; `load_parsed(parsed, session_dir)` loads files that are already parsed (the agent uses it to skip bad files). Both build all tables in `data.db.tmp` and then `os.replace` it over `data.db`. A failure leaves the old `data.db` untouched. More than `MAX_TABLES = 3` (or none) is an `IngestError`.
  - `LoadResult` holds `db_path`, `tables` (one `TableInfo` per file: table name, counts, preview, sheet info), `column_map` as `{table: {column: original}}`, and `schema_summary`. Each table block is `"table" — N rows, from file` plus its column lines, blocks separated by a blank line.
  - `load_sample` still loads `shipments.csv` as table **`data`** (`SAMPLE_TABLE`). `load_file`, `IngestResult` and `TABLE_NAME` are removed.
- **Agent** (`data_agent/agent.py`, `runner.py`, `intent.py`):
  - Session state key `uploads: [{name, b64}]` replaces `upload_name` / `upload_b64`. A missing or empty key means the sample.
  - `_build_session_data` parses each upload with `_parse_uploads`. A file that fails (corrupt base64, invalid, or beyond the third) is skipped and named in `load_error`, and the rest load. If none load, the sample is used and `load_error` says so. The existing wording about "Duplicate column name" and "corrupted" is kept.
  - `INSTRUCTION`: describes "SQLite tables built from the user's data files (the bundled sample is the table "data")", lists the table blocks, and tells the agent to JOIN only on columns whose names and meaning clearly match and to say so when no join key is clear. Rule 10 and the notices rule (14) name tables and files. The `lakequest` profile is unchanged. The agent description now says "database tables".
  - `runner.files_state(tables: list[Path] | None, docs)` builds `uploads`. `evals/run.py` passes `[table]` (a dataset still has one `table`).
  - `intent.py` prompt: "database tables built from the user's data files".
- **Tools** (`data_agent/tools.py`): removed `get_table_info`, `get_sample_rows`, `get_column_stats` and their helpers. They were not registered on the agent, and they read a hard-coded table `data`, which is wrong with several tables. `run_sql` is unchanged except for its docstring.
- **UI** (`app.py`):
  - One uploader, "Add or replace a data file (CSV or XLSX, up to 3)". `read_table` validates the file first, so a rejected file shows its error and changes nothing.
  - The first upload replaces the sample. The same file name (case-insensitive) replaces that file in place. A new name at three files shows "Up to 3 data files per session. Remove one first."
  - "Data files" list: file name, table name, rows × columns, and a Remove button for uploads. Removing the last file brings the sample back. A selectbox (shown when there are two or more tables) picks which table to preview. The ignored-sheet warning is per file.
  - `ss.table_entries` / `ss.tables_custom` mirror `ss.doc_entries` / `ss.docs_custom`. The files are reloaded together on every change (`_apply_tables`), then a new agent session starts and the chat is cleared. `ss.uploads` replaces `ss.upload`. API mode sends `{"uploads": [...]}`.
  - The chat hint is now "Ask about your data or the policy documents".
- **Docs:** root `README.md` (summary, diagram, demo constraints with the table-name rules). `ui_client.py` docstring.
- **Older dev scripts** (`phase_1_2`, `1_3`, `1_4`, `1_5`, `1_7`, `2_2`, `3_1`) were updated for `load_files` / `uploads` so they still run.

## Tests
- `test_ingest.py` rewritten: all existing validation cases through `load_files`/`read_table`; the naming rules (spaces, leading digit, `sqlite_`, empty stem, `_2`/`_3` collisions); one, two and three files; a fourth rejected with nothing written; one bad file fails the whole `load_files` and names the file; atomic replace; failed write keeps `data.db`; the sample schema.
- `test_agent.py`: two uploads give two tables; a bad file is skipped with a `load_error` while the other loads; no valid file falls back to the sample and says so; a fourth file is reported; all tables are rebuilt after `data.db` is deleted; the instruction lists the tables and the JOIN rule.
- `test_app.py`: add second and third files; fourth rejected (chat kept); same name replaces in place even at the limit; same-stem files get `sales` and `sales_2`; an invalid file keeps tables and chat; Remove keeps the others and removing all restores the sample; documents are untouched by data-file changes.
- `test_app_api.py`: several files go out in one `uploads` list; removing every file sends the sample state (`{}`) again.
- `test_runner.py`: `files_state` with several files. `test_tools.py`: the table-tool tests are removed and `run_sql` tests use table `sales`.
- Document tests (`test_app_docs.py`, `test_doc_ingest.py` and the rest) pass with only the `uploads` assertion change.

## Regression: `configs/eval/smoke.yaml`
Real model, 6 cases, 0 errors. Run folder `db-doc-qa/runs/20261027-092529_smoke`.

| Metric | Sprint 5.1 log | Now |
|---|---|---|
| `tool_sequence_match` | 1.00 | 1.00 |
| `section_recall` | 0.88 | 1.00 |
| `citation_precision` | 1.00 | 1.00 |
| `answer_contains` | 0.92 | 1.00 |
| latency p50 / p95 | 5.3 s / 9.6 s | 4.5 s / 9.4 s |

The shipments metrics did not drop.

## Decisions and assumptions
- **Sample table stays `data`** (as decided in the plan review). The naming rules apply to uploads only. An upload named `data.csv` is fine, since the first upload removes the sample.
- **Empty stem becomes `table`**, not `t_` (a bare `t_` is not a useful name). The sprint plan said `t_` prefix; this is a small wording difference.
- **Table names follow the list order.** The UI and the backend both derive them from the same ordered list, so they always agree. Replacing a file keeps its position and name. Removing a file can change a later suffix (remove `sales`, and `sales_2` becomes `sales`).
- **The whole list is reloaded on every change** (at most 3 × 500 KB), instead of editing `data.db` in place. This matches how the backend rebuilds `data.db` from state on a new instance.
- **No backward compatibility for `upload_name` / `upload_b64`.** The UI and the backend are deployed together in Phase 6.3. Sessions created by the old UI are not supported by the new backend.
- **Uploading the same file twice in a row** (after removing it) is ignored by the existing "same file id as the last rerun" guard; the user must clear the uploader first. This behaviour already existed for single uploads.

## Files
- Modified: `data_agent/ingest.py`, `data_agent/agent.py`, `data_agent/tools.py`, `data_agent/runner.py`, `data_agent/intent.py`, `evals/run.py`, `app.py`, `ui_client.py`, `README.md`, `tests/conftest.py`, `tests/test_ingest.py`, `tests/test_tools.py`, `tests/test_agent.py`, `tests/test_runner.py`, `tests/test_app.py`, `tests/test_app_api.py`, `tests/test_app_docs.py`, `tests/test_sample_data.py`, `dev_test/phase_1_2.py`, `1_3`, `1_4`, `1_5`, `1_7`, `2_2`, `3_1`, `dev_docs/sprints/sprint6.md`
- Created: `dev_test/phase_6_1.py`, `dev_docs/execution_logs/sprint_6_1.md`

## Commands
```
cd db-doc-qa
uv run pytest                                  # 333 passed
uv run python -m dev_test.phase_6_1            # three tables, schema, limit message, live cross-table question
uv run python -m evals configs/eval/smoke.yaml # shipments regression
uv run streamlit run app.py                    # add up to three files in the left column
```

## Limitations
- Joins rely on the model matching column names; there is no foreign-key detection.
- Only the first sheet of an XLSX is loaded, and each file is limited to 500 KB.
- The hosted app is not updated until Phase 6.3. `adk web` still uses only the sample.
