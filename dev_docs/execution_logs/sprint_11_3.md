# Sprint 11 — Phase 11.3: Author the Suites

## Summary
All initial cases of design §3.1 now exist as golden YAML and build cleanly: **32 cases + 28 variants = 60 ADK eval cases** in six suites. Coverage is complete for rules 1-10 and 12-14 (rule 11 stays the known gap). Offline only; no agent change. `uv run pytest`: **578 passed, 2 skipped** (the live gate).

| Suite | Cases | Variants | Match | Critical |
|---|---|---|---|---|
| `unit_routing` | 9 | 12 | EXACT | `edge_refuse_write` |
| `unit_hybrid` | 10 | 5 | IN_ORDER | none |
| `memory` | 4 | 2 | IN_ORDER | `mem_refusal_persists` |
| `adversarial` | 7 | 9 | `exact/` + `in_order/` | `adv_injected_document`, `adv_urgent_write`, `adv_sql_in_question`, `adv_fabricated_section` |
| `regression` | 1 (`reg_empty_state_build`) | 0 | EXACT | none |
| `regression_cache` | 1 (`reg_followup_not_cached`) | 0 | EXACT | none |

## How it was done
- **Expected values come from the sources, not the agent.** Data values use `contains_sql` (location of 12345, delayed count, Express ids, row/column counts via `pragma_table_info`). Document fragments ("2 days", "Tier 2", "4 days", "5 days"/"7 days") and sections were copied from the policy text; a test checks every golden section number exists in the sample documents (including `injection.txt` 1.1).
- **Memory exclusions** (non-Express delayed / in-transit ids) are literal lists in `memory.yaml`, because the schema has no `excludes_sql`; a test recomputes them from the data and fails if they drift.
- **Adversarial split:** `adv_sql_in_question` and `adv_out_of_scope_value` are `match: IN_ORDER` + `trajectory: any`; the rest are EXACT. The builder writes `adversarial/exact/` and `adversarial/in_order/` with one manifest.
- **Cache suite:** `configs/eval/cache_session.yaml` is `smoke.yaml` plus `cache.response: {enabled: true, scope: session}`. `reg_followup_not_cached` asks the same question twice and expects `run_sql` both times (a cache hit makes no tool call, which EXACT rejects).
- **Review (design §4.3):** one behaviour per case; every failure-mode rule has an edge/adversarial case; redundancy warning cleared by giving `unit_db_group_by_type` (`GROUP BY`) and `unit_db_table_shape` (`COUNT`) different tool fragments; critical set equals the design's ⚑ list.
- `citation_grounded` has no YAML field: it runs on every answer, so `adv_fabricated_section` relies on it plus `contains: ["not"]` (the design's check).

## Builder change
Variants now reword the **last** turn (identical for single-turn cases). This lets the multi-turn critical case `mem_refusal_persists` have variants of the refusal turn; the "variants only on single-turn" rule from 11.1 was removed and its test replaced.

## Files
- Created: `evals/golden/{memory,adversarial,regression,regression_cache}.yaml`, `configs/eval/cache_session.yaml`, generated `evals/adk/{memory,adversarial/{exact,in_order},regression,regression_cache}/`, `tests/test_adk_suites.py` (10 tests), `dev_test/phase_11_3.py`.
- Modified: `evals/golden/{unit_routing,unit_hybrid}.yaml`, generated `unit_*` sets, `evals/adk/{build,golden}.py` (variants), `tests/test_adk_evalsets.py` (now checks every folder), `tests/test_adk_golden.py` (full-set coverage test), `dev_docs/sprints/sprint11.md`.

## Assumptions and limits
- All fragments are unverified against live output (no Gemini run until 11.5); a failing case is a finding, not a reason to edit the golden. Weak or risky checks to watch: `contains: ["not"]`, `excludes: ["kg", "$", "dropped"]`, the `GROUP BY` / `COUNT` query fragments, and `contains: ["corrupt.xlsx", "could not be loaded"]` (depends on the model repeating the notice).
- `edge_record_not_found` has no "invented location" exclusion (no concrete forbidden value exists).
- `mem_*` runs score each turn; turns with no golden tools in `mem_constraint_retention` (turn 1) have no answer check.

## Commands
From `db-doc-qa/`:
```bash
uv run python -m evals.adk.build            # regenerate; prints warnings and the coverage matrix
uv run python -m evals.adk.build --check    # exit 1 if a committed file is stale
uv run python -m dev_test.phase_11_3        # cases per suite: layer, rules, critical, tools per turn
uv run pytest                               # 578 passed, 2 skipped
```
