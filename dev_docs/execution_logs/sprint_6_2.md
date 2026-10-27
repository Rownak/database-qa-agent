# Sprint 6 — Phase 6.2 Execution Log: Verification

## Summary
The multi-file behaviour was checked in a real browser (Playwright + Chrome) against the Streamlit UI in **local mode** and in **API mode** (local `adk api_server` plus a second UI with `API_URL`). All 25 checks pass in both modes. One bug was found and fixed (the Documents list lagged one rerun behind a document upload). `uv run pytest`: **334 passed**. Nothing was deployed.

## What was checked
The browser script is `db-doc-qa/dev_test/phase_6_2_browser.py` (not a project dependency; see its docstring). It calls the real Gemini model. New fixtures `sample_data/database/customers.csv` and `orders.csv` give a real join (expected answer: Grace Hopper, 499.99).

| Area | Result |
|---|---|
| Sample on load | table `data`, 60 rows × 11 columns |
| Single file | uploading `sales.csv` replaces the sample; one table `sales`; "how many records" answers 50 |
| Three files | `sales`, `customers`, `orders`; the cross-table question returns Grace Hopper / 499.99 and the expander shows the JOIN |
| Limit | a fourth new file shows "Up to 3 data files per session. Remove one first." and changes nothing; re-uploading `orders.csv` (same name, different content) replaces it in place at three files |
| Invalid files | `notes.txt` (rejected by the browser's file-type filter), `empty.csv`, `headerless.csv`, `numeric_header.csv`, `duplicate_header.csv`, `corrupt.xlsx`, and a file over 500 KB each show their error and keep the three loaded tables |
| Name collisions | `sales.csv` then `sales.xlsx` gives `sales` and `sales_2`; `Sales.CSV` then replaces `sales.csv` in place, keeping the table name `sales` |
| `multi_sheet.xlsx` | first sheet loaded; warning "only the first sheet was loaded. Ignored sheets: summary" |
| Remove | removing one file keeps the other; removing the last returns the sample |
| Documents | with a data file loaded: remove a document, add `structured.md`, replace it by name, then a policy question is answered with a `shipment_delay_policy` citation, and the data file is still loaded |

**API mode:** `uv run adk api_server --port 8000` and `API_URL=http://localhost:8000 uv run streamlit run app.py --server.port 8502`. The same script passed unchanged. The backend log shows `data built from upload, 3 table(s), 61 rows`, so the `uploads` state key reaches the backend and all tables are built there. `dev_test/phase_6_2.py` does the round trip by hand: it creates a session with `customers.csv` and `orders.csv` in `uploads`, asks the cross-table question, and prints the `run_sql` call (a `JOIN` on `customer_id`) and the answer (Grace Hopper, $499.99).

## Bug found and fixed
**The document list did not update right after a document upload.** `_render_documents()` is drawn above the document uploader, and `_handle_doc_upload` ran after it without a rerun, so a newly added or replaced document showed only on the next interaction. It existed since Sprint 3 (the AppTest checks looked at session state, not the page). Data files are not affected, because their uploader is handled before their list is drawn.
- **Fix** (`app.py`): `_handle_doc_upload` calls `st.rerun()` after a successful change. The existing `last_doc_upload` guard stops it from looping.
- **Regression test** (`tests/test_app_docs.py::test_add_a_document`): the new document's name must be in the rendered markdown in the same run. I confirmed it fails without the fix and passes with it.

## Other findings (no code change)
- Streamlit's file uploader has its own "Remove <file>" button, so a browser test must match the app's Remove buttons with `exact=True`. That was a test-script mistake, not an app bug.
- After an invalid `.txt` upload, the browser's own "text/plain files are not allowed" message stays next to the uploader until the next file is chosen. Existing Streamlit behaviour.

## Tests
`uv run pytest`: **334 passed** (333 after Phase 6.1, plus the SQL join over `customers.csv` / `orders.csv` in `test_tools.py`). The regression assertion in `test_add_a_document` is part of the existing test.

## Files
- Modified: `app.py`, `tests/test_app_docs.py`, `tests/test_tools.py`, `dev_docs/sprints/sprint6.md`
- Created: `sample_data/database/customers.csv`, `sample_data/database/orders.csv`, `dev_test/phase_6_2.py`, `dev_test/phase_6_2_browser.py`, `dev_docs/execution_logs/sprint_6_2.md`

## Commands
```
cd db-doc-qa
uv run pytest                                           # 334 passed

# local mode browser run
uv run streamlit run app.py --server.port 8501 --server.headless true
UI_URL=http://localhost:8501 uv run --no-project --with playwright python -m dev_test.phase_6_2_browser

# API mode
uv run adk api_server --port 8000
API_URL=http://localhost:8000 uv run streamlit run app.py --server.port 8502 --server.headless true
UI_URL=http://localhost:8502 uv run --no-project --with playwright python -m dev_test.phase_6_2_browser
uv run python -m dev_test.phase_6_2                     # round trip against the local API server
```
(PowerShell: set `$env:UI_URL` / `$env:API_URL` instead of the inline variables.)

## Limitations
- The browser script covers the checks in the sprint plan, not every wording of the answers; the model's text varies between runs.
- The hosted app is still the old version. `phase_6_2_browser.py` takes `UI_URL`, so Phase 6.3 can reuse it against the hosted UI.
