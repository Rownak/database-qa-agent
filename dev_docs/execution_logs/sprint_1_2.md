# Sprint 1 — Phase 2 Execution Log: File Ingestion (`ingest.py`)

## What was implemented
`spreadsheet-qa/data_agent/ingest.py` with `load_file(uploaded_name, file_bytes, session_dir)`, which returns an `IngestResult` or an `IngestError`.

- **Validation, in order:** extension → 10 MB size → readable (CSV `utf-8-sig`, XLSX `openpyxl`) → not empty after dropping fully empty rows/columns → valid header row (see *Revision 1* below). Error texts follow design §5.
- **XLSX:** loads `sheet_names[0]`; the rest go in `ignored_sheets`. `sheet_name` is also returned so the UI can show "Loaded sheet 'X'".
- **Column cleaning:** trim, lowercase, non-alphanumeric runs → `_`, strip edge `_`, `c_` prefix for leading digits. Duplicates are no longer renamed (rejected instead, see *Revision 1*). `column_map` maps cleaned → original.
- **Type inference:** int/float/bool dtypes kept (`INTEGER`/`REAL`); other columns try `to_numeric`, then `to_datetime` (stored as ISO text, labeled `date`), else `TEXT`. All-or-nothing per column.
- **Load:** `df.to_sql("data", ...)` into `<session_dir>/data.db`, connection closed. `schema_summary` has one line per column: `name (type) — original name`.
- **Result fields:** `db_path`, `row_count`, `column_count`, `column_map`, `schema_summary`, `ignored_sheets`, `preview` (first 10 rows, cleaned names), `sheet_name`.

## Key decisions
- **No side effects on failure:** the session dir and `data.db` are only created after all validation passes; a failed `to_sql` deletes the partial file.
- **Raw headers re-read:** pandas renames duplicate headers to `a.1` before we see them, which would hide real duplicates and corrupt the original-name map. The first row is re-read with `header=None, dtype=str` and used as the labels.
- **Re-upload:** an existing `data.db` in the session dir is replaced.
- **Dates:** Excel datetime cells and date-like strings are stored as `YYYY-MM-DD`, or `YYYY-MM-DD HH:MM:SS` if any value has a time part.

## Files
- Modified: `spreadsheet-qa/data_agent/ingest.py`, `spreadsheet-qa/sample_data/headerless.csv` (see below), `dev_docs/sprints/sprint1.md`, `dev_docs/design/design.md`, `dev_docs/design/project_description.md`
- Created (revision 1): `spreadsheet-qa/sample_data/numeric_header.csv`, `spreadsheet-qa/sample_data/duplicate_header.csv`
- Created: `dev_docs/execution_logs/sprint_1_2.md`

## Assumptions, ambiguity, limitations
- **`headerless.csv` changed.** The Phase 1 version had data in its first row, so pandas treated those values as headers and the "No header row detected." rule (all `Unnamed: N`) could not trigger. It now starts with a blank header line (`,,,,`) followed by the two data rows. A file whose first row is real data (e.g. `numeric_header.csv`) is now caught by the numeric-header rule from revision 1.
- Empty files, and files with a header but no data rows, both give "The file has no data rows or no columns." (design §5 gives no separate text).
- A header made only of symbols (cleans to an empty name) is reported as "Column N has no header."
- Size error text ("File is too large. The limit is 10 MB.") was not specified in the design; I chose it.
- A column that is entirely empty is dropped, so it does not appear in the table.
- No currency/locale cleanup (design backlog).

## Quick check results (every sample file)
| File | Result |
|---|---|
| `sales.csv` | 50 rows, 5 cols; types INTEGER, date, TEXT, INTEGER, REAL; DB row count 50 |
| `sales.xlsx` | 50 rows, 5 cols, same types |
| `multi_sheet.xlsx` | 50 rows, 5 cols, `ignored_sheets=['summary']` |
| `notes.txt` | "Unsupported file type. Upload a .csv or .xlsx file." |
| `empty.csv` | "The file has no data rows or no columns." |
| `headerless.csv` | "No header row detected." |
| `corrupt.xlsx` | "Could not read the file: File is not a zip file." |

Also checked: >10 MB bytes rejected; `1a` → `c_1a`; blank middle header → "Column 2 has no header."; bad UTF-8 → "Could not read the file: …". No session dir was created for any failure.

## Commands to test
Loads `sales.csv` and prints the counts and schema summary.
```
cd spreadsheet-qa
uv run python -m dev_test.phase_1_2
```

---

## Revision 1 — Header row must be strings and unique

**Requirement (from the developer):** the uploaded file must have a header row; every header must be a string and unique; duplicates are not allowed and produce an error.

### Changes
- **Docs:** `design.md` §5 step 5 rewritten (also the collision bullet and §8 checklist); `project_description.md` (cleaning description, header rule, acceptance criterion); `sprint1.md` Phase 2 items and Phase 6 file list updated.
- **Code (`ingest.py`):** `_check_headers` now runs, in order: all-`Unnamed` → "No header row detected."; blank → "Column N has no header."; numeric → "Column N header must be text, not a number: '1001'."; duplicate → "Duplicate column name: 'A' (column i) and 'B' (column j)." The old `_clean_column_names` with `_2`/`_3` suffixes was replaced by a single `_clean_name`, since names are guaranteed unique after validation.
- **Sample files:** added `numeric_header.csv` (data in the first row) and `duplicate_header.csv` (`Revenue` and `revenue`).

### Decisions
- "String" means the header text is not a plain number (`1001`, `-3`, `2024`, `2.5`, `1e3`). Everything in a CSV is text on disk, so the check looks at the content; XLSX numeric header cells are caught the same way. Consequence: year-style headers such as `2024` are rejected. Headers like `1a` or `2024_sales` are fine (cleaned to `c_1a`, `c_2024_sales`).
- "Unique" is judged after cleaning, so `Order ID` and `order_id` clash (both become `order_id`). Rejecting beats silently renaming, and it matches the "no duplicate column name" rule.
- Checks are ordered so the most basic problem is reported first.

### Quick check results
| File / input | Result |
|---|---|
| `numeric_header.csv` | "Column 1 header must be text, not a number: '1001'." |
| `duplicate_header.csv` | "Duplicate column name: 'Revenue' (column 3) and 'revenue' (column 4)." |
| `a,b,a` | "Duplicate column name: 'a' (column 1) and 'a' (column 3)." |
| `Order ID,order_id` | "Duplicate column name: 'Order ID' (column 1) and 'order_id' (column 2)." |
| `a,-3` / `name,2024` / XLSX header `2024` | numeric-header error |
| `1a,b` | loads as `c_1a`, `b` |
| All earlier sample files | unchanged results; no session dir created on any failure |
