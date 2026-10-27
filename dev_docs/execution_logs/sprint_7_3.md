# Sprint 7 — Phase 7.3 Execution Log: Configuration, Instruction and Scoring

## Summary
One YAML now runs HybridQA with the generic runner (`hybridqa_dev50_ollama.yaml`: Ollama embeddings, Gemini agent), and `evals.hybridqa score` checks a run with the official scorer logic. A 5-case smoke run and a 2-case live check (one `table`, one `passage`) all scored EM 1.0 with bare first-line answers. `uv run pytest`: **384 passed** (8 new).

## What was implemented
- **Instruction** `db-doc-qa/configs/prompts/hybridqa.md` (design §4.1): sources (one table + linked entity articles), routing (SQL for the row, then `search_documents` with `source="<Entity>.md"`, repeated for chained hops, an unfiltered search if no article is named after the entity), scope (only these sources, no outside knowledge), answer format (first line = the bare answer; at most 2 justification lines citing `[Entity.md]`; no `Sources:` line), and the read-only and prompt-injection rules of the built-in instructions. It has only the placeholders `{schema_summary}`, `{docs_summary}`, `{load_error?}` (no `{intent?}`, since the classifier is off).
- **Run configs:** `hybridqa_dev50_ollama.yaml` (the one used for results) and `hybridqa_dev50.yaml` (Gemini embeddings) both have `agent.instruction_file: configs/prompts/hybridqa.md`, `intent_enabled: false`, `temperature: 0.0`, `metrics: [exact_match, squad_f1, answer_contains, coverage, object_f1]`, `predictions: true` and `prediction_answer: first_line`.
- **`score` command** (`evals/hybridqa.py`): `uv run python -m evals.hybridqa score <run_dir> --reference <bundle>/reference/dev_reference.json`. It turns `predictions.jsonl` into `[{question_id, pred}]` and applies the logic of `benchmark/HybridQA/evaluate_script.py`, writes `official_scores.json` into the run folder and prints it. A reference question with no prediction fails with `error: ... N reference question(s) have no prediction: <ids>` (exit code 1) instead of `KeyError`; a missing `predictions.jsonl` says to run with `eval.predictions=true`.

## Decisions
- **Scorer copied, not imported.** The official normalisation and F1 are copied into `hybridqa.py` (MIT) rather than reusing `evals.metrics`, so `score` is an independent check of our `exact_match` / `squad_f1` (Phase 7.4 compares them).
- **Output keys** are the official ones (`table exact`, `table f1`, `passage ...`, `total ...`, 0-100) plus `n`. A type with no questions gives `null` (the official script would divide by zero).
- **The instruction needed no adjustment** after the smoke run, so it was not re-run.

## Smoke run (`dataset.limit=5`, run folder `runs/20261027-164039_hybridqa_dev50_ollama`)
- 5 cases, 0 errors; EM 1.00, F1 1.00, `answer_contains` 1.00, `coverage` 1.00; latency p50 10.2 s, p95 18.9 s. The first 5 cases are all `table` questions (the bundle lists `table` first); the dev script covers both types.
- Every `predictions.jsonl` answer is a bare value (`Malmö SS`, `Paide Linnameeskond`, `4th Defense Battalion`, `Harvey Kurtzman`, `Belgium`); the justification lines cite `[Entity.md]`.
- `object_f1` is 0.07: the predicted ids include the SQL table name and every searched article, while the gold holds the answer-node entities. It stays a diagnostic.
- `score` on this 5-case run fails cleanly as designed: `45 reference question(s) have no prediction` (the reference holds all 50).

## Tests (`tests/test_hybridqa_score.py`)
Both configs load with the expected agent, eval and embedding settings; the instruction file exists, has the three placeholders and no other braces, and `build_agent` uses it; `score` on a hand-made run folder gives table EM 50 / F1 90, passage 50 / 50, total 50 / 70; our `exact_match` / `squad_f1` agree with it on the same pairs; a missing qid and a missing file fail cleanly (also through the CLI exit code). All earlier tests pass; `grep -ri hybridqa` over the protected files finds nothing.

## Commands and results
```
cd db-doc-qa
uv run pytest                                                                      # 384 passed
uv run python -m evals configs/eval/hybridqa_dev50_ollama.yaml dataset.limit=5     # smoke run
uv run python -m evals.hybridqa score <run_dir> --reference ../benchmark/hybridqa_prepared/dev_s50_ollama/reference/dev_reference.json
uv run python -m dev_test.phase_7_3        # 2 live cases (needs GOOGLE_API_KEY and Ollama running)
```
Dev script: the `table` case used `run_sql` plus 10 article searches and answered `Malmö SS` (gold `Malmö SS`, EM 1.0); the `passage` case used 2 `run_sql` and 1 `search_documents` on `Two Can Play That Game.md` and answered `Vivica A. Fox and Morris Chestnut` (gold `Vivica A . Fox and Morris Chestnut`; the normalisation makes them equal, EM 1.0).

## Notes
- The table case searched ten articles for a question one article answers; that is a routing inefficiency to look at in Phase 7.4's failure analysis, not an error.
- No git commit was made.
