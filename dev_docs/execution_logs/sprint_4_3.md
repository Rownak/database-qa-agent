# Sprint 4 — Phase 4.3 Execution Log: Evaluation

## Summary
`uv run python -m evals <config.yaml> [key=value ...]` loads a dataset, asks every case in a fresh `AgentSession`, scores the answers with four deterministic metrics and saves `config.yaml`, `results.jsonl` and `metrics.json` under `runs/<timestamp>_<name>/`. `uv run pytest`: **265 passed** (246 earlier + 19 new). A live run of the 6 shipped cases on the real model gave 0 errors; the 2-case dev script gave all metrics 1.0.

## What was done
- **`evals/dataset.py`:** `ExpectedSection`, `Expected` (`tools`, `sections`, `answer_contains`), `EvalCase`, `EvalDataset` (unique case ids, `extra="forbid"`), `load_dataset` and `dataset_files` (paths relative to `db-doc-qa/`; a docs folder expands to its sorted files). `tools: []` ("call no tool") is kept distinct from `None` ("not checked").
- **`evals/datasets/shipments_v1.yaml`:** 6 cases: `db_location`, `doc_express_threshold`, `hybrid_delayed_express`, `conflict_international` (12347), `no_policy_refrigerated` (12349, expects the general rule 2.4 and "4 days") and `refuse_write`.
- **`evals/metrics.py`:** `METRICS` with `tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision`; each returns `None` when the case has nothing to compare. The last two sections metrics read the passages from the `search_documents` responses in `TurnResult.calls`. Section numbers match exactly or as a prefix of a split section (`3.2` and `3.2(a)-(c)`), but `3.3` does not match `3.30`. `citation_precision` parses `[source, §number ...]` and is `None` when the answer has no citation.
- **`evals/report.py`:** `write_config`, `short_calls` (tool responses cut to 300 characters) and `write_run(run_dir, cfg, rows, metrics)`.
- **`evals/run.py`:** `run_eval(cfg, run_dir, session_factory=AgentSession)` follows the four steps of the sprint. It writes `config.yaml` before the first question. Each case and repeat gets its own session and state dict, and the session is always closed. A failing factory, `ask` or `close` becomes `error` and the run goes on. `metrics.json` holds `n`, `errors`, `latency_p50_s`, `latency_p95_s` and `metrics` (mean per metric over the cases where it applies).
- **`evals/__main__.py`:** parses the arguments, calls `load_config`, creates `runs/<YYYYmmdd-HHMMSS>_<name>/`, runs and prints the summary and the folder. Exit code 2 without arguments.
- **`.gitignore`:** `db-doc-qa/runs/` added.

## Decisions and assumptions
- **Metric names:** `eval.metrics` in `config.py` still allows the ten names of the design (including `intent_accuracy` and `mrr`). `run_eval` computes only those that exist in `METRICS`, so `smoke.yaml` and the default list work unchanged; the others are skipped silently. `mrr` arrives in Phase 4.4.
- **`fail_under`:** `smoke.yaml` still sets it, but this phase has no threshold gate (not in the sprint plan); it is ignored.
- **Errored runs are scored** like any other, with their empty answer, and counted in `errors`. Their latency is left out of p50/p95.
- **Percentiles** use `numpy.percentile` (linear interpolation).
- **No `meta.json` / `summary.md`:** the sprint plan lists three files for this phase.

## Findings from the live run
- All 6 cases routed as expected and every `section_recall` was 1.0. Mean `citation_precision` was 0.85.
- It was 0.4 on `conflict_international`: the model cited `§3 Rules by Shipment Type` (the parent heading) instead of `§3.3`, so those citations do not match a returned passage. This is a real citation-format weakness the metric found, not a parser error. No production change was made.
- Latency p50 5.1 s, p95 7.5 s per case.

## Files
- **Created:** `evals/__init__.py`, `evals/dataset.py`, `evals/metrics.py`, `evals/report.py`, `evals/run.py`, `evals/__main__.py`, `evals/datasets/shipments_v1.yaml`, `tests/test_eval_dataset.py`, `tests/test_eval_metrics.py`, `tests/test_eval_run.py`, `dev_test/phase_4_3.py`, this log.
- **Modified:** `.gitignore`, `dev_docs/sprints/sprint4.md` (Phase 4.3 ticked).

## Tests (19 new)
- `test_eval_dataset.py` (5): the shipped dataset loads; `tools: []` is kept; duplicate id, unknown key and unknown tool are rejected; a docs folder expands.
- `test_eval_metrics.py` (7): each metric on a passing and a failing result; split sections; `None` when `expected` is empty.
- `test_eval_run.py` (7): a full run with a fake session writes three files and the expected means; the saved `config.yaml` has the override and reloads equal; rows, repeats, one session per run, all closed; a case that raises and a failing factory are counted in `errors`; `limit` and an unknown case id; the CLI.

## Commands
```
cd db-doc-qa
uv run pytest                                          # 265 passed
uv run python -m dev_test.phase_4_3                    # live, 2 cases; needs GOOGLE_API_KEY
uv run python -m evals configs/eval/smoke.yaml         # live, all 6 cases
uv run python -m evals configs/eval/smoke.yaml agent.retrieval.mode=bm25 name=smoke_bm25
```

## Limitations
- The evaluation is not deterministic: model output varies between runs even at `temperature: 0`. Use `eval.repeats` for comparisons.
- Each case rebuilds `docs.db` (one embedding call) plus 2-4 model calls.
- No `git commit` was made.
