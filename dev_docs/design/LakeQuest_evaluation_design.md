# LakeQuest Evaluation — Technical Design

This design describes how to evaluate the existing agent on the LakeQuest benchmark (`benchmark/LakeQuest/`) with the Sprint 4 evaluation pipeline (`evals/`, `configs/`, `data_agent/runner.py`). It adds one adapter module and small extensions. The agent, the app and the deployed services keep their behaviour.

---

## 1. Scope

| In scope | Out of scope (and why) |
|---|---|
| **Bank lake** (`bank`). Its questions combine client records (SQLite) with policy documents, which is the same shape as our shipments use case: look up a record, then apply the policy | **`aiml` and `drug` lakes.** These are document collections (about 4,500 model/dataset cards; DrugBank tables plus passages) with a different structure. They can follow later with the same adapter |
| **Open Dev:** local scoring with the downloaded references | **Closed Test (code submission).** It runs offline in the organiser's Docker image without internet, so the Gemini API (agent, embeddings) is unavailable |
| **Open Test:** produce `predictions.jsonl` for upload (answers are hidden) | **The organiser's optional LLM judge** (OpenRouter) |

## 2. LakeQuest Facts That Drive the Design

- **Questions and references are not in this repository.**
  - `public_data/public_lake_data.zip` contains only the lake data: `lakes/<lake>/{manifest.json, corpus_objects.parquet, split_entities.parquet}` and `raw_lake_snapshot/`.
  - The Open Dev questions (`questions.jsonl`/`.parquet`) and references (`references.jsonl`/`.parquet`) must be downloaded from Codabench.
- **Prediction row:** `{"qa_id", "answer", "object_ids"?, "provenance_ids"?}`, one per line in `predictions.jsonl`.
- **Reference columns used by the scorer:** `qa_id`, `domain`, `answer_short`, `answer_long`, `gold_object_ids`, `gold_provenance_ids`.
- **Scoring (`scoring_program/score.py`):**
  - `answer_f1` is the maximum token F1 of the answer against `answer_short` and against `answer_long`. Tokens are `[A-Za-z0-9]+`, lower-cased.
  - `overall_score` equals `answer_f1`.
  - `object_f1` and `provenance_f1` are set F1 scores, reported as diagnostics only.
  - `coverage` is the share of questions with a non-empty answer.
  - Results are also broken down per domain.
  - The verbosity penalty that matters: token F1 punishes long answers through precision.
- **Bank lake content:**
  - `structured/financial_data_lake.db` is a 40 MB SQLite database.
  - `corpus_objects.parquet` lists 8 tables (`client`, `bank_account`, `transaction`, `cheque`, `bank_branch`, `dialog_session`, `geo_location`, `targets`) and 212 `policy_document` objects.
  - `policy_documents.jsonl` holds 215 rows of `{object_id, title, summary, document}`. The documents are Markdown, 825–1,449 words each and about 233k words in total, with headings like `## 5) Required Safeguards`.
  - The questions name clients ("Mark Johnson (client_id: 748)") and policies ("Policy ID deceased-estate-servicing Section 5").
- **⚠ Answer leakage in the database:** besides the corpus tables, the file contains benchmark build tables. One is `persona_workflows`, which holds `qa_id`, `question`, `answer_direct`, `answer_reasoning` and `ground_truth`. Others are `persona_workflow_policies`, `answer_intent_*`, `yes_no_distribution`, `filtering_*`, `question_counts_*` and `policy_workflow_*`. **The agent must never see these tables.**

## 3. Compatibility Gaps and the Smallest Changes

| # | Gap (current code) | Smallest change |
|---|---|---|
| G1 | Questions and references are not in the repository | Download Open Dev input and reference data manually to `benchmark/LakeQuest/open_dev/{input,reference}/` (`benchmark/` is already git-ignored) |
| G2 | The bank database contains answer tables | The adapter copies **only** the tables listed as `object_type == "table"` in `corpus_objects.parquet` into a new `bank.db`. This is a whitelist taken from the data, not hard-coded. A test asserts that `persona_workflows` is absent |
| G3 | The agent expects one table `data`, built per session from a CSV of at most 500 KB (`ingest.load_file`) | **No core change.** `ensure_data` already skips building when the state has `db_path`, `column_map` and `schema_summary` and the file exists, and `run_sql` (read-only, `mode=ro`) can query any table in that file. The adapter writes a multi-table `schema_summary` (table, columns with types, row count) and `column_map = {}` |
| G4 | `INSTRUCTION`, the agent description and the `run_sql` docstring are specific to shipments and to table `data`, and the answer format (sections, SQL, `Sources:` line) lowers token F1 | Add `AgentConfig.profile: Literal["shipments", "lakequest"] = "shipments"`. `build_agent` selects `LAKE_INSTRUCTION` (§6) for `lakequest`. Make the `run_sql` docstring neutral ("the session's SQLite database; the instruction lists its tables"). The default profile keeps production unchanged |
| G5 | The intent classifier prompt is about shipments | No change: `agent.intent_enabled: false` in the LakeQuest config |
| G6 | Documents are limited to 3 per session and `docs.db` is rebuilt (and re-embedded) for every session; the lake has 212 policies (about 1.5–2k chunks) | The adapter builds `docs.db` **once** with the existing `doc_ingest.build` (the 3-document limit is enforced only in the UI). Each case reuses it through `docs_db_path` in the state, which `ensure_data` already accepts |
| G7 | `docs_summary` (one line per document with up to 8 sections) would be about 212 long lines in the instruction | The adapter writes a compact `docs_summary`: one line per policy, `<object_id>.md — <title>` |
| G8 | Headings `## 5) Purpose` do not match the numbered-heading rule, so `section_number` is empty and citations cannot name `§5` | The adapter normalises `## N) ` to `## N ` and adds `# <title>` as the first line when writing each policy as `<object_id>.md`. No chunker change |
| G9 | `run_eval` always builds the state from files (`files_state`), and `AgentSession.close()` deletes `Path(db_path).parent`, which would delete the shared bundle | Add `EvalDataset.state_file: Path \| None` (a JSON file of the prebuilt state). When it is set, `run.py` loads it instead of calling `files_state` and closes sessions with `close(delete_files=False)` |
| G10 | `Expected` has no reference answers or object ids, and there is no `answer_f1`, `object_f1` or `coverage` metric | Add `Expected.reference_answers: list[str]` and `Expected.object_ids: list[str]`, three metrics (§7) and their names in `MetricName` |
| G11 | There is no `predictions.jsonl` output | Add `EvalConfig.predictions: bool = False`. When it is true, `report.write_run` also writes `predictions.jsonl` in the LakeQuest format |
| G12 | The table `transaction` is an SQL keyword | `LAKE_INSTRUCTION` says to wrap every table and column name in double quotes (the existing rule already does this for columns) |

Nothing else in `data_agent/` changes. Ingestion, retrieval, tools, sessions, the UI and the deployment files stay as they are.

## 4. Dataset Adaptation (`evals/lakequest.py`, new)

Run `uv run python -m evals.lakequest prepare` once per lake and split. It reuses the existing ingestion and writes a self-contained bundle:

```
uv run python -m evals.lakequest prepare \
  --lake-zip ../benchmark/LakeQuest/public_data/public_lake_data.zip \
  --questions ../benchmark/LakeQuest/open_dev/input \
  --references ../benchmark/LakeQuest/open_dev/reference \
  --domain bank --config configs/eval/lakequest_bank_dev.yaml \
  --out ../benchmark/lakequest_prepared/bank_dev
```

| Output | How it is built |
|---|---|
| `bank.db` | The whitelisted corpus tables copied from `financial_data_lake.db` (`ATTACH` plus `CREATE TABLE … AS SELECT`, done offline by the adapter, never by the agent) |
| `docs.db` | Each policy written as `<object_id>.md` (G8), then `doc_ingest.build(entries, out, chunking=cfg.agent.chunking, embedding=cfg.agent.embedding)`. This is one embedding pass of about 20 batch calls |
| `state.json` | `db_path`, `column_map: {}`, `schema_summary`, `docs_db_path`, `docs_summary`. Paths are stored relative to `state.json` and resolved on load |
| `dataset.yaml` | An `EvalDataset` with `state_file: state.json`, one case per question, and `domain` in the tags (see the mapping below) |
| `prepare.json` | Source file SHA-256s, question and case counts, and the `chunking` and `embedding` settings used |

Each case is built from one question:

- `id` is the `qa_id`.
- `question` is the question text.
- `tags` is `[domain]`.
- `expected.reference_answers` is `[answer_short, answer_long]`, with empty values left out.
- `expected.object_ids` is `gold_object_ids`.

Without `--references` (Open Test), `expected` is left empty. The adapter reads the `qa_id`, `question` and `domain` columns from `questions.*`, and the reference columns from `references.*`. It stops with a clear error if a column is missing.

**Config guard:** `run_eval` compares the `chunking` and `embedding` settings recorded in `prepare.json` with the run config. If they differ, it fails, so a stale `docs.db` is never scored silently.

## 5. YAML Configuration

`configs/eval/lakequest_bank_dev.yaml`:

```yaml
name: lakequest_bank_dev
agent:
  profile: lakequest            # NEW: LAKE_INSTRUCTION (shipments is the default)
  intent_enabled: false         # the classifier prompt is about shipments
  model:
    agent: gemini-3.5-flash-lite
    temperature: 0.0
  retrieval:
    mode: hybrid
    max_top_k: 8
dataset:
  path: ../benchmark/lakequest_prepared/bank_dev/dataset.yaml
  limit: 50                     # start small: about 3 model calls per question
eval:
  mode: agent
  metrics: [answer_f1, object_f1, coverage, citation_precision]
  predictions: true             # NEW: also write predictions.jsonl
```

Open Test uses the same file with a different `dataset.path` and `metrics: [coverage, citation_precision]`. Overrides work as usual, for example `dataset.limit=10 agent.retrieval.mode=bm25`.

## 6. Agent Execution

- **Per case:** `run_eval` builds a new `AgentSession(cfg.agent, state)` from `state.json`, calls `ask`, then `close(delete_files=False)`. Sessions don't share history, and the bundle stays read-only and shared. `ensure_data` finds both databases ready and builds nothing. The tools open them with `mode=ro`.
- **`LAKE_INSTRUCTION`** keeps the same tools, the grounding and safety rules (9–13 of the current instruction) and the `[source, §number title]` citation form. It changes the following:
  - **Data sources:** "a read-only SQLite database with these tables: `{schema_summary}`" and "bank policy documents: `{docs_summary}`".
  - **SQL:** wrap every table and column name in double quotes (`"transaction"`) and join on `client_id` / `account_id`.
  - **Routing:** a question that names a client goes to `run_sql` first. A question that names a Policy ID uses `search_documents` with `source="<policy_id>.md"`, and a policy question with no ID uses an unfiltered search.
  - **Answer format:** the first sentence is the direct answer (Yes/No, a value, or an action). Then give at most three sentences of justification with inline citations. No headings, no SQL in the answer, and no `Sources:` line.
- **Answer post-processing (shared by the metric and `predictions.jsonl`):** `final_answer(result)` removes a trailing `Sources:` line if the model still writes one. This makes our `answer_f1` match the official scorer on the same text.
- **Rate and cost:** cases run one after another, as today. About 3 LLM calls per question plus one query embedding, so 50 questions is about 150 calls. Run the full set only after the first results look right.

## 7. Metrics

| Metric | Source | Definition |
|---|---|---|
| `answer_f1` (primary) | New, a copy of `score.py` | `max(token_f1(ref, final_answer))` over `expected.reference_answers`, using the scorer's exact tokenizer. `None` when there are no references (Open Test) |
| `object_f1` | New, a copy of `score.py` | Set F1 between `predicted_object_ids(result)` and `expected.object_ids`. Predicted ids are the sources of the cited passages (without `.md`), or of all returned passages if the answer has no citations, together with the table names after `FROM`/`JOIN` in successful `run_sql` queries |
| `coverage` | New | 1 if `final_answer` is non-empty, else 0. Its mean is the scorer's coverage |
| `citation_precision` | Existing | Unchanged. It checks that every citation points to a passage returned in this turn (grounding) |

Not used for LakeQuest:
- `tool_sequence_match`, `section_recall`, `answer_contains`, `mrr`: there are no gold tools or sections and no expected substrings.
- `provenance_f1`: the gold provenance ids are internal and not released for prediction.
- Retrieval mode: it needs section-level gold.

A later "policy recall@k" (gold policy ids among the retrieved sources) would make retrieval mode usable here too.

**Official cross-check:** the run folder is a valid `LAKEQUEST_RESULT_DIR`, so the organiser's scorer can be run on it unchanged:

```
LAKEQUEST_REFERENCE_DIR=../benchmark/LakeQuest/open_dev/reference \
LAKEQUEST_RESULT_DIR=runs/<run> LAKEQUEST_SCORE_OUTPUT_DIR=runs/<run>/official \
python ../benchmark/LakeQuest/scoring_program/score.py
```

On the same questions, its `answer_f1` must equal ours. A test checks this parity on fixed strings, and it is skipped when `score.py` is not present. The scorer averages over **all** reference rows, so with `dataset.limit` set its score only matches ours after filtering its `per_item_scores.csv` to the questions that ran.

## 8. Saved Results (`runs/<timestamp>_lakequest_bank_dev/`)

| File | Content |
|---|---|
| `config.yaml` | The resolved `RunConfig` (existing) |
| `results.jsonl` | One row per case (existing), plus `object_ids`, the predicted ids, for error analysis |
| `metrics.json` | Means of `answer_f1`, `object_f1`, `coverage` and `citation_precision`, plus counts and latency (existing format). Also `prepare.json` copied from the bundle, so the run records which data snapshot it used |
| `predictions.jsonl` | **New when `eval.predictions` is true:** `{"qa_id", "answer": final_answer, "object_ids"}` per case. For Open Test, zip this file for Codabench |
| `official/` | Optional output of the cross-check: `scores.json` and `per_item_scores.csv` |

## 9. Code Change Summary

| File | Change | Size |
|---|---|---|
| `data_agent/config.py` | `AgentConfig.profile`; `EvalConfig.predictions`; `answer_f1`, `object_f1` and `coverage` in `MetricName` | ~5 lines |
| `data_agent/agent.py` | `LAKE_INSTRUCTION`; `build_agent` selects the instruction and description by `profile` | ~40 lines (mostly the prompt) |
| `data_agent/tools.py` | Neutral `run_sql` docstring | 2 lines |
| `evals/dataset.py` | `Expected.reference_answers` and `object_ids`; `EvalDataset.state_file` (relative paths resolved against the dataset file) | ~10 lines |
| `evals/metrics.py` | `final_answer`, `predicted_object_ids`, `answer_f1`, `object_f1`, `coverage` | ~60 lines |
| `evals/run.py` | Prebuilt-state branch, `close(delete_files=False)`, `prepare.json` config guard | ~20 lines |
| `evals/report.py` | `predictions.jsonl` writer; copy `prepare.json` into `metrics.json` | ~15 lines |
| `evals/lakequest.py` | **New:** `prepare` (table whitelist, Markdown export, `doc_ingest.build`, state and dataset writing) | ~150 lines |
| `configs/eval/lakequest_bank_dev.yaml` | **New** | — |

## 10. Implementation Checklist

- [ ] **Data:** download the Open Dev input and reference data into `benchmark/LakeQuest/open_dev/`. Confirm the column names in `questions.*` and `references.*` match §4.
- [ ] **Config and profile:** add `profile`, `predictions` and the three metric names. Add `LAKE_INSTRUCTION` and select it in `build_agent`. Neutralise the `run_sql` docstring.
  - Tests: the default profile builds the current instruction, and `profile: lakequest` builds the new one.
  - Re-run the shipments smoke evaluation and confirm its metrics have not dropped.
- [ ] **Adapter:** `evals/lakequest.py prepare`.
  - Tests use a tiny fixture zip with 2 tables, 1 leak table, 2 policies and 3 questions, together with `fake_embed`.
  - They check that only whitelisted tables are in `bank.db` and that `persona_workflows` is absent.
  - They check that the headings get section numbers, that `state.json` reloads, that `dataset.yaml` validates, and that Open Test (no references) leaves `expected` empty.
- [ ] **Runner and dataset:** `state_file`, the prebuilt-state branch, `close(delete_files=False)` and the config guard.
  - Tests: a fake session receives the loaded state, the bundle survives the run, and a chunking mismatch raises an error.
- [ ] **Metrics and report:** `final_answer`, `answer_f1`, `object_f1`, `coverage` and `predictions.jsonl`.
  - Tests use hand-made `TurnResult`s and check parity with `score.py` (skipped if it is absent).
- [ ] **Dev script** `dev_test/lakequest_bank.py`: prepare the bundle, run `limit: 10`, run the official scorer on the run folder, and print both scores.
- [ ] **First baseline:** run `limit: 50`. Record `answer_f1`, `object_f1`, `coverage`, cost and latency in the execution log, then decide whether to run the full set.

## 11. Risks and Open Points

- **Token F1 rewards wording, not correctness.** A correct but differently phrased answer scores low. Read the low-scoring rows in `results.jsonl`, and treat `answer_f1` as a relative signal between runs.
- **Instruction length:** the schema plus 212 title lines is about 5k tokens per call. That is acceptable for flash-lite. If it hurts latency or cost, replace the list with a count and rely on search.
- **Leakage beyond tables:** the policy texts and the corpus tables are participant-facing by design. Only the build tables are excluded. Re-check the whitelist if the snapshot version changes, since `prepare.json` stores the hashes.
- **To confirm:**
  - the exact column names in the downloaded `questions.*` (assumed `qa_id`, `question`, `domain`)
  - whether Open Dev contains only bank questions or all three lakes (the adapter filters by `--domain`)
