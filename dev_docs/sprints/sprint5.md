# Sprint 5 — LakeQuest Evaluation (Bank Lake)

**Goal:** evaluate the agent on the LakeQuest **bank** lake with the Sprint 4 evaluation pipeline, as described in [LakeQuest_evaluation_design.md](../design/LakeQuest_evaluation_design.md):

- a `lakequest` agent profile with a concise answer format; the default `shipments` profile and production stay unchanged
- an adapter that builds a leak-free, prebuilt bundle (`bank.db`, `docs.db`, `state.json`, `dataset.yaml`)
- LakeQuest metrics (`answer_f1`, `object_f1`, `coverage`) that match the official scorer, and a `predictions.jsonl` output
- a first baseline on Open Dev

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use small fixtures and `fake_embed`, with no network
- a short script `db-doc-qa/dev_test/phase_5_N.py`, run with `uv run python -m dev_test.phase_5_N`
- an entry in `dev_docs/execution_logs/sprint_5_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 5.1 | Data download, config, `lakequest` profile, metrics |
| 5.2 | Adapter: `evals/lakequest.py prepare` |
| 5.3 | Runner support for prebuilt state, and `predictions.jsonl` |
| 5.4 | Official-scorer check and first baseline |

**Rules for all phases**

- The agent never sees the benchmark build tables (`persona_workflows` and the rest). Only the tables in `corpus_objects.parquet` reach `bank.db`.
- Benchmark data stays in `benchmark/` (git-ignored). Nothing from it is committed.
- The `shipments` profile is the default, so the app, the deployed agent and the shipments evaluation keep their behaviour.
- Reuse the existing modules (`doc_ingest.build`, `AgentSession`, `run_eval`, `report`). Do not restructure them.

---

## Phase 5.1 — Data, Profile and Metrics

**Goal:** the agent can be configured for LakeQuest, and the LakeQuest metrics score like the official scorer.

- [ ] **Download Open Dev** from Codabench into `benchmark/LakeQuest/open_dev/input/` (questions) and `benchmark/LakeQuest/open_dev/reference/` (references). Record in the execution log:
  - the real column names of `questions.*` and `references.*`
  - the number of questions per `domain`

  If the columns differ from the design (`qa_id`, `question`, `domain`), update the design before Phase 5.2.
- [ ] **Config** (`data_agent/config.py`):
  - add `AgentConfig.profile: Literal["shipments", "lakequest"] = "shipments"`
  - add `EvalConfig.predictions: bool = False`
  - add `answer_f1`, `object_f1` and `coverage` to `MetricName`
- [ ] **Profile** (`data_agent/agent.py`):
  - add `LAKE_INSTRUCTION` (design §6): the tables and policies come from `{schema_summary}` and `{docs_summary}`
  - its rules: quote every table and column name (`"transaction"`); look up a named client with `run_sql` first; use `search_documents` with `source="<policy_id>.md"` when a Policy ID is named
  - its answer format: a direct first sentence, then at most 3 sentences with `[source, §N title]` citations; no headings, SQL or `Sources:` line
  - keep the current grounding and safety rules
  - `build_agent` picks the instruction and description by `cfg.profile`
- [ ] **Neutral tool text:** change the `run_sql` docstring from "table ``data``" to "the session's SQLite database; the instruction lists its tables".
- [ ] **Dataset fields** (`evals/dataset.py`): add `Expected.reference_answers: list[str] = []` and `Expected.object_ids: list[str] = []`.
- [ ] **Metrics** (`evals/metrics.py`):
  - `final_answer(result)` strips a trailing `Sources:` line.
  - `predicted_object_ids(result)` returns the sources of the cited passages, without `.md` (or of all returned passages when nothing is cited), plus the table names after `FROM`/`JOIN` in successful `run_sql` calls. Ids are lower-cased.
  - `answer_f1` is the maximum token F1 over `reference_answers`, using the tokenizer and F1 copied from `score.py` (`[A-Za-z0-9]+`, lower-cased).
  - `object_f1` is the set F1 against `expected.object_ids`.
  - `coverage` is 1 if `final_answer` is non-empty, else 0.
  - `answer_f1` and `object_f1` return `None` when the expected field is empty.
  - Register the three metrics in `METRICS` and `AGENT_METRICS`.
- [ ] **Tests:**
  - `test_agent.py`: the default profile still uses `INSTRUCTION`; `profile="lakequest"` uses `LAKE_INSTRUCTION`
  - `test_eval_metrics.py`: each new metric on one passing and one failing hand-made `TurnResult`; `final_answer` removes the `Sources:` line; `predicted_object_ids` reads the cited sources and the SQL tables
  - **parity test:** `answer_f1` equals `score._token_f1` on 5 fixed answer/reference pairs. The test imports `benchmark/LakeQuest/scoring_program/score.py` by path, and is skipped when that file is missing
- [ ] **Regression:** run `configs/eval/smoke.yaml`. The shipments metrics must not drop.
- [ ] **Dev script** `phase_5_1.py`: print `answer_f1`, `object_f1` and `coverage` for two hand-made results, and the first 300 characters of the `lakequest` instruction.

## Phase 5.2 — Adapter (`evals/lakequest.py prepare`)

**Goal:** one command turns the LakeQuest files into a self-contained bundle that the agent can use as is.

- [ ] **CLI:** `uv run python -m evals.lakequest prepare --lake-zip … --questions … [--references …] --domain bank --config <run.yaml> --out <dir>`. The `--config` file supplies the chunking and embedding settings.
- [ ] **`bank.db`:**
  - Extract `financial_data_lake.db` to a temporary folder.
  - Copy only the tables with `object_type == "table"` in `lakes/bank/corpus_objects.parquet` (`ATTACH` plus `CREATE TABLE … AS SELECT`).
  - Stop with an error if a whitelisted table is missing.
- [ ] **Policies:**
  - Write each row of `policy_documents.jsonl` whose `object_id` is a `policy_document` in `corpus_objects` as `<object_id>.md`, with `# <title>` as its first line.
  - Normalise headings `## N) ` to `## N `.
  - Build `docs.db` with `doc_ingest.build(entries, out, chunking=…, embedding=…)`.
  - Log the number of documents, chunks and skipped documents.
- [ ] **`state.json`:** `db_path`, `column_map: {}`, `schema_summary` (per table: name, row count, `column type` list), `docs_db_path`, and `docs_summary` (one line per policy, `<object_id>.md — <title>`). Paths are relative to `state.json`.
- [ ] **`dataset.yaml`:**
  - `name`, `version`, `state_file: state.json`
  - one case per question of `--domain`: `id = qa_id`, `question`, `tags = [domain]`
  - when `--references` is given, `expected.reference_answers = [answer_short, answer_long]` (empty values left out) and `expected.object_ids = gold_object_ids`
  - stop with a clear error when a required column is missing
- [ ] **`prepare.json`:** the SHA-256 of each source file, the counts, and the `chunking` and `embedding` settings used.
- [ ] **Tests** (`tests/test_lakequest_prepare.py`, with a tiny fixture zip built in the test):
  - the fixture has 2 corpus tables, 1 leak table (`persona_workflows`), 2 policies and 3 questions
  - `bank.db` has exactly the 2 corpus tables; `persona_workflows` is absent
  - a normalised heading gets `section_number == "5"` in `docs.db`
  - `state.json` reloads and its paths exist
  - `dataset.yaml` validates as `EvalDataset`
  - without `--references`, every `expected` is empty
- [ ] **Dev script** `phase_5_2.py`: prepare the real Open Dev bank bundle into `benchmark/lakequest_prepared/bank_dev/`, then print the tables in `bank.db`, the number of chunks, the case count and the first 5 lines of `docs_summary`.

## Phase 5.3 — Runner Support and Predictions

**Goal:** `run_eval` runs a prebuilt bundle without rebuilding or deleting it, and writes `predictions.jsonl`.

- [ ] **Dataset:** add `EvalDataset.state_file: Path | None = None`, resolved relative to the dataset file.
- [ ] **Runner** (`evals/run.py`):
  - when `state_file` is set, load the state (resolving relative paths) instead of `files_state`, and close every session with `close(delete_files=False)`
  - **config guard:** if the bundle's `prepare.json` chunking or embedding differs from `cfg.agent`, raise `ValueError` before the first question
- [ ] **Report** (`evals/report.py`):
  - add the predicted `object_ids` to each `results.jsonl` row
  - when `eval.predictions` is true, write `predictions.jsonl` with `{"qa_id": id, "answer": final_answer, "object_ids": [...]}` per case
  - copy `prepare.json` into `metrics.json` under `"data"`
- [ ] **Config:** add `configs/eval/lakequest_bank_dev.yaml` (design §5: `profile: lakequest`, `intent_enabled: false`, `limit: 50`, the four metrics, `predictions: true`).
- [ ] **Tests** (`tests/test_eval_run.py`, with a fake session and a bundle from the Phase 5.2 fixture):
  - the fake session receives the loaded state, with absolute paths
  - `bank.db` and `docs.db` still exist after the run
  - `predictions.jsonl` has one valid row per case, and the rows match `starter_kit/predictions.schema.json` (`qa_id` and `answer` are strings; `object_ids` is a list)
  - a chunking mismatch with `prepare.json` raises before any question is asked
  - the shipments `run_eval` tests still pass unchanged
- [ ] **Dev script** `phase_5_3.py`: run `lakequest_bank_dev.yaml` with `dataset.limit=3` on the live agent, then print each answer, its `answer_f1` and the predicted `object_ids`.

## Phase 5.4 — Official Check and Baseline

**Goal:** our scores agree with the official scorer, and a first baseline is recorded.

- [ ] **Run 10 questions:** `uv run python -m evals configs/eval/lakequest_bank_dev.yaml dataset.limit=10`.
- [ ] **Official scorer:** run `benchmark/LakeQuest/scoring_program/score.py` with `LAKEQUEST_RESULT_DIR` set to the run folder (design §7).
  - Filter its `per_item_scores.csv` to the 10 questions that ran.
  - Check that the mean `answer_f1` and `object_f1` equal ours, then record both in the log.
- [ ] **Read the results:** look at the 3 lowest-scoring rows in `results.jsonl`. Classify each failure as wrong SQL, wrong policy, a correct but differently worded answer, or too long. If the cause is format, adjust `LAKE_INSTRUCTION` once and re-run the 10 questions.
- [ ] **Baseline:** run `limit: 50`. Record `answer_f1`, `object_f1`, `coverage`, `citation_precision`, the error count, p50/p95 latency and the approximate number of model calls.
- [ ] **Decide on the full run:** decide from the cost of the baseline. Record the decision in the log.
- [ ] **Docs:** add a short "LakeQuest evaluation" section to `README.md`: the download location, the `prepare` command, the run command and the official-scorer command.
- [ ] **Dev script** `phase_5_4.py`: read a run folder and print our metrics side by side with the official `scores.json` filtered to the same questions.

---

## Out of Scope

- The `aiml` and `drug` lakes. The same adapter can be extended once the bank pipeline works.
- The Closed Test (code submission). It runs offline without the Gemini API.
- `provenance_f1`, the organiser's LLM judge, and a retrieval-mode "policy recall@k" metric.
- Uploading to Codabench (manual: zip `predictions.jsonl` from an Open Test run).
