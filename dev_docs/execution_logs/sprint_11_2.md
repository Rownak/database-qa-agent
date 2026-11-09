# Sprint 11 — Phase 11.2: Answer and Citation Metrics

## Summary
Four new gating metrics (`answer_contains`, `answer_excludes`, `section_retrieved`, `citation_grounded`) are in `evals/adk/metrics.py` and registered, with `tool_args_contain`, in every suite's `test_config.json` (written by the builder). Offline only. `uv run pytest`: **555 passed, 2 skipped**.

## Implementation
- **Adapter:** `to_turn_result(invocation)` gives the harness `TurnResult` (answer without thought parts; calls with their responses). `to_eval_case(golden_invocation)` turns the rubrics into `Expected` (`ANSWER_CONTAINS` -> `answer_contains`, `EXPECTED_SECTION` `source|section` -> `sections`).
- **Metrics:** reuse `evals.metrics.answer_contains`, `section_recall` and `citation_precision`; `answer_excludes` is new (share of forbidden fragments absent, same normalisation) since the harness has no equivalent. One shared `_evaluate` helper builds ADK's result: one entry per invocation, threshold from `criterion`, `NOT_EVALUATED` when the golden invocation has no matching rubric.
- **Config:** `build.config_for` now registers the trajectory score plus five custom metrics (`GATING_METRICS`), all at 1.0. Rubric type names are shared constants in `metrics.py` used by the builder. Generated JSON was regenerated (only the two `test_config.json` files changed).

## Decisions
- `citation_grounded` needs no rubric: it applies to every answer and is `NOT_EVALUATED` only when nothing is cited. A cited answer with no search in the turn fails.
- A turn that lists an `EXPECTED_SECTION` but made no search scores 0.0 (fails), not `NOT_EVALUATED`.

## Files
- Created: `tests/test_adk_answer_metrics.py` (14 tests), `dev_test/phase_11_2.py`.
- Modified: `evals/adk/metrics.py`, `evals/adk/build.py`, `evals/adk/*/test_config.json` (2), `tests/test_adk_evalsets.py` (config test now expects six metrics), `dev_docs/sprints/sprint11.md`.

## Commands
From `db-doc-qa/`:
```bash
uv run python -m evals.adk.build
uv run python -m dev_test.phase_11_2   # all four metrics PASSED on a hand-written answer
uv run pytest
```
