# Sprint 1 — Phase 1.5 Execution Log: Default Sample Database

## What was implemented
- `ingest.py`: new `load_sample(session_dir)` — calls `load_file` on `sample_data/sales.csv` (path built from the module location, so it works from any working directory).
- `ingest.py`: `_write_sqlite` now writes to `data.db.tmp` and moves it over `data.db` with `os.replace` only after a successful write. On any failure the temp file is removed and the existing `data.db` is untouched. (Before: the old `data.db` was deleted first.)
- Validation already ran fully before any write (Phase 1.2), so an invalid file never reaches the write step. Covered by tests.
- `agent.py`: the `before_agent_callback` now calls `load_sample` instead of loading the file itself.

## Files
- Modified: `spreadsheet-qa/data_agent/ingest.py`, `spreadsheet-qa/data_agent/agent.py`, `spreadsheet-qa/tests/test_ingest.py`, `dev_docs/sprints/sprint1.md`
- Created: `spreadsheet-qa/dev_test/phase_1_5.py`, `dev_docs/execution_logs/sprint_1_5.md`

## Key decisions / notes
- Atomicity covers the common failure (write error). It relies on no connection holding `data.db` open at swap time; tools open and close per call, so this holds. If a tool were mid-query on Windows during an upload, `os.replace` could fail with a permission error, which `load_file` would raise rather than hide.
- `phase_1_5.py` uses `multi_sheet.xlsx` as specified; it has the same 50 rows as the sample, so the replacement shows through `ignored_sheets` rather than the row count. `tests/test_ingest.py::test_valid_upload_replaces_db` checks real replacement with a 2-row CSV.
- Phase 1.2's log says an invalid file "creates no database"; that still holds when no database exists yet, and now an existing one is kept.
- Streamlit usage of `load_sample` (first run) is Phase 1.6.

## Tests
`uv run pytest` → `35 passed` (4 new: sample loads; invalid uploads keep `data.db` byte-for-byte; valid upload replaces it with no stray temp files; failed write keeps it).

## Commands to test
Loads the sample, uploads an invalid file (data kept), then a valid one (data replaced).
```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_5
uv run pytest
```
Output:
```
sample: 50 50
invalid upload: Unsupported file type. Upload a .csv or .xlsx file. | rows still 50
valid upload: 50 ['summary'] | rows now 50
leftover files: ['data.db']
```
`uv run python -m dev_test.phase_1_4b` (empty-state session, as in `adk web`) still answers "50 records".
