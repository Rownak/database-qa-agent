# Sprint 5 — LakeQuest Evaluation (Bank Lake)

**Goal:** evaluate the agent on the LakeQuest **bank** lake with the Sprint 4 evaluation pipeline, as described in [LakeQuest_evaluation_design.md](../design/LakeQuest_evaluation_design.md):

- a `lakequest` agent profile with a concise answer format; the default `shipments` profile and production stay unchanged
- an adapter that builds a leak-free, prebuilt bundle (`bank.db`, `docs.db`, `state.json`, `dataset.yaml`)
- LakeQuest metrics (`answer_f1`, `object_f1`, `coverage`) that match the official scorer, and a `predictions.jsonl` output
- **`bank_sample`**: a 40-case internal evaluation set (20 hybrid, 10 table, 10 document) built from the public snapshot. We are not entering the competition, so the Codabench files are not needed. Scores are internal only, never leaderboard results

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use small fixtures and `fake_embed`, with no network
- a short script `db-doc-qa/dev_test/phase_5_N.py`, run with `uv run python -m dev_test.phase_5_N`
- an entry in `dev_docs/execution_logs/sprint_5_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 5.1 | Config, `lakequest` profile, metrics (done) |
| 5.2 | Adapter: `evals/lakequest.py prepare` (done) |
| 5.3 | `bank_sample` case generation from the snapshot |
| 5.4 | Runner support for prebuilt state, and `predictions.jsonl` |
| 5.5 | Run `bank_sample` and check against the official scorer |

**Rules for all phases**

- The agent never sees the benchmark build tables (`persona_workflows` and the rest). Only the tables in `corpus_objects.parquet` reach `bank.db`. The case generator may read `persona_workflows`; the agent may not.
- Benchmark data stays in `benchmark/` (git-ignored). Nothing from it is committed.
- The `shipments` profile is the default, so the app, the deployed agent and the shipments evaluation keep their behaviour.
- Reuse the existing modules (`doc_ingest.build`, `AgentSession`, `run_eval`, `report`). Do not restructure them.

---

## Phase 5.1 — Data, Profile and Metrics

**Goal:** the agent can be configured for LakeQuest, and the LakeQuest metrics score like the official scorer.

- [x] ~~**Download Open Dev** from Codabench~~ Dropped: registration is pending and not needed. Cases come from the snapshot instead (Phase 5.3).
- [x] **Config** (`data_agent/config.py`):
  - add `AgentConfig.profile: Literal["shipments", "lakequest"] = "shipments"`
  - add `EvalConfig.predictions: bool = False`
  - add `answer_f1`, `object_f1` and `coverage` to `MetricName`
- [x] **Profile** (`data_agent/agent.py`):
  - add `LAKE_INSTRUCTION` (design §6): the tables and policies come from `{schema_summary}` and `{docs_summary}`
  - its rules: quote every table and column name (`"transaction"`); look up a named client with `run_sql` first; use `search_documents` with `source="<policy_id>.md"` when a Policy ID is named
  - its answer format: a direct first sentence, then at most 3 sentences with `[source, §N title]` citations; no headings, SQL or `Sources:` line
  - keep the current grounding and safety rules
  - `build_agent` picks the instruction and description by `cfg.profile`
- [x] **Neutral tool text:** change the `run_sql` docstring from "table ``data``" to "the session's SQLite database; the instruction lists its tables".
- [x] **Dataset fields** (`evals/dataset.py`): add `Expected.reference_answers: list[str] = []` and `Expected.object_ids: list[str] = []`.
- [x] **Metrics** (`evals/metrics.py`):
  - `final_answer(result)` strips a trailing `Sources:` line.
  - `predicted_object_ids(result)` returns the sources of the cited passages, without `.md` (or of all returned passages when nothing is cited), plus the table names after `FROM`/`JOIN` in successful `run_sql` calls. Ids are lower-cased.
  - `answer_f1` is the maximum token F1 over `reference_answers`, using the tokenizer and F1 copied from `score.py` (`[A-Za-z0-9]+`, lower-cased).
  - `object_f1` is the set F1 against `expected.object_ids`.
  - `coverage` is 1 if `final_answer` is non-empty, else 0.
  - `answer_f1` and `object_f1` return `None` when the expected field is empty.
  - Register the three metrics in `METRICS` and `AGENT_METRICS`.
- [x] **Tests:**
  - `test_agent.py`: the default profile still uses `INSTRUCTION`; `profile="lakequest"` uses `LAKE_INSTRUCTION`
  - `test_eval_metrics.py`: each new metric on one passing and one failing hand-made `TurnResult`; `final_answer` removes the `Sources:` line; `predicted_object_ids` reads the cited sources and the SQL tables
  - **parity test:** `answer_f1` equals `score._token_f1` on 5 fixed answer/reference pairs. The test imports `benchmark/LakeQuest/scoring_program/score.py` by path, and is skipped when that file is missing
- [x] **Regression:** run `configs/eval/smoke.yaml`. The shipments metrics must not drop.
- [x] **Dev script** `phase_5_1.py`: print `answer_f1`, `object_f1` and `coverage` for two hand-made results, and the first 300 characters of the `lakequest` instruction.

## Phase 5.2 — Adapter (`evals/lakequest.py prepare`)

**Goal:** one command turns the LakeQuest files into a self-contained bundle that the agent can use as is.

- [x] **CLI:** `uv run python -m evals.lakequest prepare --lake-zip … --questions … [--references …] --domain bank --config <run.yaml> --out <dir>`. The `--config` file supplies the chunking and embedding settings.
- [x] **`bank.db`:**
  - Extract `financial_data_lake.db` to a temporary folder.
  - Copy only the tables with `object_type == "table"` in `lakes/bank/corpus_objects.parquet` (`ATTACH` plus `CREATE TABLE … AS SELECT`).
  - Stop with an error if a whitelisted table is missing.
- [x] **Indexes on `bank.db`:** the lake database has no indexes, and `run_sql` stops a query after 5 seconds. After copying, create an index on every column whose name ends with `_id` in the copied tables (`CREATE INDEX IF NOT EXISTS`). Check on the real lake that a join of `client`, `bank_account` and `transaction` for one client runs well under the 5-second limit.
- [x] **Policies:**
  - Write each row of `policy_documents.jsonl` whose `object_id` is a `policy_document` in `corpus_objects` as `<object_id>.md`, with `# <title>` as its first line.
  - Normalise headings `## N) ` to `## N `.
  - Build `docs.db` with `doc_ingest.build(entries, out, chunking=…, embedding=…)`.
  - Log the number of documents, chunks and skipped documents.
- [x] **`state.json`:** `db_path`, `column_map: {}`, `schema_summary` (per table: name, row count, `column type` list), `docs_db_path`, and `docs_summary` (one line per policy, `<object_id>.md — <title>`). Paths are relative to `state.json`.
- [x] **`dataset.yaml`:**
  - `name`, `version`, `state_file: state.json`
  - one case per question of `--domain`: `id = qa_id`, `question`, `tags = [domain]`
  - when `--references` is given, `expected.reference_answers = [answer_short, answer_long]` (empty values left out) and `expected.object_ids = gold_object_ids`
  - stop with a clear error when a required column is missing
- [x] **`prepare.json`:** the SHA-256 of each source file, the counts, and the `chunking` and `embedding` settings used.
- [x] **Tests** (`tests/test_lakequest_prepare.py`, with a tiny fixture zip built in the test):
  - the fixture has 2 corpus tables, 1 leak table (`persona_workflows`), 2 policies and 3 questions
  - `bank.db` has exactly the 2 corpus tables; `persona_workflows` is absent
  - a normalised heading gets `section_number == "5"` in `docs.db`
  - `state.json` reloads and its paths exist
  - `dataset.yaml` validates as `EvalDataset`
  - without `--references`, every `expected` is empty
- [x] ~~**Dev script** on the real Open Dev bundle~~ Moved to Phase 5.3 (`bank_sample`).

## Phase 5.3 — `bank_sample` Cases from the Snapshot

**Goal:** `prepare` builds the bundle and its cases from the public snapshot alone (design §2–3).

- [x] **CLI:** `--questions` becomes optional. Without it, `prepare` generates `bank_sample`:

  ```
  uv run python -m evals.lakequest prepare --lake-zip ../benchmark/LakeQuest/public_data/public_lake_data.zip \
    --config configs/eval/lakequest_bank.yaml --out ../benchmark/lakequest_prepared/bank_sample
  ```

  The existing Codabench path (`--questions` / `--references`) stays as it is.
- [x] **Generate the cases** (`build_sample_cases`, fixed seed, validation clients from `split_entities.parquet` only):
  - **20 `hybrid`:** `persona_workflows` rows sorted by `qa_id`, read from the lake database (never from `bank.db`). `tools: [run_sql, search_documents]`; `sections` from each "Section N" in `answer_reasoning`; `reference_answers: [answer_direct, answer_reasoning]`; `object_ids: sources + [policy_id]`.
  - **10 `table`:** "How many accounts / transactions / cheques does {first} {last} (client_id: {id}) have?". The count is computed with SQL on `bank.db`. `tools: [run_sql]`; `answer_contains` and `reference_answers` hold the count; `object_ids` are the tables used. A client with a zero count is skipped.
  - **10 `document`:** for the hybrid policies, 5 × "Under policy {id}, which section covers {heading}?" (`answer_contains: [N]`) and 5 × "What is the purpose of policy {id}?" (`reference_answers: [<Purpose paragraph>]`). `tools: [search_documents]`; `sections: [{source: <id>.md, section: N}]`; `object_ids: [<id>]`.
  - `tags: [bank, <type>]`; ids are the `qa_id`, or `tbl-<n>` / `doc-<n>`.
- [x] **`reference/references.jsonl`:** one row per case in the scorer's format (`qa_id`, `domain`, `question`, `answer_short`, `answer_long`, `gold_object_ids`, `gold_provenance_ids: []`).
- [x] **`prepare.json`:** add the seed and the case counts per type.
- [x] **Tests** (`tests/test_lakequest_prepare.py`, extending the existing fixture with `persona_workflows` rows, a validation client and a numbered policy):
  - the generated cases validate as `EvalDataset`, with the expected counts per type
  - a table answer equals the SQL count; a document section number matches the policy heading
  - `persona_workflows` is still absent from `bank.db`
  - `references.jsonl` has the required scorer columns
  - running twice gives the same cases (fixed seed)
- [x] **Dev script** `phase_5_3.py`: build the real `bank_sample` bundle, then print the tables in `bank.db`, the chunk count, the case count per type and one case of each type.

## Phase 5.4 — Runner Support and Predictions

**Goal:** `run_eval` runs a prebuilt bundle without rebuilding or deleting it, and writes `predictions.jsonl`.

- [x] **Runner** (`evals/run.py`):
  - when `state_file` is set, load the state (relative paths resolved) instead of `files_state`, and close sessions with `close(delete_files=False)`
  - **config guard:** raise `ValueError` before the first question if the chunking or embedding in `prepare.json` differs from `cfg.agent`
  - add means per tag (`hybrid`, `table`, `document`) to `metrics.json`
- [x] **Report** (`evals/report.py`): add the predicted `object_ids` to each row; when `eval.predictions` is true, write `predictions.jsonl` (`qa_id`, `answer` = `final_answer`, `object_ids`); copy `prepare.json` into `metrics.json` under `"data"`.
- [x] **Config:** replace `configs/eval/lakequest_bank_dev.yaml` with `configs/eval/lakequest_bank.yaml` (done in Phase 5.3, because `prepare` needs it) (design §5: `profile: lakequest`, `intent_enabled: false`, all seven metrics, `predictions: true`).
- [x] **Tests** (`tests/test_eval_run.py`, fake session and the 5.3 fixture bundle):
  - the session receives the loaded state
  - `bank.db` and `docs.db` survive the run
  - `predictions.jsonl` has one row per case with `qa_id` and `answer`
  - a chunking mismatch raises before any question
  - the per-tag means are present
  - the shipments tests still pass
- [x] **Dev script** `phase_5_4.py`: run `lakequest_bank.yaml` with `dataset.limit=3` on the live agent; print each answer, its `answer_f1` and its `object_ids`.

## Phase 5.5 — Run and Official Check

**Goal:** a first internal result on `bank_sample`, confirmed by the official scorer.

- [ ] **Run all 40 cases:** `uv run python -m evals configs/eval/lakequest_bank.yaml`.
- [ ] **Official scorer:** run `score.py` with `LAKEQUEST_REFERENCE_DIR` set to the bundle's `reference/` folder and `LAKEQUEST_RESULT_DIR` set to the run folder (design §6). Its `answer_f1` must equal ours.
- [ ] **Read the results:** for each type, look at the lowest-scoring row. Classify the failure as wrong SQL, wrong policy or section, correct but differently worded, or too long. If the cause is format, adjust `LAKE_INSTRUCTION` once and re-run.
- [ ] **Record** in the execution log the metrics overall and per type, the error count, p50/p95 latency and the approximate number of model calls.
- [ ] **Docs:** add a short "LakeQuest evaluation" section to `README.md` with the `prepare`, run and official-scorer commands.
- [ ] **Dev script** `phase_5_5.py`: read a run folder and print our metrics next to the official `scores.json`.

---

## Out of Scope

- The `aiml` and `drug` lakes. The same adapter can be extended once the bank pipeline works.
- The Closed Test (code submission). It runs offline without the Gemini API.
- `provenance_f1`, the organiser's LLM judge, and a retrieval-mode "policy recall@k" metric.
- Codabench submissions, and a larger set (`--split test`, more cases). The generator can grow later without new code.
