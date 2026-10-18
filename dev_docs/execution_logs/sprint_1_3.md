# Sprint 1 — Phase 3 Execution Log: Agent Tools and SQL Safety (`tools.py`)

## What was implemented
`spreadsheet-qa/data_agent/tools.py` with four read-only tools. Each returns `{"status": "success", ...}` or `{"status": "error", "error_message": ...}` and never raises.

| Tool | Behaviour |
|---|---|
| `get_table_info()` | `row_count`, `column_count`, `columns` as `{name, original_name, type}` |
| `get_sample_rows(n=5)` | First `n` rows, `n` clamped to 1–20 |
| `get_column_stats(column)` | Numeric: count, nulls, min, max, mean, median. Text/date: count, nulls, distinct count, top 5 values. Unknown column → error listing valid columns |
| `run_sql(query)` | Pre-check, then `columns`, `rows`, `row_count`, `truncated` |

Supporting pieces:
- `_connect_ro(tool_context)`: opens `file:<db_path>?mode=ro` from `tool_context.state["db_path"]`; callers wrap it in `closing(...)`. A progress handler stops queries after 5 s (reported as "Query timed out after 5 seconds. Simplify the query.").
- `run_sql` pre-check: strip one trailing `;` and reject if another `;` remains; must start with `SELECT`/`WITH`; whole-word, case-insensitive block of `ATTACH PRAGMA INSERT UPDATE DELETE DROP ALTER CREATE`.
- Up to `MAX_ROWS + 1` (101) rows fetched, `truncated` set when over 100; text cells cut to 200 characters (plus `…`); any exception becomes an error dict.

## Key decisions
- **Read-only connection is the real guard**, as in design §6. Confirmed directly: running `WITH x AS (SELECT 1) DELETE FROM data` on a raw read-only connection fails with "attempt to write a readonly database".
- **`tool_context` is keyword-only** (`*, tool_context`) on the three tools that take other arguments, because `get_sample_rows(n=5, tool_context)` is not valid Python with a default before it. ADK passes it by name and omits it from the model-facing schema (checked via `FunctionTool` declarations).
- **Median in SQL** (ordered `LIMIT/OFFSET`), since SQLite has no median and the table can be up to 10 MB.
- **Types come from SQLite's declared column types** (`PRAGMA table_info`, used internally only). Date columns therefore show `TEXT`; the `date` label stays in `schema_summary` in the agent instruction.
- DB path is percent-encoded into the URI so Windows drive letters and folders with spaces or `#` work (tested with a folder named `a b#c`).

## Files
- Modified: `spreadsheet-qa/data_agent/tools.py`, `dev_docs/sprints/sprint1.md` (Phase 3 checkboxes)
- Created: `dev_docs/execution_logs/sprint_1_3.md`

## Assumptions and limitations
- **State contract for Phase 5:** the ADK session state must contain `db_path` (required) and `column_map` (used by `get_table_info` for original names; falls back to the cleaned name if missing), plus `schema_summary` for the instruction.
- The keyword block is a plain word match, so a string literal such as `WHERE note = 'create'` is also rejected. Accepted trade-off for a simple pre-check.
- A bare `DELETE FROM data` is rejected by the "must start with SELECT/WITH" rule, so its message is that one, not the keyword message.
- Only table `data` is described; no other tables exist.

## Quick check results (`sales.csv`, fake context `SimpleNamespace(state={...})`)
- Tools return correct data: 50 rows / 5 columns; `sales_amount` min 50, max 488, mean 279.34, median 283.5; `category` has 5 distinct values (Toys 12 first); unknown column lists valid names; `n=99` → 20 rows.
- Rejected, with clear errors: `DELETE FROM data`, `WITH x AS (SELECT 1) DELETE FROM data`, `SELECT 1; DROP TABLE data`, `PRAGMA ...`, `drop table data`, `SELECT 1 ... /* attach */`, `SELECT * FROM data; ;`.
- SQL errors (`no such table`, `incomplete input`) returned as error dicts.
- 500-row recursive query → 100 rows, `truncated: true`; 500-char cell → 201 characters.
- Infinite recursive query stopped by the timeout (1 s when lowered for the test).
- No loaded file → "No file has been loaded for this session."
- Row count was 50 before and after all attempts.

## Commands to test
```
cd spreadsheet-qa
uv run python -c "
from pathlib import Path; from types import SimpleNamespace
from data_agent.ingest import load_file; from data_agent import tools
p = Path('sample_data/sales.csv'); r = load_file(p.name, p.read_bytes(), Path('tmp_session'))
ctx = SimpleNamespace(state={'db_path': str(r.db_path), 'column_map': r.column_map})
print(tools.run_sql('WITH x AS (SELECT 1) DELETE FROM data', tool_context=ctx))
print(tools.run_sql('SELECT COUNT(*) FROM data', tool_context=ctx))
"
rm -rf tmp_session
```
