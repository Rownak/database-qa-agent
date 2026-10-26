# Sprint 5 — Phase 5.1 Execution Log: Data, Profile and Metrics

## Summary
The agent can now be configured with a `lakequest` profile, and three LakeQuest metrics (`answer_f1`, `object_f1`, `coverage`) exist and match the official scorer's token F1. The default `shipments` profile is unchanged. `uv run pytest`: **279 passed** (271 earlier + 8 new). **One task is not done: the Open Dev download** (see below).

## Not done: Open Dev download
Codabench needs a logged-in account to download the Open Dev input and reference files (<https://www.codabench.org/competitions/17066/>), so I could not fetch them. `benchmark/LakeQuest/open_dev/` does not exist yet. Because of this:
- the real column names of `questions.*` and `references.*` are **not recorded**
- the number of questions per `domain` is **not recorded**
- the design assumption (`qa_id`, `question`, `domain`; references `answer_short`, `answer_long`, `gold_object_ids`) is **unconfirmed**. The reference columns come from `score.py`, so they are reliable. The question columns come from the starter kit text only.

**To do (manual):** download the input and reference files into `benchmark/LakeQuest/open_dev/input/` and `.../reference/`, then add the column names and the per-domain counts here. Phase 5.2 should not start until the columns are checked.

## What was done
- **Config** (`data_agent/config.py`): `AgentConfig.profile: Literal["shipments", "lakequest"] = "shipments"`, `EvalConfig.predictions: bool = False`, and `answer_f1`, `object_f1`, `coverage` in `MetricName`. They are therefore also part of `ALL_METRICS`, the default list.
- **Profile** (`data_agent/agent.py`):
  - `LAKE_INSTRUCTION` has the tables and policies from `{schema_summary}` and `{docs_summary}`. It also has `{load_error?}` and no `{intent?}`, since the classifier is off for this profile.
  - Its rules: double-quote table and column names, run SQL first when a client is named, use `search_documents` with `source="<policy_id>.md"` when a Policy ID is named.
  - Its answer format: a direct first sentence, at most 3 sentences with `[source, §N title]` citations, no headings, SQL or `Sources:` line. The grounding and safety rules are kept.
  - `_PROFILES` maps a profile to its `(instruction, description)`, and `build_agent` picks from it by `cfg.profile`.
- **Neutral tool text** (`data_agent/tools.py`): the `run_sql` docstring now says "the session's SQLite database; the instruction lists its tables". The shipments instruction already says table `data`, so the model still gets that from the instruction.
- **Dataset** (`evals/dataset.py`): `Expected.reference_answers` and `Expected.object_ids`.
- **Metrics** (`evals/metrics.py`): `final_answer`, `predicted_object_ids`, `answer_f1`, `object_f1`, `coverage`, registered in `METRICS` and `AGENT_METRICS`.
  - The tokenizer and F1 are copied from `score.py`, including its empty-text cases.
  - `predicted_object_ids` uses the sources of cited passages that were really returned (a citation to a passage that was not returned does not count). When nothing valid is cited, it uses all returned passages. It adds tables after `FROM`/`JOIN` from successful `run_sql` calls. Ids are lower-cased and sorted.
  - `answer_f1` and `object_f1` return `None` when the expected field is empty. `coverage` is never `None`.
- **Tests:**
  - `test_agent.py`: the default profile builds `INSTRUCTION`; `lakequest` builds `LAKE_INSTRUCTION` (key rules present, no stray braces).
  - `test_eval_metrics.py`: a passing and a failing case for each new metric, `final_answer` removing the `Sources:` line, `predicted_object_ids` reading cited sources and SQL tables, and the **parity test**. The parity test imports `benchmark/LakeQuest/scoring_program/score.py` by path and compares `answer_f1` with `score._token_f1` on 5 pairs. It runs here (the file exists) and is skipped when the file is missing.
  - Changed an existing test: `test_every_metric_is_none_when_expected_is_empty` now skips `coverage`, which does not need an expectation.
- **Dev script** `dev_test/phase_5_1.py`.

## Regression: `configs/eval/smoke.yaml`
Real model, 6 cases, 0 errors. Run folder `db-doc-qa/runs/20261026-120610_smoke`.

| Metric | Before (Phase 4.4 log) | Now |
|---|---|---|
| `tool_sequence_match` | 1.00 | 1.00 |
| `section_recall` | 0.75 (1.00 in an earlier run) | 0.88 |
| `citation_precision` | 0.95 (0.85 earlier) | 1.00 |
| `answer_contains` | 1.00 | 0.92 |
| latency p50 / p95 | 4.7 s / 8.1 s | 5.3 s / 9.6 s |

`answer_contains` is 0.08 lower than the Phase 4.4 run, but those scores varied between runs in Phase 4.4 as well (the model writes different queries), and the shipments instruction, tools and profile are unchanged. I did not repeat the run (`eval.repeats`) to separate noise from a real drop. The new metrics print `-` for answer_f1 and object_f1 (no expectations) and 1.00 for coverage.

## Decisions and assumptions
- `coverage` is added to the default metric list, so every run now reports it. It is harmless for shipments.
- `LAKE_INSTRUCTION` is not run against a real LakeQuest bundle yet. That needs Phases 5.2–5.3, so its wording is untested on the model.

## Files
- Modified: `data_agent/config.py`, `data_agent/agent.py`, `data_agent/tools.py`, `evals/dataset.py`, `evals/metrics.py`, `tests/test_agent.py`, `tests/test_eval_metrics.py`
- Created: `dev_test/phase_5_1.py`, `dev_docs/execution_logs/sprint_5_1.md`

## Commands
```
cd db-doc-qa
uv run pytest                              # 279 passed
uv run python -m dev_test.phase_5_1        # metrics for two hand-made results + the instruction start
uv run python -m evals configs/eval/smoke.yaml
```
