# Sprint 1 — Phase 1 Execution Log: uv Project and Dependency Setup

## What was done
- Created the standalone uv project `spreadsheet-qa/` (Python 3.12, `--no-workspace`) and removed the generated `main.py`.
- Added dependencies: `google-adk`, `streamlit`, `pandas`, `openpyxl`, `python-dotenv`. `pyproject.toml` + `uv.lock` replace `requirements.txt`.
- Created the design §3 skeleton: empty `app.py`, `data_agent/{__init__,agent,tools,ingest,sessions}.py`, `sample_data/`, and `.streamlit/config.toml` (`[server] maxUploadSize = 10`).
- Added `spreadsheet-qa/.env` with a placeholder `GOOGLE_API_KEY`. It is already ignored by the root `.gitignore` (line 153), so `.gitignore` was not changed.
- Generated sample data in `sample_data/` with a throwaway script (deleted afterwards, seeded with `numpy` seed 42):
  - `sales.csv`: 50 rows, columns `order_id`, `order date`, `category`, `sales_amount`, `revenue`
  - `sales.xlsx`: same data, one sheet
  - `multi_sheet.xlsx`: `sales` sheet plus a `summary` sheet (totals per category)
  - Invalid files: `notes.txt`, `empty.csv` (0 bytes), `headerless.csv` (data rows only, no header), `corrupt.xlsx` (text renamed to `.xlsx`)
- Ticked all Phase 1 items in `dev_docs/sprints/sprint1.md`.

## Files created/modified
- New: everything under `spreadsheet-qa/` (except `.venv/` and `.env`, which are git-ignored), `dev_docs/execution_logs/sprint_1_1.md`
- Modified: `dev_docs/sprints/sprint1.md` (checkboxes)

## Assumptions and limitations
- **Project location:** following the sprint text literally, the project lives in `spreadsheet-qa/` inside this repo, not at the repo root. If you'd rather have it at the root, the contents can be moved up.
- **API key:** `.env` holds a placeholder. Replace `your-api-key-here` with a real key before Phase 4. Gemini access was not tested in this phase.
- Pinned versions came from the resolver: `google-adk>=2.12.0`, `pandas>=3.0.6`, `streamlit>=1.66.0`, `openpyxl>=3.1.5`, `python-dotenv>=1.2.4`.
- `pyproject.toml` still has uv's default description, and `spreadsheet-qa/README.md` is the generated empty one.
- `sales_amount` values are whole numbers (random ints); `revenue` has 2 decimals.
- Nothing was staged or committed.

## Commands to test
Confirms the dependencies import.
```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_1
```
