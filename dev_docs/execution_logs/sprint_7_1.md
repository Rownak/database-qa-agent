# Sprint 7 — Phase 7.1 Execution Log: Generic Pipeline Extensions

## Summary
The eval pipeline can now run a dataset where each case has its own prebuilt bundle, its own agent instruction (from a file) and short-answer metrics (`exact_match`, `squad_f1`). Nothing is HybridQA-specific, and every new field is optional with a default that keeps today's behaviour. `uv run pytest`: **353 passed**.

## What changed
| Area | File | Change |
|---|---|---|
| Per-case bundle | `evals/dataset.py` | `EvalCase.state_file: Path \| None = None` (relative to the dataset file). |
| Runner | `evals/run.py` | State per case: case `state_file` > dataset `state_file` > `table`/`docs`. Each distinct `state.json` is loaded once. Any prebuilt state closes with `delete_files=False`. Retrieval mode rejects case-level bundles as well as dataset-level ones. |
| Guard | `evals/run.py` (`_load_prebuilt`) | New optional `fallback_dir` and `checked` arguments. `prepare.json` is looked up next to `state.json`, then next to `dataset.yaml`. A cache keyed by the `prepare.json` path makes the chunking/embedding guard run once per distinct file, before the first question. `metrics["data"]` holds the first `prepare.json` found. |
| Instruction | `data_agent/config.py`, `data_agent/agent.py` | `AgentConfig.instruction_file: Path \| None = None`. `build_agent` reads it as UTF-8 (relative paths from `db-doc-qa/`) and keeps the profile's description. A missing or unreadable file raises `FileNotFoundError` at startup. `config.py` stays pydantic-only. |
| Metrics | `evals/metrics.py`, `MetricName` | `first_line`, `short_answer`, `normalize_answer`, `exact_match`, `squad_f1`. Both metrics return `None` without `reference_answers`. `answer_f1` is untouched. |
| Predictions | `config.py`, `evals/report.py` | `eval.prediction_answer: "full" \| "first_line"` (default `full`). `first_line` writes the first non-empty line of the answer without its `Sources:` line. |
| Design | `dev_docs/design/hybridqa_evaluation_design.md` | `hybridqa` profile replaced by `instruction_file` and `prediction_answer` in §4, §4.1 and §6. |

## Decisions and notes
- **New metrics are opt-in.** `MetricName` includes them, but the default `eval.metrics` list (`DEFAULT_METRICS`) leaves them out. Otherwise existing configs (`smoke.yaml`, ...) would get two extra all-`None` keys in `metrics.json`. Checked: `smoke.yaml dataset.limit=1` prints no `exact_match` row.
- **SQuAD normalisation** follows `benchmark/HybridQA/evaluate_script.py` exactly (lower, strip punctuation, strip articles, collapse spaces). The F1 empty-text rule is `int(gold_tokens == pred_tokens)`, as in the script.
- **`short_answer` scores `final_answer`**, so a trailing `Sources:` line never counts and `predictions.jsonl` (`first_line`) holds the same text.
- The error text of the stale-bundle guard now says "run `prepare` again" instead of naming `evals.lakequest`, since a second adapter uses it. The test matches only "chunking"/"embedding".
- With several different `prepare.json` files, `metrics["data"]` shows only the first. No dataset needs more yet.

## Tests added (`db-doc-qa/tests/`)
- `test_eval_dataset.py`: case-level `state_file` loads; shipped dataset unchanged.
- `test_eval_run.py`: two cases get their own states and both bundles survive; a dataset-level `state_file` still works and the case wins; a chunking mismatch in a shared `prepare.json` raises before any question; case-level bundles in retrieval mode raise; `prediction_answer` `first_line` vs default.
- `test_agent.py`: `instruction_file` replaces the instruction and keeps the description; built-in instructions unchanged; a missing file raises.
- `test_eval_metrics.py`: articles/punctuation/case, multi-line answer with `Sources:`, `None` without references, empty-text cases, and five hand-made pairs checked against the formulas copied from `evaluate_script.py`. The registry test lists the two new metrics.

## Commands and results
```
cd db-doc-qa
uv run pytest                                   # 353 passed
uv run python -m dev_test.phase_7_1             # two-case demo: state keys, metrics, example scores
uv run python -m evals configs/eval/smoke.yaml dataset.limit=1   # runs; 1 case, 0 errors
grep -ri hybridqa data_agent evals/dataset.py evals/run.py evals/report.py evals/metrics.py   # no matches
```
Dev script output: the bundle case got state keys `column_map, db_path, docs_db_path, docs_summary, schema_summary`; the sample case got `[]` (the agent builds the samples itself). Example scores against `["beatles", "New York City", "Paris"]`: `"The Beatles."` EM 1.0 / F1 1.0; `"New York"` EM 0.0 / F1 0.8; `"Paris\nSources: x"` EM 1.0 / F1 1.0.

## Limitations
- `smoke.yaml` was run live (real model, 1 case); `lakequest_bank.yaml` was not run in this phase. Its path is covered by the existing prebuilt-bundle tests, which pass unchanged (Phase 7.4 re-checks it live).
- No git commit was made. The earlier untracked files (`dev_docs/sprints/sprint7.md`, HybridQA design and benchmark reports) are unchanged by this phase except the design doc edit.
