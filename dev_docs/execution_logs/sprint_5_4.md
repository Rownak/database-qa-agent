# Sprint 5 — Phase 5.4 Execution Log: Runner Support and Predictions

## Summary
`run_eval` now runs a prebuilt bundle (`state_file`) without rebuilding or deleting it, stops early when the bundle was built with other chunking or embedding settings, reports means per tag, and writes `predictions.jsonl`. It was run on the real `bank_sample` bundle with the live agent (3 cases). `uv run pytest`: **306 passed** (299 earlier + 7 new). The existing shipments tests pass unchanged.

## What was done
- **Runner** (`evals/run.py`):
  - When the dataset has `state_file` (resolved against the dataset file), `_load_prebuilt` reads it, makes `db_path` and `docs_db_path` absolute, and checks that both files exist. Each case gets a copy of that state, and the session is closed with `close(delete_files=False)`. Without `state_file`, the old path (`files_state`, `close()`) is unchanged.
  - **Config guard:** `prepare.json` next to `state.json` has the `chunking` and `embedding` used for `docs.db`. If either differs from `cfg.agent`, `ValueError` is raised before the first question (after `config.yaml` is written). A bundle without `prepare.json` skips the guard.
  - A prebuilt dataset in `eval.mode: retrieval` raises `ValueError` (retrieval mode builds its own `docs.db` from files).
  - `metrics["by_tag"]`: `{tag: {"n", "metrics"}}` for every tag in the dataset (`bank`, `hybrid`, `table`, `document` for `bank_sample`). `metrics["data"]` holds the bundle's `prepare.json`, so the run records which data it used. Both are in the returned dict and in `metrics.json`.
  - Each `results.jsonl` row has `object_ids` (from `predicted_object_ids`).
- **Report** (`evals/report.py`): `write_predictions` writes `predictions.jsonl` when `eval.predictions` is true: `{"qa_id", "answer", "object_ids"}`, the answer without the `Sources:` line (`metrics.strip_sources`, shared with `final_answer`). Only the first repeat of each case is written, because the scorer keeps one prediction per `qa_id`.
- **Summary output** (`evals/__main__.py`): `format_summary` prints one line per tag.
- **Config:** `configs/eval/lakequest_bank.yaml` was already created in Phase 5.3.
- **Tests** (`tests/test_eval_run.py`, fake session and a bundle built by `prepare` from the Phase 5.3 fixture): the session receives the loaded state with absolute paths and is closed with `delete_files=False`; `bank.db` and `docs.db` survive; `predictions.jsonl` has one row per case with `qa_id`, `answer` (no `Sources:` line) and a list `object_ids`; no predictions file when `eval.predictions` is false; per-tag means and the bundle info are present; a chunking mismatch and an embedding mismatch raise before any session is created; a prebuilt dataset in retrieval mode raises.
- **Dev script** `dev_test/phase_5_4.py`.

## Live check (3 cases, `dataset.limit=3`)
Run folder `db-doc-qa/runs/20261026-133018_phase_5_4`: files `config.yaml`, `results.jsonl`, `metrics.json`, `predictions.jsonl`. 0 errors, p50 latency 4.1 s, the bundle was intact afterwards.

| Case | `answer_f1` | `object_ids` |
|---|---|---|
| qa-0349e99af2 | 0.37 | client, large-cash-withdrawal-ctr-exception |
| qa-0be5abbc13 | 0.30 | client, debit-card-reissue-expedite-exception |
| qa-0cacf2ec6e | 0.32 | p2p-scam-appeal-exception |

Means over the 3 cases: `answer_f1` 0.33, `object_f1` 0.52, `tool_sequence_match` 0.67, `section_recall` 0.25, `citation_precision` 0.67, `coverage` 1.00. These are 3 cases only, so they are not a baseline; the baseline is Phase 5.5. The answers follow the concise format (a direct first sentence, citations).

## Limits and notes
- The guard compares `chunking` and `embedding` only. A different `retrieval` or model setting is allowed, since it does not change `docs.db`.
- `by_tag` is added to `metrics.json` for every dataset, including shipments. The existing tests do not mind, but the file has a new key.
- With `eval.repeats > 1`, `results.jsonl` has every repeat and `predictions.jsonl` only the first.

## Files
- Modified: `evals/run.py`, `evals/report.py`, `evals/metrics.py` (`strip_sources`), `evals/__main__.py`, `tests/test_eval_run.py`, `dev_docs/sprints/sprint5.md` (ticked 5.4)
- Created: `dev_test/phase_5_4.py`, `dev_docs/execution_logs/sprint_5_4.md`

## Commands
```
cd db-doc-qa
uv run pytest                                  # 306 passed
uv run python -m evals configs/eval/lakequest_bank.yaml dataset.limit=5
uv run python -m dev_test.phase_5_4            # live agent, 3 cases; needs the bank_sample bundle and the API key
```
