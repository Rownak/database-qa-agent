# Sprint 7 — Phase 7.2 Execution Log: HybridQA Adapter `prepare`

## Summary
`uv run python -m evals.hybridqa prepare ...` now writes a self-contained, reproducible HybridQA bundle that the Phase 7.1 runner runs unchanged. The real `dev_s50` bundle was built: 25 `table` + 25 `passage` questions, 50 tables, 1,521 passage files, 3,565 chunks, all embedded, none of the sampled questions skipped. `uv run pytest`: **369 passed** (16 new).

## What was implemented
New module `db-doc-qa/evals/hybridqa.py` (the only place with HybridQA parsing and mapping; no existing module changed in this phase).

- **CLI** `prepare --hybridqa --config --out [--split dev] [--per-type N|all] [--seed] [--cache] [--one-per-table/--no-one-per-table]`. The config supplies only `agent.chunking` and `agent.embedding`.
- **Labels:** reads `released_data/<split>.traced.json` and `<split>_reference.json` (clear `PrepareError` if either is missing). `table`/`passage` come from the reference lists; the 92 unlabeled dev questions are excluded and counted.
- **Sample:** qids of each type sorted, shuffled with `random.Random(seed)` per type. Skipped qids are recorded with a reason (failed download/parse, table already used).
- **Fetch with cache:** `tables_tok/<id>.json` and `request_tok/<id>.json` from `raw.githubusercontent.com/wenhuchen/WikiTables-WithLinks/master/`, cached under `--cache` (default `../benchmark/hybridqa_prepared/raw`). The fetch function is a parameter (`fetch=`), so tests use a local reader. The sha256 of every file used goes to `prepare.json`.
- **Per table** in `tables/<safe_id>/`: `<safe_id>.csv` (header + `cell_text`, links dropped), `docs/<Entity>.md` (`# Title`, blank line, passage), `data.db` (`ingest.load_files`), `docs.db` (`doc_ingest.build`), `state.json` (relative paths, same keys as the LakeQuest bundle). A table shared by two sampled questions is built once.
- **Bundle root:** `dataset.yaml` (`hybridqa_dev_s50`; per-case `state_file`, tags `[hybridqa, table|passage]`, `reference_answers` = `answer_contains` = the gold answer, `object_ids` = entity file stems of the `passage` nodes), `reference/dev_reference.json` (subset, official format) and `prepare.json` (source hashes, HybridQA git commit, seed, counts, skipped, chunking, embedding).

## Key decisions
- **Containment of samples.** With a shared one-per-table rule, a naive sequential walk (all `table`, then all `passage`) can make a larger sample change the smaller one. Instead the two lists are walked in alternation, and a type whose quota is full keeps *reserving* tables (without keeping the question). Each decision then depends only on earlier ones, so a larger `--per-type` extends a smaller sample. Tested.
- **Header repair.** HybridQA headers are sometimes blank, numeric or repeated, which `ingest.read_table` rejects. The adapter renames them (`col_<n>`, `col_<text>`, `name 2`) rather than losing the table. Tables that still fail ingestion are skipped and recorded.
- **Embedding retries.** The first real run logged `embedding failed (ClientError)` for 7 tables (API rate limit); `doc_ingest.build` then silently writes a BM25-only index. `prepare` now rebuilds a table's `docs.db` up to 6 times with a growing pause and **fails with `PrepareError`** if embedding still fails, so a bundle can never contain a BM25-only index that is scored as hybrid. The re-run needed no retry (raw files came from the cache).
- **Safe names.** Entity file names replace `<>:"/\|?*` and control characters, avoid Windows reserved names, cap at 60 characters and get `_2`, `_3` on duplicates. The title heading keeps the real name. The table stem comes from `ingest.table_name_for`, so the folder, CSV and SQL table share one SQL-safe name.
- **Gold object ids** use the entity *file stem* (the safe name), so `object_f1` compares like with like.
- `dataset.yaml` stores `state_file` with forward slashes (Windows would otherwise write backslashes).

## Real bundle (`benchmark/hybridqa_prepared/dev_s50`, git-ignored, 24 MB)
| | |
|---|---|
| cases per type | table 25, passage 25 (unlabeled dev questions excluded: 92) |
| skipped qids | none |
| tables / passages / chunks | 50 / 1,521 / 3,565 (4 passage files had no readable text and were skipped by `doc_ingest`) |
| raw files hashed | 100 (50 tables + 50 request files) |
| HybridQA commit | `db22fda8c5951438fade3c69d75b350335ba93b3` |

## Files
- Created: `db-doc-qa/evals/hybridqa.py`, `tests/test_hybridqa_prepare.py`, `dev_test/phase_7_2.py`, `configs/eval/hybridqa_dev50.yaml` (minimal now so `prepare` can read the chunking/embedding settings; Phase 7.3 completes it with `instruction_file` and `prediction_answer`).
- Modified: `dev_docs/sprints/sprint7.md` (Phase 7.1 and 7.2 tasks ticked).

## Tests (`tests/test_hybridqa_prepare.py`, mini `released_data`: 8 questions over 4 tables, `fake_embed`, no network)
Dataset validates with the requested counts; same seed gives the same cases and a larger sample contains the smaller; unlabeled excluded and a failed fetch skipped and recorded; `--no-one-per-table` builds a shared table once; CSV header/rows and entity heading match the fixture; a Windows-unsafe entity name gets a safe file; `state.json` paths are relative and load through `_load_prebuilt`; the reference subset has exactly the sampled qids in the right lists; `run_eval` on the bundle gives per-tag means for `table` and `passage`; plus cache reuse, embedding retry/failure, CLI, forward-slash paths and helpers.

## Commands and results
```
cd db-doc-qa
uv run pytest                                  # 369 passed
uv run python -m dev_test.phase_7_2            # builds the real dev_s50 (network + embedding API), ~1 min
uv run python -m evals.hybridqa prepare --hybridqa ../benchmark/HybridQA \
  --config configs/eval/hybridqa_dev50.yaml --per-type 25 --seed 20260101 \
  --out ../benchmark/hybridqa_prepared/dev_s50
```
`grep -ri hybridqa data_agent evals/dataset.py evals/run.py evals/report.py evals/metrics.py`: no matches.

## Limitations
- Console output of non-ASCII titles shows `?` on Windows without `PYTHONIOENCODING=utf-8`; the files themselves are UTF-8.
- Gold `object_ids` for `table` questions can list a passage node (HybridQA's `answer-node` is approximate); `object_f1` stays a diagnostic.
- No git commit was made.
