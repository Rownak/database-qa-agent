# Sprint 7 — HybridQA Evaluation

**Goal:** evaluate the agent on HybridQA dev questions (one Wikipedia table plus its linked passages), as described in [hybridqa_evaluation_design.md](../design/hybridqa_evaluation_design.md):

- a few **dataset-agnostic** extensions to the Sprint 4/5 pipeline: a bundle per case, an agent instruction from a file, short-answer metrics
- a **HybridQA adapter** (`evals/hybridqa.py`) that holds all HybridQA parsing, mapping and official-format code
- a reproducible **50-question sample** (25 `table`, 25 `passage`), checked against the official `evaluate_script.py`, then larger samples through arguments and YAML only

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use small fixtures and `fake_embed`, with no network
- a short script `db-doc-qa/dev_test/phase_7_N.py`, run with `uv run python -m dev_test.phase_7_N`
- an entry in `dev_docs/execution_logs/sprint_7_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 7.1 | Generic pipeline extensions (per-case bundle, instruction file, short-answer metrics) |
| 7.2 | HybridQA adapter: `prepare` builds the 50-question bundle |
| 7.2a | Selectable embedding provider (Gemini or Ollama) to avoid embedding rate limits |
| 7.3 | Configuration, instruction and `score`; 5-case smoke run |
| 7.4 | Run the 50 questions and verify against the official scorer |
| 7.5 | Scale up through configuration |

**Rules for all phases**

- **Embeddings for evaluation (Phases 7.3-7.5):** use the local Ollama `nomic-embed-text:latest` (Phase 7.2a) to avoid the Gemini embedding rate limit. Every command, config and bundle below is the `_ollama` one (`hybridqa_*_ollama.yaml`, `dev_s50_ollama`, `dev_s200_ollama`). Prerequisite: the Ollama server is running with the model pulled. The agent LLM stays Gemini. The Gemini-embedding config and bundle remain available but are not used for results; never mix providers in one result series.

- **Keep the main pipeline dataset-agnostic.** The string `hybridqa` appears only in:
  - `evals/hybridqa.py`
  - `configs/eval/hybridqa_*.yaml`
  - `configs/prompts/hybridqa.md`
  - tests, dev scripts and docs

  It never appears in `data_agent/` or in `evals/{dataset,run,report,metrics}.py`.
- **Every new field is optional, and its default keeps today's behaviour.** The app, the deployed agent (`DATA_AGENT_CONFIG` unset), `shipments_v1`, `smoke.yaml`, `bm25_only.yaml` and `lakequest_bank.yaml` run exactly as before. The existing tests pass unchanged.
- **Benchmark data stays in `benchmark/`** (git-ignored); nothing from it is committed. The raw table files are fetched per table and cached. The 800 MB WikiTables-WithLinks repo is never cloned.
- **Reuse, don't restructure:** `ingest.load_files`, `doc_ingest.build`, `AgentSession`, `run_eval`, `_load_prebuilt`, `report.write_run` and the per-tag means.

**Change from the design (§4, item 4):** the design proposed a `hybridqa` profile in `data_agent/agent.py`. To keep `data_agent/` dataset-agnostic, the instruction comes from a file named in the config instead (Phase 7.1). The design is updated in Phase 7.1.

---

## Phase 7.1 — Generic Pipeline Extensions

**Goal:** the pipeline can run a dataset whose cases each have their own prebuilt bundle, its own instruction and short-answer metrics. Nothing is HybridQA-specific, and every default keeps current behaviour.

- [x] **Per-case bundle** (`evals/dataset.py`): add `EvalCase.state_file: Path | None = None`, resolved relative to the dataset file.
  - **Precedence:** the case's `state_file` wins over the dataset's `state_file`, which wins over `table`/`docs`.
  - **Validation:** a dataset with neither `table`/`docs` nor `state_file` still means the samples.
- [x] **Runner** (`evals/run.py`):
  - Choose the state per case using that precedence, and reuse `_load_prebuilt` to resolve the paths.
  - Run the chunking and embedding guard **once per distinct `prepare.json`**, before the first question. Look for it next to `state.json` first, then next to `dataset.yaml`. Put it in `metrics["data"]` as today.
  - Any prebuilt state, per case or per dataset, closes with `delete_files=False`.
  - Retrieval mode rejects per-case bundles with a clear `ValueError`, as it does now for `state_file`.
- [x] **Instruction from a file** (`data_agent/config.py`, `data_agent/agent.py`):
  - Add `AgentConfig.instruction_file: Path | None = None`. When it is set, `build_agent` reads the file (UTF-8) and uses it as the instruction, keeping the description of `profile`. When it is `None`, nothing changes.
  - The file uses the same state placeholders as the built-in instructions: `{schema_summary}`, `{docs_summary}`, `{load_error?}`.
  - Raise a clear error at startup for a missing file.
  - No new import in modules the UI image loads: `config.py` stays pydantic-only.
- [x] **Short-answer metrics** (`evals/metrics.py`, `MetricName` in `config.py`):
  - `short_answer(result)`: the first non-empty line of `final_answer`.
  - `exact_match`: the SQuAD normalisation (lower case, no punctuation or articles `a/an/the`, collapsed spaces), best over `reference_answers`.
  - `squad_f1`: token F1 with the same normalisation, best over `reference_answers`. Empty-text cases are handled like the SQuAD scorer.
  - Both return `None` without `reference_answers`. The existing `answer_f1` is untouched (LakeQuest parity).
- [x] **Prediction text** (`EvalConfig`, `report.write_predictions`): add `eval.prediction_answer: Literal["full", "first_line"] = "full"`. With `first_line`, `predictions.jsonl` holds `short_answer`, the same text `exact_match` scores. The default keeps the LakeQuest output unchanged.
- [x] **Design doc:** in `hybridqa_evaluation_design.md`, replace the `hybridqa` profile in §4 and §4.1 with `instruction_file` and `prediction_answer`.
- [x] **Tests:**
  - `tests/test_eval_dataset.py`: a case-level `state_file` loads and resolves relative to the dataset; existing datasets load unchanged.
  - `tests/test_eval_run.py` (fake session):
    - two cases with different bundles each receive their own state, and both bundles survive the run
    - a dataset-level `state_file` still works
    - a chunking mismatch in a shared `prepare.json` raises before any question
    - per-case bundles in retrieval mode raise
  - `tests/test_agent.py`:
    - `instruction_file` replaces the instruction and keeps the profile's description
    - without it, the `shipments` and `lakequest` instructions are unchanged
    - a missing file raises
  - `tests/test_eval_metrics.py`:
    - `exact_match`/`squad_f1` on articles, punctuation and case ("The Beatles." vs "beatles")
    - a multi-line answer with a `Sources:` line scores only its first line
    - `None` without references
    - our values equal the formulas of `benchmark/HybridQA/evaluate_script.py` on 5 hand-made pairs, copied into the test
  - `tests/test_eval_report.py` or `test_eval_run.py`: `prediction_answer: first_line` writes the first line; the default writes the full answer.
  - The existing `test_config`, `test_app*`, `test_stage_ui_context` and `test_lakequest_prepare` pass unchanged.
- [x] **Compatibility check:**
  - `grep -ri hybridqa data_agent evals/dataset.py evals/run.py evals/report.py evals/metrics.py` finds nothing.
  - `uv run python -m evals configs/eval/smoke.yaml dataset.limit=1` still runs.
- [x] **Dev script** `phase_7_1.py`: build a two-case dataset in a temp folder, one with a per-case bundle and one without (the tiny fixture bundle and the sample data). Run it with a canned session and print each case's state keys. Then print `exact_match`/`squad_f1` for three example answers.

## Phase 7.2 — HybridQA Adapter: `prepare`

**Goal:** `uv run python -m evals.hybridqa prepare ...` writes a self-contained 50-question bundle that the Phase 7.1 runner can run unchanged (design §2–3).

- [x] **CLI** (`evals/hybridqa.py`, modelled on `evals/lakequest.py`):

  ```
  uv run python -m evals.hybridqa prepare --hybridqa ../benchmark/HybridQA \
    --config configs/eval/hybridqa_dev50.yaml --per-type 25 --seed 20260101 \
    --out ../benchmark/hybridqa_prepared/dev_s50
  ```

  Other arguments:
  - `--split dev` (default)
  - `--cache ../benchmark/hybridqa_prepared/raw`
  - `--per-type all`
  - `--one-per-table / --no-one-per-table` (default on)

  The config file supplies only `agent.chunking` and `agent.embedding`.
- [x] **Read the labels:**
  - Read `released_data/<split>.traced.json` and `dev_reference.json`, and fail clearly if either is missing.
  - `table` and `passage` come from the reference lists. Questions in neither are excluded and counted.
- [x] **Sample:**
  1. Sort the qids of each type and shuffle each type with its own `random.Random(seed)` stream.
  2. Walk each list, applying the one-per-table rule and skipping tables whose download or parse fails, until `per-type` questions are kept.
  3. Record every skipped qid with its reason.

  With the same seed, a larger sample contains the smaller one.
- [x] **Fetch (cached):**
  - Download `tables_tok/<id>.json` and `request_tok/<id>.json` from `raw.githubusercontent.com/wenhuchen/WikiTables-WithLinks/master/` into `--cache`, reusing cached files.
  - The fetch function is a parameter, so tests pass a local fixture reader with no network.
  - Store each file's sha256.
- [x] **Convert each table** to `tables/<safe_id>/` (HybridQA mapping lives only here):
  - `<safe_id>.csv`: the header names plus each cell's `cell_text`, with links dropped. The safe id is a Windows- and SQL-safe stem.
  - `docs/<Entity>.md`: `# <Entity title>` (URL-decoded, `_` → space), a blank line, then the passage. File names are made safe, and duplicates get a suffix.
  - Build `data.db` with `ingest.load_files`, and `docs.db` with `doc_ingest.build` using the config's chunking and embedding.
  - Write `state.json` with relative `db_path`, `column_map`, `schema_summary`, `docs_db_path` and `docs_summary`, the same keys as the LakeQuest bundle.
  - A table shared by two sampled questions (only with `--no-one-per-table`) is built once.
- [x] **Bundle root:**
  - `dataset.yaml`: `EvalDataset(name=f"hybridqa_{split}_s{n}")`. Each case has:
    - `id = question_id`
    - `tags: [hybridqa, table | passage]`
    - `state_file: tables/<safe_id>/state.json`
    - `expected.reference_answers` and `answer_contains`: `[answer-text]`
    - `expected.object_ids`: the entity names of the `passage` nodes in `answer-node`
  - `reference/dev_reference.json` (`reference`, `table`, `passage`), restricted to the sampled qids.
  - `prepare.json`:
    - source files and hashes, and the HybridQA git commit
    - seed, per-type counts, skipped qids and reasons
    - number of tables, passages and chunks
    - `chunking` and `embedding`
- [x] **Tests** (`tests/test_hybridqa_prepare.py`). The fixture is a mini `released_data`: 6 questions over 3 tables, plus 3 table/request JSON files, with `fake_embed`.
  - the dataset validates as `EvalDataset`, with the requested counts per type
  - the same seed gives the same cases, and a larger `--per-type` contains the smaller sample
  - unlabeled qids are excluded, and a failed fetch is skipped and recorded
  - a CSV header and row match the fixture table; an entity `.md` starts with its title heading
  - a Windows-unsafe entity name becomes a safe file name
  - `state.json` paths are relative and load through `_load_prebuilt`
  - the subset `dev_reference.json` has exactly the sampled qids in the right lists
  - running `run_eval` on the fixture bundle with a fake session gives per-tag means for `table` and `passage`
- [x] **Dev script** `phase_7_2.py`: build the real `dev_s50` bundle (network and embedding API). Print:
  - counts per type and the skipped qids
  - number of tables and chunks
  - one case of each type
  - the first lines of one `schema_summary` and one `docs_summary`

## Phase 7.2a — Selectable Embedding Provider (Gemini or Ollama)

**Goal:** the eval config chooses the embedding provider. Gemini stays the default; `ollama` with `nomic-embed-text:latest` (768 dimensions) is an option that avoids the Gemini embedding rate limit seen while running `prepare` (`ClientError`, Phase 7.2). The choice is per run config, never fixed in code.

- [x] **Config** (`data_agent/config.py`): add `EmbeddingConfig.provider: Literal["gemini", "ollama"] = "gemini"` and `EmbeddingConfig.base_url: str = "http://localhost:11434"` (used by `ollama` only). Defaults equal today's values, so the app, the deployed agent and existing configs are unchanged. `config.py` stays pydantic-only.
- [x] **Embedding call** (`data_agent/doc_ingest.py`, `embed`): branch on `embedding.provider`.
  - `ollama`: `POST {base_url}/api/embed` with `{"model", "input": [...]}` in batches of `embedding.batch`, using the standard library (no new dependency). If the returned vector size differs from `embedding.dim`, return `None` with a warning (a wrong-size index must not be written).
  - Same L2 normalisation and the same `None`-on-failure rule as Gemini. `embed_options` already passes the config, so documents, queries and `retrieval` mode follow the choice with no other change.
- [x] **Bundle guard:** `prepare.json` already stores `embedding`; with `provider` and `model` in it, `_load_prebuilt` rejects a bundle built with another provider. Rebuild the bundle when the provider changes.
- [x] **Adapter** (`evals/hybridqa.py`): no code change expected. `prepare` takes the provider from `--config`; the retry-then-fail rule of Phase 7.2 applies to Ollama too (a stopped server fails clearly).
- [x] **Config files:** keep `hybridqa_dev50.yaml` on Gemini. Add `configs/eval/hybridqa_dev50_ollama.yaml` (same, with `agent.embedding: {provider: ollama, model: "nomic-embed-text:latest", dim: 768}`) and prepare into a separate folder (`dev_s50_ollama`). Gemini and Ollama results are not comparable; keep one provider per result series.
- [x] **Tests** (no network, no Ollama; the HTTP call is patched):
  - `test_config.py`: defaults unchanged; `provider` accepts only `gemini` / `ollama`.
  - `test_doc_ingest.py`: the Ollama branch sends the right request, batches, normalises, and returns `None` on a wrong dimension or a connection error; the Gemini branch is untouched.
  - `test_eval_run.py`: a bundle with a different `embedding.provider` raises before any question.
  - `test_hybridqa_prepare.py`: `prepare` with an Ollama config records the provider in `prepare.json`.
  - All earlier tests pass.
- [x] **Dev script** `phase_7_2a.py`: embed two strings with `provider: ollama` and print the vector size and cosine similarity. Prerequisite: `ollama pull nomic-embed-text` and a running Ollama server.
- [x] **Docs:** one short note each in `dev_docs/design/design.md` (§9) and `README.md` (Evaluation) on the embedding rate limit and the Ollama option. Update the design and README again when the phase is implemented.

## Phase 7.3 — Configuration, Instruction and Scoring

**Goal:** one YAML runs HybridQA with the generic runner, and the official scorer can check its output.

- [ ] **Instruction** `configs/prompts/hybridqa.md` (design §4.1), with these parts:
  - **Sources:** one Wikipedia table and the articles about the entities linked from its cells.
  - **Routing:** SQL first to find the row, then `search_documents` with `source="<Entity>.md"`, repeated for chained hops.
  - **Scope:** answer only from these sources.
  - **Answer format:** the first line holds only the answer; at most 2 justification lines citing `[Entity.md]`; no `Sources:` line.
  - **Rules:** keep the read-only and prompt-injection rules of the built-in instructions.
- [ ] **Run config** `configs/eval/hybridqa_dev50_ollama.yaml` (design §6; created in Phase 7.2a, extended here). Add the same `instruction_file` and `prediction_answer` keys to `hybridqa_dev50.yaml` (Gemini embeddings) so both stay equivalent:
  - `agent.instruction_file: configs/prompts/hybridqa.md`
  - `intent_enabled: false`, `temperature: 0.0`
  - `metrics: [exact_match, squad_f1, answer_contains, coverage, object_f1]`
  - `predictions: true`, `prediction_answer: first_line`
- [ ] **`score` command** (`evals/hybridqa.py`):

  ```
  uv run python -m evals.hybridqa score <run_dir> --reference <bundle>/reference/dev_reference.json
  ```

  It converts `predictions.jsonl` to `[{question_id, pred}]` and scores it with the logic of `evaluate_script.py`. That scorer is MIT-licensed: copy its normalisation into the adapter, or call the script by path. A run whose predictions miss a reference qid fails with a clear message instead of `KeyError`. The command writes `official_scores.json` into the run folder and prints it.
- [ ] **Smoke run:**
  - Run `uv run python -m evals configs/eval/hybridqa_dev50_ollama.yaml dataset.limit=5`.
  - Check that the first line of each answer is a bare value. If not, adjust the instruction once and re-run.
- [ ] **Tests:**
  - `hybridqa_dev50_ollama.yaml` loads.
  - `instruction_file` exists and contains the three placeholders.
  - `score` on a hand-made run folder gives the expected table, passage and total EM/F1, and a missing qid fails cleanly.
  - All earlier tests pass.
- [ ] **Dev script** `phase_7_3.py`: run 2 live cases (one of each type). Print each answer, its first line, the gold answer, the tool calls and `exact_match`/`squad_f1`.

## Phase 7.4 — Run the 50 Questions and Verify

**Goal:** a first internal HybridQA result per question type, confirmed by the official scorer.

- [ ] **Run:** `uv run python -m evals configs/eval/hybridqa_dev50_ollama.yaml`.
- [ ] **Official check:** run `evals.hybridqa score` on the run folder. Our `by_tag` `exact_match` and `squad_f1` (×100) must equal the official `table`, `passage` and `total` EM and F1. A difference is a metric bug, to fix before reading any result.
- [ ] **Read the results:** for each type, look at the lowest-scoring rows in `results.jsonl` and classify each failure:
  - wrong row (SQL)
  - wrong or missing entity article
  - right article, wrong span
  - correct but worded differently (`answer_contains` = 1, EM = 0)
  - error or timeout
- [ ] **Record** in the execution log:
  - the per-type table of design §5.2 (n, EM, F1, `answer_contains`, `object_f1`)
  - error count and p50/p95 latency
  - approximate model calls (embedding runs locally, so it has no API cost; record the `prepare` time instead)
- [ ] **Compatibility re-check:** run `smoke.yaml` and `lakequest_bank.yaml` with `dataset.limit=2`. Their run folders have the same files and keys as before Sprint 7.
- [ ] **Docs:** add a short "HybridQA evaluation" section to `README.md` with the `prepare`, run and `score` commands, and the note that the data is internal-only (no license on WikiTables-WithLinks).
- [ ] **Dev script** `phase_7_4.py`: read a run folder and print our per-tag metrics next to `official_scores.json`.

## Phase 7.5 — Scale Up Through Configuration

**Goal:** larger and repeated runs with **no code change**: only `prepare` arguments and YAML (design §7).

- [ ] **200-question sample:**
  - Prepare with `--config configs/eval/hybridqa_dev50_ollama.yaml --per-type 100 --out ../benchmark/hybridqa_prepared/dev_s200_ollama`, using the same seed (the Ollama embedding settings come from that config).
  - Check in the dev script that it contains all 50 qids of `dev_s50_ollama`.
  - Add `configs/eval/hybridqa_dev200_ollama.yaml` (a copy with `name` and `dataset.path` changed), run it and score it.
- [ ] **Variance:** re-run `hybridqa_dev50_ollama.yaml` with `eval.repeats=3` and record the spread per type.
- [ ] **One ablation, as an override only:** `agent.retrieval.mode=bm25` on `dev_s50_ollama`. Compare per type with the baseline.
- [ ] **Full dev (optional, decide after the 200 run):** estimate the download size and the local embedding time from `prepare.json` of `dev_s200_ollama` and record it. Run `--per-type all --no-one-per-table` only if the time and disk size are accepted.
- [ ] **Tests:** only if a code change was needed. Otherwise record "no code change" in the execution log, and confirm `uv run pytest` still passes.
- [ ] **Dev script** `phase_7_5.py`: compare two run folders (for example `dev_s50_ollama` and `dev_s200_ollama`, or the baseline and `bm25`), printing per-type EM and F1 side by side.

---

## Out of Scope

- **Test-split scoring on CodaLab:** the answers are hidden and a login is needed. `score` can write `test_answers.json` later without new pipeline code.
- **LLM-as-judge metrics, open-corpus table retrieval** (all 3,053 tables in one session), **and a cell-link column in the CSV.** The last is a later ablation inside the adapter only.
- **Hydra and MLflow:** the extension points from Sprint 4 are unchanged.
