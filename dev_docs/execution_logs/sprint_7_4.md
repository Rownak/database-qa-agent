# Sprint 7 — Phase 7.4 Execution Log: Run the 50 Questions and Verify

## Summary
The 50-question HybridQA dev sample ran end to end with `configs/eval/hybridqa_dev50_ollama.yaml` (Gemini agent `gemini-3.5-flash-lite`, temperature 0.0, Ollama `nomic-embed-text` embeddings, hybrid retrieval). **Overall EM 62.0, F1 73.8; the official scorer gives exactly the same numbers per type**, so no metric bug. 0 errors. Run folder: `db-doc-qa/runs/20261027-165235_hybridqa_dev50_ollama` (git-ignored).

## Result (design §5.2 table)
| Type | n | EM | F1 | answer_contains | object_f1 |
|---|---|---|---|---|---|
| table | 25 | 68.0 | 78.2 | 0.88 | 0.14 (diagnostic) |
| passage | 25 | 56.0 | 69.3 | 0.76 | 0.45 |
| all | 50 | 62.0 | 73.8 | 0.82 | 0.36 |

- `coverage` 1.00 (every question got an answer); errors 0; latency p50 4.6 s, p95 24.5 s.
- 178 tool calls in total (avg 3.6 per question, max 11), so about 230 model calls (one per tool call plus the final answer per question); 1 SQL error (a quote character copied from a cell, `unrecognized token`), retried by the agent.
- Embedding: local Ollama, no API cost. Building `dev_s50_ollama` took 3 min 18 s (Phase 7.2a).
- n=50 is small: one question is 2 points, so differences between types are not significant yet.

## Official check
`uv run python -m evals.hybridqa score <run> --reference ../benchmark/hybridqa_prepared/dev_s50_ollama/reference/dev_reference.json`

| | ours EM | official EM | ours F1 | official F1 |
|---|---|---|---|---|
| table | 68.00 | 68.00 | 78.23 | 78.23 |
| passage | 56.00 | 56.00 | 69.33 | 69.33 |
| total | 62.00 | 62.00 | 73.78 | 73.78 |

## How the metrics are calculated
- **EM (`exact_match`):** takes the first line of the answer and the gold answer, lower-cases both, removes punctuation and the words a/an/the, and collapses spaces. Score 1 if they are equal, else 0.
- **F1 (`squad_f1`):** same normalisation, then splits both into words. Precision = shared words / words in the answer; recall = shared words / words in the gold; F1 = 2PR / (P+R). It gives partial credit (`New York` vs `New York City` = 0.8).
- **`answer_contains`:** 1 if the gold answer appears anywhere in the whole answer (lower-cased, spaces collapsed), so extra words do not hurt.
- **`object_f1`:** F1 between two sets of names. Gold = the linked entities of the answer's passage nodes. Predicted = the articles cited in the answer (or all articles returned by searches if none is cited) plus the tables in the successful SQL queries. Rough guide only, because the gold is approximate and SQL table names always count as predicted.
- Per-type and overall figures are the mean over the questions of that type.
- **Official EM and F1:** `evals.hybridqa score` reads the run's `predictions.jsonl` (the first-line answers), pairs each with its gold answer from `dev_reference.json`, and applies the logic copied from the benchmark's `evaluate_script.py` (same normalisation, EM and F1 as above). It averages per question list (`table`, `passage`, all) and multiplies by 100 (`official_scores.json`). It is code separate from `evals/metrics.py`, so equal numbers confirm our metrics.

## Failure analysis (19 questions with EM = 0)
| Class | n | Questions |
|---|---|---|
| Correct but worded differently | 8 | `Belgian` for Belgium; `John Daniel Hinton` for John Hinton; `Sarah Jane Kenyon ( 2 )` (cell suffix kept); `7,500 acres`, `104 kg` (unit added); `Battle of Dan-no-ura` for Dan-no-ura; `5` for `five`; a long phrase around `directeur sportif` |
| Right article, wrong span | 5 | `Alpine skiing` for 2013; `2008, 2012 and 2016` for 2008; `Buttercup and Primrose` for Buttercup; `Sikorsky UH-60 Black Hawk` for `United States Army UH-60 Black Hawk`; "sources do not contain the answer" for `five` (a count over a personnel list) |
| Wrong row (SQL) | 4 | `20` for 17 (wrong country); `3 years` for 6 (no article search, wrong gap); `Shinjuku, Tokyo` for Kyoto; a time returned as `2026-10-27 01:31:04` for `1:31.075` |
| Wrong or missing entity article | 2 | `Seremban District` for Kinta District (searched the wrong article); "sources do not contain" the 2011 population of a village (11 tool calls) |
| Error or timeout | 0 | |

Observations:
- **Format is the largest single cause:** 8 of 19 misses are the right answer with extra words, units or a digit instead of a word. Three more (`2008, 2012 and 2016`, `Buttercup and Primrose`, `Sikorsky ...`) are also near misses. A stricter answer instruction ("copy the cell or passage text exactly, no units, no extra items") could recover a large share of EM. This phase only reads results, so the instruction was not changed; it is a candidate for a later experiment (a new instruction file, no code change).
- **Ingestion artifact:** a time like `1:31.075` is stored as an ISO timestamp by the generic type inference (`ingest._to_iso`), so the agent cannot return the original text. Not changed here (shared by the app); worth a separate look.
- **Routing:** table questions sometimes search many articles one after another (up to 11 calls), which drives the p95 latency.
- `answer_contains` (0.82) is well above EM (0.62), which confirms the format gap. `object_f1` stays a weak diagnostic: it counts SQL table names and every searched article as predicted objects.

## Compatibility re-check
- `smoke.yaml dataset.limit=2` and `lakequest_bank.yaml dataset.limit=2` ran (2 cases, 0 errors each). The run folders have the same keys in `metrics.json`, the same metric names and the same `results.jsonl` row keys as the earlier runs. The only difference in the file list is an `official/` folder in an older LakeQuest run, written by the manual LakeQuest scoring step, not by the pipeline.

## Other changes
- `README.md`: new "HybridQA evaluation" section (prepare, run and `score` commands; internal-use-only note, since WikiTables-WithLinks has no license and the data stays in git-ignored `benchmark/`).
- New `db-doc-qa/dev_test/phase_7_4.py`: `uv run python -m dev_test.phase_7_4 <run_dir>` prints our per-type EM/F1 next to `official_scores.json`.
- `dev_docs/sprints/sprint7.md`: Phase 7.4 ticked. No code change in this phase; `uv run pytest` was already green (384 passed) and was not affected.

## Commands
```
cd db-doc-qa
uv run python -m evals configs/eval/hybridqa_dev50_ollama.yaml
uv run python -m evals.hybridqa score runs/20261027-165235_hybridqa_dev50_ollama \
  --reference ../benchmark/hybridqa_prepared/dev_s50_ollama/reference/dev_reference.json
uv run python -m dev_test.phase_7_4 runs/20261027-165235_hybridqa_dev50_ollama
```

## Limitations
- One run at temperature 0; Phase 7.5 measures the variance with `eval.repeats=3`.
- Numbers are for the Gemini agent with Ollama embeddings; they are not comparable with Gemini-embedding runs.
- No git commit was made.
