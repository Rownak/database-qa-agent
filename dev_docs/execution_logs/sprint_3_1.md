# Sprint 3 — Phase 3.1 Execution Log: Rename, Dependencies, Sample Data

## Summary
The project folder is now `db-doc-qa/`, `pypdf` and `semchunk` are installed (`fpdf2` for development only), and the shipment scenario is the default sample: `shipments.csv` plus three policy documents. The document test fixtures for Phase 3.2 are in place. `sales.csv` is now a test fixture. All 78 tests pass.

## What changed

**Rename**
- `git mv spreadsheet-qa db-doc-qa`. Git records the files as renames, so their history is kept.
- `pyproject.toml`: `name = "db-doc-qa"`, with a real description.
- Paths updated in `CLAUDE.md`, `README.md`, `dev_docs/deploy/deploy.md`, `scripts/stage_ui_context.py` and `dev_test/phase_2_6.py`.
- Not changed, on purpose: the Sprint 1–2 logs and plans, `design_v1.md`, and a historical tip in `dev_docs/git_commands.md`.
- The old `.venv` was deleted and rebuilt by `uv`, because a virtual environment stores absolute paths.

**Dependencies**
- `uv add pypdf semchunk` installed `pypdf` 6.21.1 and `semchunk` 4.1.1.
- `uv add --dev fpdf2` installed `fpdf2` 2.8.9. Pillow comes with it and is used to draw the image-only fixture.
- `pypdf==6.21.1` and `semchunk==4.1.1` are pinned in `data_agent/requirements.txt` (backend) and `requirements-ui.txt` (UI). The UI needs them for validating documents and showing the chunk count.

**Table sample: `data_agent/sample_data/shipments.csv`**
- 60 rows (IDs 12301–12360) and 11 columns, as in project description §8. The loader infers `shipment_id` and `delay_days` as `INTEGER`, the four date columns as `date`, and the rest as `TEXT`.
- The rows are internally consistent with a fixed as-of date of 2026-10-15:
  - Delivered shipments are at their destination.
  - In Transit shipments have an expected date that has not passed yet.
  - Delayed and held shipments have `delay_days` = 2026-10-15 − expected date.
- Seeded demo rows:

| ID | Type | Status | `delay_days` | Purpose |
|---|---|---|---|---|
| 12345 | Express | Delayed | 3 | Past the Express threshold of 2 days, so a Tier 2 escalation |
| 12346 | Standard | Delayed | 1 | Within the 2-day grace period, so monitor and notify |
| 12347 | International | Customs Hold | 6 | **Between the conflicting 5- and 7-day thresholds**, so the conflict changes the answer |
| 12348 | Standard | In Transit | 0 | On schedule, which contradicts a user who says it is delayed |
| 12349 | Refrigerated | Delayed | 2 | No Refrigerated rule exists, so the general rule (§2.4) applies |
| 99999 | — | — | — | Not present, to test a record that is not found |

**Policy documents**
- The source text is in `scripts/sample_docs/*.txt`. The documents use numbered sections (`3`, `3.2`) and `(a)` clauses. They contain no Refrigerated section, and their thresholds match the seeded rows.
  - `shipment_delay_policy`: definitions, a general rule for types without their own rule (§2.4), rules for Standard, Express and International shipments (§3; international threshold **5 days**), escalation tiers and contacts (§4), and exceptions (§5).
  - `customer_communication_guidelines`: when to notify customers, what customers can do (reschedule, cancel, claim), and compensation.
  - `international_shipping_procedures`: required documents, customs holds, and international delays (threshold **7 days**). It is dated an earlier effective date than the delay policy, which gives later phases a reason to explain the conflict.
- `scripts/make_sample_pdfs.py` (using `fpdf2`) renders the first two documents to `data_agent/sample_data/docs/*.pdf`, with bold headings and indented clauses, and copies the third as `.txt`. The demo set therefore covers both supported formats.
- Two sections cross a page break:
  - In `shipment_delay_policy.pdf` (3 pages), the heading **3.2 Express Shipments** is at the bottom of page 1 and its text is on page 2.
  - In the guidelines (2 pages), the heading **4.1 Service credit** is on page 1 and its clauses are on page 2.

  Phase 3.2 can use these to test page ranges.

**Test fixtures (`sample_data/`)**
- The script also builds the PDF fixtures, so every binary file can be rebuilt:
  - `scanned.pdf`: an image only; `pypdf` extracts the empty string.
  - `encrypted.pdf`: protected with a user password using `pypdf`.
  - `oversize_pages.pdf`: 21 pages.
- Text fixtures:
  - `empty.txt`: 0 bytes.
  - `latin1.txt`: French text in Latin-1, which fails to decode as UTF-8.
  - `no_headings.txt`: 4 plain paragraphs.
  - `injection.txt`: a structured document whose §2 asks the model to ignore its instructions, reveal the system prompt and run `DELETE FROM data`.

**Default sample switch**
- `SAMPLE_FILE` now points to `shipments.csv`. `sales.csv` was moved with `git mv` to `sample_data/sales.csv`.
- Docstrings in `app.py` and `ingest.py` were updated. The old `dev_test` scripts (phases 1.2, 1.3, 1.4, 1.7 and 2.6) now read `sample_data/sales.csv`.
- `scripts/stage_ui_context.py` copies `shipments.csv` into the UI build context instead of `sales.csv`. This was needed for `test_stage_ui_context.py` to pass.

**Tests**
- `conftest.py`: a new `SALES` constant. The `loaded` and `ctx` fixtures, which the tool tests use, load `sales.csv` explicitly, so tests that depend on its known values (50 rows, `sales_amount`, 5 categories) are unchanged.
- Updated for the new default:
  - `test_ingest.py`: `load_sample` gives 60 rows, and all fixtures are read from `sample_data/`.
  - `test_agent.py`: the sample schema starts with `shipment_id`.
  - `test_app.py` and `test_app_api.py`: the UI shows `shipments.csv (sample)` and `60 rows × 11 columns`.
- New: `test_shipments_sample_schema_and_seeded_rows` checks the columns, their types, and the five seeded rows, and confirms that 99999 is absent.

## Files
- **Renamed:** `spreadsheet-qa/` → `db-doc-qa/` (all files), and `data_agent/sample_data/sales.csv` → `sample_data/sales.csv`.
- **Modified:**
  - `CLAUDE.md`, `README.md`, `dev_docs/deploy/deploy.md`, `dev_docs/sprints/sprint3.md` (tasks ticked)
  - `pyproject.toml`, `uv.lock`, `data_agent/requirements.txt`, `requirements-ui.txt`
  - `data_agent/ingest.py`, `app.py`, `scripts/stage_ui_context.py`
  - `tests/conftest.py`, `tests/test_ingest.py`, `tests/test_agent.py`, `tests/test_app.py`, `tests/test_app_api.py`
  - `dev_test/phase_1_2.py`, `phase_1_3.py`, `phase_1_4.py`, `phase_1_7.py`, `phase_2_6.py`
- **Created:**
  - `data_agent/sample_data/shipments.csv` and `data_agent/sample_data/docs/` (2 PDFs, 1 TXT)
  - `scripts/sample_docs/` (3 TXT sources) and `scripts/make_sample_pdfs.py`
  - `sample_data/` fixtures: `scanned.pdf`, `encrypted.pdf`, `oversize_pages.pdf`, `empty.txt`, `latin1.txt`, `no_headings.txt`, `injection.txt`
  - `dev_test/phase_3_1.py`

## Notes and limitations
- **Your shell environment:** `VIRTUAL_ENV` still points to the old `spreadsheet-qa\.venv`, so `uv` prints a warning and ignores it. Open a new terminal, or reactivate `db-doc-qa\.venv`, to make it go away. Also update any VS Code interpreter setting that names the old path.
- **The `shipments.csv` generator was not committed.** It was a one-off script; the CSV is the source of truth, and the table above documents the seeded rows.
- **The UI build only stages `shipments.csv` for now.** It does not include the sample documents yet; Phase 3.6 adds them when the UI starts using documents.
- **Phase 2.6 checks:** `dev_test/phase_2_6.py` (the Sprint 2 browser checks against the deployed UI) still expects `sales.csv` to be the default sample. Phase 3.7 replaces those checks. The deployed services were not changed in this phase.
- **README:** it still describes the sales example questions. Phase 3.7 updates it.

## How to run
From `db-doc-qa/`:

```bash
uv sync
uv run pytest                                # 83 passed
uv run python -m dev_test.phase_3_1          # sample schema, document pages, fixtures, semchunk smoke test
uv run python scripts/make_sample_pdfs.py    # only after editing scripts/sample_docs/*.txt
```

---

## Follow-up: Markdown documents and the `sample_data/` reorganization

Two tasks added to Phase 3.1 after the first pass. Tests: **83 passed** (`uv run pytest`, 5 new in `tests/test_sample_data.py`).

### Layout

```
db-doc-qa/
├── data_agent/sample_data/default/      # loaded at session start; ships with adk deploy
│   ├── shipments.csv
│   └── docs/                            # shipment_delay_policy.pdf, customer_communication_guidelines.pdf,
│                                        # international_shipping_procedures.txt
└── sample_data/                         # test fixtures only
    ├── database/                        # sales.csv, sales.xlsx, multi_sheet.xlsx, invalid CSV/XLSX files,
    │                                    # notes.txt (unsupported table type), the HR attrition CSV
    └── documents/                       # scanned.pdf, encrypted.pdf, oversize_pages.pdf, empty.txt, latin1.txt,
                                         # no_headings.txt, injection.txt, structured.md
```

- Tracked files were moved with `git mv`. The files added earlier in this phase were moved with `mv` and will show as renames once staged.
- `data_agent/ingest.py`: `SAMPLE_FILE` now points to `default/shipments.csv`. Two constants were added: `SAMPLE_DIR` and `SAMPLE_DOCS_DIR` (the default documents folder, used by Phase 3.2).
- `tests/conftest.py`: `SAMPLES` was split into `DB_SAMPLES` and `DOC_SAMPLES`. The four test files that used `SAMPLES` now use `DB_SAMPLES`.
- Paths also updated in `scripts/make_sample_pdfs.py` (PDFs go to `default/docs/`, fixtures to `documents/`), `scripts/stage_ui_context.py`, and the `dev_test` scripts for phases 1.2, 1.3, 1.4, 1.5, 1.7, 2.2, 2.3, 2.6 and 3.1.
- `Dockerfile.ui` needed no change: it copies the whole `data_agent/sample_data/` folder. The staged UI context now contains `default/shipments.csv`.

### Markdown support
- `.md` is accepted as a document type, treated like `.txt` (UTF-8, 1 MB, `#` lines as headings, where the heading level is the number of `#`). There is no ingestion code yet, so Phase 3.2 is where this is implemented.
- Documentation updated: `project_description.md` (inputs table, uploader note, out-of-scope list, `doc_type`, validation message, chunking, test fixtures, backlog), `design.md` (project tree, validation steps, heading rules, uploader types, test list) and `sprint3.md` (Phase 3.2 validation, Phase 3.6 uploader).
- New fixture `sample_data/documents/structured.md`: `#`/`##`/`###` headings, `(a)` clauses and a bullet list.

### Notes
- `README.md` still shows `data_agent/sample_data/sales.csv` in its workflow diagram; Phase 3.7 rewrites the README.
- The default document set has no `.md` file, so the Markdown path is covered by the fixture only. Say if you want one of the sample policies converted to `.md`.
