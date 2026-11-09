# Sprint 11 — Phase 11.1: Golden YAML, Builder and Coverage

## Summary
Golden cases are now written in YAML (`db-doc-qa/evals/golden/`) and generated into committed ADK eval sets by `evals/adk/build.py`. Sprint 10's `routing` and `hybrid` sets are migrated (9 cases, layer-prefixed ids) into `unit_routing` and `unit_hybrid`, and the old folders are removed. The builder validates the golden files, computes data facts from the bundled `shipments.csv`, and prints the rule coverage matrix. Everything is offline. `uv run pytest`: **540 passed, 2 skipped** (the 2 are the opt-in live gate). No agent code was changed and nothing was committed.

## What was implemented
- **Schema** `evals/adk/golden.py` (pydantic, `extra="forbid"`): suite (`suite`, `instruction_sha`, `match`, `description`, `cases`), case (`id`, `layer`, `rules`, `critical`, `state`, `variants`, `trajectory: any`, `match`, `turns`), turn (`q`, `tools`, `contains`, `contains_sql`, `excludes`, `sections`). Ids must carry the layer prefix (`unit_`, `edge_`, `mem_`, `adv_`, `reg_`); rules must be distinct numbers 1-14. Tools are written as `- run_sql: {query: "12345"}`.
- **State presets** `evals/golden/states/{default,no_docs,injection_docs,corrupt_upload}.yaml`. Files are inlined as base64 by the builder (`@sample_docs` = the three bundled documents). `default` gives `{}`.
- **Builder** `uv run python -m evals.adk.build` (`--check` writes nothing and exits 1 if a committed file is stale). Per suite it writes `<suite>/<suite>.test.json`, `test_config.json` and `manifest.json`:
  - one invocation per turn; tool fragments in `intermediate_data.tool_uses`
  - answer checks as typed invocation `rubrics`: `ANSWER_CONTAINS`, `ANSWER_EXCLUDES`, `EXPECTED_SECTION` (value `source|section`)
  - variants as `<id>__vN`, reported as one `group` in the manifest
  - `contains_sql` run read-only on the bundled data; the first column of each row becomes a `contains` fragment
  - the output is validated with ADK's `EvalSet` before it is written
- **Validation** (errors stop the build; all are listed together): empty tool list without `trajectory: any` under IN_ORDER; `trajectory: any` under EXACT or with no empty turn; unknown tool or tool argument; duplicate ids (also across suites and against variant ids); unknown preset; variants on a multi-turn case; a fragment both required and forbidden; unusable `contains_sql`. **Warnings:** redundant cases (same layer, rules, match type and golden tools with fragments) and a stale `instruction_sha`.
- **Coupling:** `current_instruction_sha()` hashes `INSTRUCTION` plus the signature and docstring of both tools (the same inputs the response cache fingerprints). The golden files store the first 12 hex digits (`27076fc64606`).
- **Coverage matrix:** rule x layer counts for rules 1-14; rule 11 is printed as a known gap. `coverage_gaps()` returns the uncovered rules and the failure-mode rules (4, 5, 6, 7, 8, 10, 12, 13, 14) with no edge or adversarial case.
- **Migration:** ids renamed as in design §3 (`db_location` -> `unit_db_lookup_by_id`, `db_count_delayed` -> `unit_db_count_by_status`, `doc_express_threshold` -> `unit_doc_express_threshold`, `refuse_write` -> `edge_refuse_write`, `hybrid_delayed_express` -> `unit_hybrid_record_to_policy`, `conflict_international` -> `edge_conflicting_docs`, `no_policy_refrigerated` -> `edge_no_type_rule`, `record_not_found` -> `edge_record_not_found`, `threshold_first` -> `unit_hybrid_policy_then_filter`). Questions and golden tool fragments are unchanged.

## Key decisions and assumptions
- **Match type lives in the YAML** (`match` on the suite, optional override per case), because the schema in the sprint list had no field for it. A suite with both types is split into `exact/` and `in_order/` folders automatically, with files named `<suite>_exact.test.json`; this is how 11.3 will build `adversarial`. "A match type that does not fit the folder" is implemented as the two cases above (`trajectory: any` under EXACT; empty list under IN_ORDER without `any`).
- **`trajectory: any`** is required whenever a turn has an empty tool list under IN_ORDER (this includes the memory turns that expect no tool). It does not loosen turns that list tools.
- **Checks added during migration are limited to values the sources prove:** `contains_sql` for the location of 12345 (`Cleveland Sort Facility`) and the delayed count (`9`), and "read-only" for the write refusal (rule 12 text). Document fragments, sections and the other checks of design §3.1 are left to 11.3, which reads the policy text. `edge_refuse_write` is marked critical as in the design table.
- **Rule tags:** `edge_no_type_rule` is `[2, 6]` (the design's YAML example); the others follow the §3.1 table.
- **Negative/edge layers** for the failure-mode check are `edge` and `adversarial`.
- **`test_config.json` still holds the two Sprint 10 metrics.** Registering the four new metrics is Phase 11.2.
- The golden-set coverage test cannot demand every rule yet (only 9 cases exist), so it asserts that the open rules are only 4, 8, 9, 10, 13, 14 and that no failure-mode rule has cases without a negative one. The "every rule has a case" assertion on the full set belongs to 11.3.

## Files
- Created: `evals/adk/golden.py`, `evals/adk/build.py`, `evals/golden/unit_routing.yaml`, `evals/golden/unit_hybrid.yaml`, `evals/golden/states/*.yaml` (4), `evals/adk/unit_routing/` and `evals/adk/unit_hybrid/` (`*.test.json`, `test_config.json`, `manifest.json`), `tests/test_adk_golden.py` (30 tests), `dev_test/phase_11_1.py`.
- Removed: `evals/adk/routing/`, `evals/adk/hybrid/`.
- Modified: `tests/test_adk_evalsets.py`, `tests/test_adk_metrics.py`, `tests/test_adk_eval.py` and `dev_test/phase_10_1.py`, `phase_10_2.py` (new folder names), `README.md` (folder names, build command), `dev_docs/sprints/sprint11.md` (11.1 boxes ticked).

## Current coverage (9 cases; the gaps are expected until 11.3)
Covered: rules 1, 2, 3, 5, 6, 7, 12. Open: 4, 8, 9, 10, 13, 14. Known gap: 11.

## Limitations
- The migrated sets still check mostly tools. Answer rubrics are generated but no metric scores them until 11.2.
- Results of old Sprint 10 live runs (none exist) would reference the old eval-set ids.
- Editing the agent instruction or a tool docstring now makes `test_golden_files_match_the_current_instruction` fail until the golden files are reviewed and `instruction_sha` updated (intended).

## Commands and results
From `db-doc-qa/`:

```bash
uv run python -m evals.adk.build          # regenerate eval sets, print warnings and the coverage matrix
uv run python -m evals.adk.build --check  # exit 1 if a committed file is stale
uv run python -m dev_test.phase_11_1      # build, case counts per suite, coverage matrix
uv run pytest tests/test_adk_golden.py -q # new tests
uv run pytest                             # 540 passed, 2 skipped
```
