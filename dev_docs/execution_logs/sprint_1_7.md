# Sprint 1 — Phase 1.7 Execution Log: End-to-End Verification

## What was done
Ran every acceptance check through the real code paths: `load_file` for all sample files, and the Streamlit app driven by `AppTest` (real Gemini calls) for the chat, upload and End-session checks. Not run in a browser.

| Check | Result |
|---|---|
| `sales.csv`, `sales.xlsx` | 50 rows × 5 columns |
| `multi_sheet.xlsx` | 50 × 5, ignored sheet `summary` shown as a warning |
| `notes.txt`, `empty.csv`, `headerless.csv`, `numeric_header.csv`, `duplicate_header.csv`, `corrupt.xlsx` | each gives its specific error; no `data.db` created |
| "How many records are there?" | 50 (`get_table_info`) |
| Average sales amount | 279.34, equals pandas; `run_sql` `AVG` query |
| Highest total revenue category | Clothing, equals pandas; `GROUP BY` query shown |
| Profit margin | explains cost/profit columns are missing, no number |
| "Delete all rows" | refused (read-only); count still 50 |
| `Source:` line | **Failed at first** on the profit-margin and delete answers (see below); fixed, all answers now end with it |
| End session | folder deleted, new session id |
| Upload resets chat | valid upload: chat cleared, data replaced; invalid upload: error shown, sample and chat kept |
| Restart empties `adk_sheet_qa/` | stale folder removed on first run of a fresh process (cache cleared in test) |

## Failure found and fixed
- Refusals and "missing data" answers had no `Source:` line, breaking "every answer ends with `Source:`".
- Fix: `agent.py` rule 9 now says every answer, including refusals and missing-data explanations, ends with `Source:`, using `Source: none (no data queried)` when nothing was queried. Re-run confirmed both answers now end with it.

## Files
- Created: `spreadsheet-qa/dev_test/phase_1_7.py`, this log
- Modified: `spreadsheet-qa/data_agent/agent.py` (instruction rule 9), `spreadsheet-qa/tests/test_app.py` (upload flow, purge on start), `dev_docs/sprints/sprint1.md`

## Notes / limitations
- Answers come from an LLM, so wording and the `Source:` line can vary between runs; re-run `phase_1_7` if a check looks off.
- Browser look and feel (split layout, file picker dialog) was not checked; do a quick manual pass with `uv run streamlit run app.py`.
- The "commit the working demo" task is left unticked: commits are yours to make.

## Tests
`uv run pytest` → `45 passed` (2 new: upload flow valid/invalid, purge on app start).

## Commands to test
```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_7   # needs GOOGLE_API_KEY; prints every check above
uv run pytest
uv run streamlit run app.py
```
