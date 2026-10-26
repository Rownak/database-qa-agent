# LakeQuest Evaluation — Technical Design (`bank_sample`)

This design evaluates the existing agent on the LakeQuest **bank** lake for internal use. We are not entering the competition. The Codabench questions and references are not needed: an evaluation set, **`bank_sample`**, is built from the public snapshot already in `benchmark/LakeQuest/public_data/public_lake_data/`. It is run with the Sprint 4 pipeline (`evals/`, `configs/`, `AgentSession`).

The scores are internal only. Some of the questions are probably in the hidden LakeQuest test set, so they must not be reported as leaderboard results.

---

## 1. Source Data (bank lake)

All paths are under `benchmark/LakeQuest/public_data/public_lake_data/`.

| File | Content | Used for |
|---|---|---|
| `raw_lake_snapshot/lakes/bank/structured/financial_data_lake.db` | 40 MB SQLite: 8 corpus tables (`client`, `bank_account`, `transaction`, `cheque`, …) **plus benchmark build tables** | The agent's database (corpus tables only), and the hybrid Q&A |
| `raw_lake_snapshot/lakes/bank/policy_documents.jsonl` | 215 policies `{object_id, title, summary, document}`. Markdown, about 1,000 words each, headings `## N) Title` | The agent's documents, and the document questions |
| `lakes/bank/corpus_objects.parquet` | The official object ids: 8 tables and 212 policies | The table whitelist and `object_ids` |
| `lakes/bank/split_entities.parquet` | `client:<id>` mapped to `validation` / `test` | Picks the clients for the sample |
| `scoring_program/score.py` (repository root) | The official scorer | Cross-check of `answer_f1` |

**Hybrid Q&A:** `persona_workflows` (a build table in the database) has 275 questions. Each one needs a client record and a policy.
- Fields: `question`, `answer_direct` (short, about 8 words), `answer_reasoning` (about 146 words, cites "Section N"), `client_id`, `policy_id` (26 policies), `sources` (the tables used) and `ground_truth` (SQL facts).
- Checked: every `policy_id` exists in `policy_documents.jsonl`, and the `ground_truth` counts match SQL on the database.
- 58 of the questions are about **validation** clients.

> **Leak rule.** `persona_workflows` and the other build tables (`persona_workflow_policies`, `answer_intent_*`, `yes_no_distribution`, `filtering_*`, `question_counts_*`, `policy_workflow_*`) are read **only by the dataset builder**. The agent's `bank.db` contains only the tables listed in `corpus_objects.parquet`.

## 2. `bank_sample` (40 cases)

`evals/lakequest.py prepare` generates the cases deterministically, with a fixed seed and only validation clients. Each case gets a `tags` entry for its type.

| Type | Count | Question | Expected (existing fields + 2 new) |
|---|---|---|---|
| `hybrid` | 20 | `persona_workflows` rows of validation clients, sorted by `qa_id`, first 20 | `tools: [run_sql, search_documents]`; `sections`: each "Section N" in `answer_reasoning` → `{source: <policy_id>.md, section: N}`; `reference_answers: [answer_direct, answer_reasoning]`; `object_ids: sources + [policy_id]` |
| `table` | 10 | Templates on the same clients, answered by SQL in the builder: "How many accounts / transactions / cheques does {first} {last} (client_id: {id}) have?" | `tools: [run_sql]`; `answer_contains: ["<count>"]`; `reference_answers: ["<count>"]`; `object_ids`: the tables queried (`client`, `bank_account`, plus `transaction` or `cheque`) |
| `document` | 10 | Templates on the policies of the hybrid cases. 5 × "Under policy {id}, which section covers {heading title}?" and 5 × "What is the purpose of policy {id}?" | `tools: [search_documents]`; `sections: [{source: <id>.md, section: N}]` (`"1"` for purpose questions); section questions add `answer_contains: ["<N>"]`, purpose questions add `reference_answers: [<Purpose paragraph>]`; `object_ids: [<id>]` |

Each case has `id` (the `qa_id`, or `tbl-<n>` / `doc-<n>`), `question`, `tags` (`[bank, <type>]`) and `expected`. The builder rejects a template answer that it cannot compute (for example a client without accounts), so every expected value is exact.

## 3. What the Builder Writes

```
uv run python -m evals.lakequest prepare \
  --lake-dir ../benchmark/LakeQuest/public_data/public_lake_data \
  --config configs/eval/lakequest_bank.yaml \
  --out ../benchmark/lakequest_prepared/bank_sample
```

| Output | How it is built |
|---|---|
| `bank.db` | The tables of `corpus_objects` with `object_type == "table"`, copied from `financial_data_lake.db` (`ATTACH` plus `CREATE TABLE … AS SELECT`). A missing table is an error |
| `docs.db` | Every corpus policy written as `<object_id>.md`, starting with `# <title>`, with headings `## N) ` normalised to `## N ` so that `section_number` is set. Built with `doc_ingest.build(…, chunking=cfg.agent.chunking, embedding=cfg.agent.embedding)` in one embedding pass |
| `state.json` | `db_path`, `column_map: {}`, `schema_summary` (table, row count, `column type` list), `docs_db_path`, and `docs_summary` (one line per policy: `<object_id>.md — <title>`). Paths are relative |
| `dataset.yaml` | An `EvalDataset` with `state_file: state.json` and the 40 cases |
| `reference/references.jsonl` | The same cases in the scorer's format: `qa_id`, `domain: bank`, `question`, `answer_short`, `answer_long` (the reference answers, or the `answer_contains` value), `gold_object_ids`, `gold_provenance_ids: []` |
| `prepare.json` | Source SHA-256s, case counts per type, the seed, and the `chunking` and `embedding` settings |

`benchmark/` is git-ignored, so none of this is committed.

## 4. Gaps in the Current Code and the Smallest Changes

| Gap | Change |
|---|---|
| The agent expects one table `data` built per session from a small CSV | **None in the core.** `ensure_data` already uses a prebuilt `db_path`/`schema_summary` from the state, and `run_sql` (read-only) can query any table in that file |
| 212 policies cannot be uploaded per session (3-document limit, re-embedding on every session) | **None in the core.** `docs.db` is built once by the builder and passed through `docs_db_path`, which `ensure_data` already accepts |
| The instruction, agent description and `run_sql` docstring are about shipments and table `data`; the long answer format hurts token F1 | `AgentConfig.profile: Literal["shipments", "lakequest"] = "shipments"`, which selects `LAKE_INSTRUCTION` (§5). The `run_sql` docstring becomes neutral. The default keeps the app and production unchanged |
| The intent classifier prompt is about shipments | `intent_enabled: false` in the config |
| `run_eval` builds the state from files, and `AgentSession.close()` deletes `Path(db_path).parent` (the shared bundle) | `EvalDataset.state_file`. When it is set, `run.py` loads it and closes sessions with `close(delete_files=False)` |
| `Expected` has no reference answers or object ids | `Expected.reference_answers: list[str] = []` and `Expected.object_ids: list[str] = []` |
| There are no LakeQuest metrics or prediction file | Add `answer_f1`, `object_f1` and `coverage` (§6), and `EvalConfig.predictions: bool` to write `predictions.jsonl` |
| `transaction` is an SQL keyword | `LAKE_INSTRUCTION`: wrap every table and column name in double quotes |

## 5. Configuration and Execution

`configs/eval/lakequest_bank.yaml`:

```yaml
name: lakequest_bank_sample
agent:
  profile: lakequest            # NEW
  intent_enabled: false
  model:
    agent: gemini-3.5-flash-lite
    temperature: 0.0
  retrieval:
    mode: hybrid
dataset:
  path: ../benchmark/lakequest_prepared/bank_sample/dataset.yaml
eval:
  mode: agent
  metrics: [tool_sequence_match, section_recall, answer_contains, citation_precision,
            answer_f1, object_f1, coverage]
  predictions: true             # NEW
```

Run it with `uv run python -m evals configs/eval/lakequest_bank.yaml`. Add `dataset.limit=5` for a quick check.

**Per case:** a fresh `AgentSession(cfg.agent, state from state.json)` is opened, asked one question, and closed with `close(delete_files=False)`. `ensure_data` finds both databases ready and builds nothing. **Config guard:** before the first question, `run_eval` stops if the `chunking` or `embedding` settings in `prepare.json` differ from the run config, so a stale `docs.db` is never used.

**`LAKE_INSTRUCTION`** keeps the tools, the grounding and safety rules and the `[source, §N title]` citations. It changes the following:
- **Data:** "a read-only SQLite database with tables `{schema_summary}`" and "bank policies `{docs_summary}`".
- **SQL:** quote every name and join on `client_id` / `account_id`.
- **Routing:** look up a named client with `run_sql` first. When a Policy ID is named, call `search_documents` with `source="<policy_id>.md"`.
- **Answer:** the first sentence is the direct answer (Yes/No, a value, a decision or a section). Then give at most three sentences with citations. No headings, no SQL and no `Sources:` line.

## 6. Metrics

| Metric | Applies to | Definition |
|---|---|---|
| `tool_sequence_match` | all | Existing |
| `section_recall` | hybrid with cited sections, document | Existing |
| `answer_contains` | table, document (section) | Existing |
| `citation_precision` | all | Existing (grounding) |
| `answer_f1` | all with `reference_answers` | **New.** Maximum token F1 of `final_answer` against the references, with the tokenizer and F1 copied from `score.py` |
| `object_f1` | all | **New.** Set F1 of `predicted_object_ids` against `expected.object_ids`. Predicted ids are the sources of the cited passages (without `.md`; all returned passages if nothing is cited) plus the tables after `FROM`/`JOIN` in successful SQL |
| `coverage` | all | **New.** 1 if `final_answer` is non-empty |

`final_answer(result)` removes a trailing `Sources:` line. It is shared by `answer_f1` and `predictions.jsonl`, so our score and the official one are computed on the same text. `metrics.json` already reports means; also break `answer_f1`, `tool_sequence_match` and `section_recall` down by the `hybrid` / `table` / `document` tag.

**Official cross-check:**

```
LAKEQUEST_REFERENCE_DIR=../benchmark/lakequest_prepared/bank_sample/reference \
LAKEQUEST_RESULT_DIR=runs/<run> LAKEQUEST_SCORE_OUTPUT_DIR=runs/<run>/official \
python ../benchmark/LakeQuest/scoring_program/score.py
```

Its `answer_f1` must equal ours when every case was run (no `limit`).

## 7. Saved Results (`runs/<timestamp>_lakequest_bank_sample/`)

| File | Content |
|---|---|
| `config.yaml` | Resolved config (existing) |
| `results.jsonl` | One row per case (existing), plus the predicted `object_ids` |
| `metrics.json` | Means overall and per type, counts and latency (existing format), plus `prepare.json` under `"data"` |
| `predictions.jsonl` | **New:** `{"qa_id", "answer", "object_ids"}` per case, the input to `score.py` |
| `official/` | Optional: `scores.json` and `per_item_scores.csv` from the cross-check |

## 8. Code Changes

| File | Change |
|---|---|
| `data_agent/config.py` | `AgentConfig.profile`, `EvalConfig.predictions`, three metric names |
| `data_agent/agent.py` | `LAKE_INSTRUCTION`; `build_agent` picks the instruction and description by profile |
| `data_agent/tools.py` | Neutral `run_sql` docstring |
| `evals/dataset.py` | `Expected.reference_answers` and `object_ids`; `EvalDataset.state_file` |
| `evals/metrics.py` | `final_answer`, `predicted_object_ids`, `answer_f1`, `object_f1`, `coverage` |
| `evals/run.py` | Prebuilt-state branch, `close(delete_files=False)`, config guard, per-tag means |
| `evals/report.py` | `predictions.jsonl`; `prepare.json` in `metrics.json` |
| `evals/lakequest.py` | **New:** `prepare` (whitelist copy, policy export, case generation, references, state) |
| `configs/eval/lakequest_bank.yaml` | **New** |

No changes to ingestion, retrieval, sessions, the UI or deployment.

## 9. Implementation Checklist

- [ ] **Profile and metrics:**
  - Add `profile`, `LAKE_INSTRUCTION`, the neutral docstring, the two `Expected` fields and the three metrics.
  - Test the metrics, including a parity check against `score.py` (skipped if it is missing).
  - Check that the shipments smoke evaluation is unchanged.
- [ ] **Builder (`evals/lakequest.py prepare`):**
  - Build `bank.db`, `docs.db`, the 40 cases, `references.jsonl`, `state.json` and `prepare.json`.
  - Test on a small fixture: no build tables in `bank.db`, numbered sections, the template answers equal SQL, and `dataset.yaml` validates.
- [ ] **Runner and report:**
  - Add `state_file`, the guard, the per-tag means and `predictions.jsonl`.
  - Test with a fake session that the bundle survives the run and each prediction row has `qa_id` and `answer`.
- [ ] **Run:**
  - Run `bank_sample` (`limit=5`, then all 40).
  - Run the official cross-check.
  - Record the metrics per type, the cost and the latency in the execution log.

## 10. Risks

- **The hybrid answers are judgments.** For example, "Eligible – approve with controls…". Token F1 is noisy there, so rely more on `tool_sequence_match`, `section_recall`, `object_f1` and the table and document cases.
- **Instruction size:** 212 policy title lines is about 5k tokens per call. If that is too costly, list only a count of the policies and rely on search.
- **Snapshot changes:** `prepare.json` stores the source hashes. Rebuild the bundle when the snapshot changes.
- **Larger set later:** 58 hybrid questions use validation clients and 217 use test clients. A `--split test` option and higher counts can grow the sample without new code.
