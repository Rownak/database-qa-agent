# HybridQA Evaluation Design

**Goal:** evaluate our agent on HybridQA dev questions that need one table and its linked Wikipedia passages. The design reuses the Sprint 4/5 evaluation pipeline: `RunConfig`, `EvalDataset`, `AgentSession`, `METRICS`, `run_eval` and `write_run`. Changes are kept small, local and additive.

**Inputs:**
- [hybridqa_benchmark_report.md](hybridqa_benchmark_report.md), §4.3 and §6
- [design.md](design.md) (Sprint 4)
- [LakeQuest_evaluation_design.md](LakeQuest_evaluation_design.md) (Sprint 5)
- The current `evals/` and `data_agent/` code

**Not covered here:** sprint plan and code.

---

## 1. Approach

```
benchmark/HybridQA/released_data  ──►  evals.hybridqa prepare  ──►  benchmark/hybridqa_prepared/dev_s50/
WikiTables-WithLinks (per table)                                          dataset.yaml + one bundle per table
                                                                          reference/dev_reference.json (subset)
configs/eval/hybridqa_dev50.yaml  ──►  python -m evals <config>  ──►  runs/<ts>_hybridqa_dev50/
                                                                          config.yaml, results.jsonl, metrics.json, predictions.jsonl
                                       evals.hybridqa score <run>   ──►  official_scores.json (evaluate_script.py)
```

- **One-time preparation.** Like `evals/lakequest.py prepare`, a prepare step turns the benchmark into a self-contained bundle. Indexing happens once, at prepare time.
- **The setting is "gold table":** each question gets exactly its own table and that table's linked passages. This is the standard HybridQA setting, since each question names its `table_id`.
- **No new runner, report writer or result format.** The run folder is the one from Sprint 4. A small `score` command produces the official numbers next to ours.

---

## 2. The 50-Question Stratified Sample

**Labels.** The two types use the benchmark's own definitions from `released_data/dev_reference.json` (the lists the official scorer uses):

| Type | Definition (from `trace_answer.py`) | Dev count | Sample |
|---|---|---|---|
| `table` | The answer string matches a table cell (some also occur in a passage) | 1,349 | 25 |
| `passage` | The answer string occurs only in a passage linked from the table | 2,025 | 25 |
| (none) | Not traced to either, so unscored by type | 92 | excluded |

**Selection (reproducible):**
1. Take the qids of each list, sorted, and shuffle them with a fixed seed (`random.Random(seed)`, one stream per type).
2. Walk each shuffled list and keep a question if:
   - its table is not already used by another sampled question (one question per table, for diversity), and
   - its table and passage files downloaded and parsed.

   Stop at 25 per type.
3. Record in `prepare.json`: the seed, the qids chosen and every qid skipped, with the reason.

Because the order per type is fixed by the seed, a larger sample with the same seed contains the smaller one (§7). This keeps runs at different sizes comparable.

---

## 3. Data Preparation

**Command** (run from `db-doc-qa/`):

```
uv run python -m evals.hybridqa prepare --hybridqa ../benchmark/HybridQA \
  --config configs/eval/hybridqa_dev50.yaml --per-type 25 --seed 20260101 \
  --out ../benchmark/hybridqa_prepared/dev_s50
```

**Sources:**
- `released_data/dev.traced.json`: questions, gold answers and `answer-node`
- `released_data/dev_reference.json`: the type lists
- For each sampled table only, `tables_tok/<table_id>.json` and `request_tok/<table_id>.json`, fetched from `raw.githubusercontent.com/wenhuchen/WikiTables-WithLinks/master/`. These are cached in `benchmark/hybridqa_prepared/raw/`, shared by all samples. The 800 MB repo is never cloned.

**Per table, in `tables/<safe_table_id>/`:**

| File | Built from | How |
|---|---|---|
| `<safe_table_id>.csv` | `tables_tok` `header` and `data` | The `cell_text` of each cell. Link lists are dropped. The table name comes from `table_name_for` in `ingest.py`. |
| `docs/<Entity>.md` | `request_tok` (`/wiki/Entity` → passage) | `# Entity Title` (URL-decoded, `_` → space), then the passage text. File names are made safe for Windows. |
| `data.db`, `docs.db` | the two above | Built by the existing ingestion code: `data_agent/ingest.py` for the table, `doc_ingest.build` with the config's `chunking` and `embedding` for the documents. |
| `state.json` | — | The prebuilt session state, as in LakeQuest: `db_path`, `column_map`, `schema_summary`, `docs_db_path`, `docs_summary`, with paths relative to the file. |

**At the bundle root:**
- `dataset.yaml`: an `EvalDataset` with one case per question:
  - `id`: the HybridQA `question_id`
  - `tags: [hybridqa, table | passage]`
  - `state_file`: that case's table bundle
  - `expected.reference_answers: [answer-text]`
  - `expected.answer_contains: [answer-text]`
  - `expected.object_ids`: the entity names of the `passage` nodes in `answer-node` (weak gold; empty for table answers)
- `reference/dev_reference.json`: the official format (`reference`, `table`, `passage`), restricted to the sampled qids, so `evaluate_script.py` scores the subset.
- `prepare.json`: source hashes (the `released_data` files, every downloaded raw file, and the HybridQA commit), seed, counts per type, skipped qids, `chunking`, `embedding`.

---

## 4. Required Adaptations

| # | Where | Change | Why |
|---|---|---|---|
| 1 | `evals/hybridqa.py` (new) | `prepare` (§3) and `score` (§5.2) | Turns the benchmark into our existing formats. It changes no other module's behaviour, as with `evals/lakequest.py`. |
| 2 | `evals/dataset.py` | Optional `state_file` on `EvalCase`; the case's value wins over the dataset's | HybridQA needs one bundle per case, while today `state_file`, `table` and `docs` are per dataset. Existing datasets are unaffected. |
| 3 | `evals/run.py` | Load the case's state when set. The chunking and embedding guard checks the bundle-root `prepare.json` once, before the first question. Sessions on a prebuilt bundle close with `delete_files=False` (unchanged). | Reuses `_load_prebuilt` and its stale-index guard. |
| 4 | `data_agent/config.py`, `data_agent/agent.py` | `profile: hybridqa`, a new `_PROFILES` entry (§4.1) | The `shipments` and `lakequest` instructions are about shipments and banks, and their answer formats do not fit 2-word gold answers. |
| 5 | `evals/metrics.py` (and `MetricName`) | `exact_match` and `squad_f1` (§5.1) | `answer_f1` uses LakeQuest's tokenizer and keeps articles, so it is not the official HybridQA score. |

`report.py`, `runner.py`, `__main__.py` and the result files are unchanged.

### 4.1 `hybridqa` profile (instruction content)

- **Sources:** one table of a Wikipedia page (in the `{schema_summary}` slot) and articles about the entities linked from its cells, one file per entity named after it (in the `{docs_summary}` slot).
- **Routing:**
  1. Find the row or rows with `run_sql`.
  2. Search the linked entity's article, with `source` set to `<Entity>.md`.
  3. Repeat if the question chains several hops.
- **Answer only from these sources.** They are a 2019–2020 Wikipedia snapshot, so outside knowledge must not be used.
- **Answer format:**
  - The first line is the answer alone: an entity, number, date or short phrase, with no sentence around it.
  - Then at most 2 lines of justification, citing `[Entity.md]`.
  - No "Sources:" line.
- **Settings:** `intent_enabled: false`, since the classifier prompt is about shipments. The temperature stays at 0.0.

---

## 5. Metrics and Reporting

### 5.1 Metrics (all in `METRICS`, chosen in the YAML)

| Metric | Scored text | Gold | Notes |
|---|---|---|---|
| `exact_match` | **first line** of `final_answer` | `reference_answers` | The official `normalize_answer` (lower case, no punctuation or articles, collapsed spaces). **Primary.** |
| `squad_f1` | first line | `reference_answers` | The official token F1, with the same normalisation. **Primary.** |
| `answer_contains` | whole answer | `answer_contains` | Existing metric. Lenient: tells a correct but wordy answer from a wrong one. |
| `coverage` | whole answer | — | Existing: was any answer given? |
| `object_f1` | cited or retrieved sources | `expected.object_ids` | Existing. A **diagnostic only** for `passage` cases, because `answer-node` is approximate (1,484 dev questions have several nodes). `None` for `table` cases. |

- **Not used:** `tool_sequence_match`, `section_recall` and `citation_precision`. There is no gold tool sequence, and the passages have no numbered sections, so these return `None`.
- **First line:** scoring the first line keeps the justification lines from lowering EM and F1. The `predictions.jsonl` answer is the same first line, so our scores and the official scores use identical text.

### 5.2 Reporting

- **`metrics.json`:** unchanged structure.
  - Overall: `n`, `errors`, latency p50/p95, and the mean per metric.
  - `by_tag`, which already exists, gives each metric for **`table` and `passage` separately**, plus `hybridqa`, which equals the overall figure.
  - `data` holds `prepare.json` (seed, counts, sources).
- **`results.jsonl`:** one row per question, with tags, answer, tool calls and per-metric scores. It is used for error analysis.
- **`evals.hybridqa score <run_dir> --reference <bundle>/reference/dev_reference.json`:**
  1. Converts `predictions.jsonl` to the official `[{question_id, pred}]`.
  2. Runs the logic of `evaluate_script.py`.
  3. Writes `official_scores.json` (table, passage and total EM/F1) into the run folder.
  - Its EM and F1 must equal our `exact_match` and `squad_f1` per type (×100). Any difference is a bug in our metric.
- **Execution log (per run):** the table below, plus the error count, latency and approximate model calls. Also note the lowest-scoring row of each type, classified as one of:
  - wrong row (SQL)
  - wrong or missing entity article
  - right article, wrong span
  - correct but formatted differently

| Type | n | EM | F1 | answer_contains | object_f1 |
|---|---|---|---|---|---|
| table | 25 | | | | — |
| passage | 25 | | | | |
| all | 50 | | | | |

---

## 6. Run Configuration

`configs/eval/hybridqa_dev50.yaml`:

```yaml
name: hybridqa_dev50
agent:
  profile: hybridqa
  intent_enabled: false
  model:
    agent: gemini-3.5-flash-lite
    temperature: 0.0
  retrieval:
    mode: hybrid
dataset:
  path: ../benchmark/hybridqa_prepared/dev_s50/dataset.yaml
eval:
  mode: agent
  metrics: [exact_match, squad_f1, answer_contains, coverage, object_f1]
  predictions: true
```

```
uv run python -m evals configs/eval/hybridqa_dev50.yaml
uv run python -m evals.hybridqa score runs/<ts>_hybridqa_dev50 \
  --reference ../benchmark/hybridqa_prepared/dev_s50/reference/dev_reference.json
```

- **Overrides that need no new bundle:**
  - `dataset.limit=5` for a smoke run
  - `dataset.case_ids=[...]` to re-run failures
  - `agent.model.agent=...`
  - `agent.retrieval.mode=bm25` for an ablation
- **Overrides that need the bundle rebuilt:** changing `agent.chunking` or `agent.embedding` requires running `prepare` again with the same config. The guard stops the run before the first question otherwise.

---

## 7. Scaling Up

| Step | `prepare` | Run config | Note |
|---|---|---|---|
| 50 (this design) | `--per-type 25` | `hybridqa_dev50.yaml` | Baseline and instruction tuning. |
| 200 | `--per-type 100 --out …/dev_s200` | copy, with `name` and `dataset.path` changed | Same seed, so it contains the 50. Gives tighter per-type means. |
| Full dev (3,374 typed) | `--per-type all --out …/dev_full` (the one-per-table rule is dropped) | `dataset.path` only | Needs all 3,053 tables: about 350 MB of raw files and about 135k passages to embed [A]. Comparable to the README leaderboard only at this size. |
| Variance | — | `eval.repeats: 3` | Shows how stable EM and F1 are at temperature 0. |
| Tuning without overfitting dev | `--split train` sample | separate config | Tune the instruction on train questions and report on dev. |

At every step the code, runner and result format stay the same. Only the `prepare` arguments and the YAML change. Test-set scoring (hidden answers, CodaLab login) is out of scope. `predictions.jsonl` can be converted to `test_answers.json` by the same `score` conversion if it is ever needed.

---

## 8. Assumptions and Limitations

**Assumptions [A]**
- Entity names in table cells match the passage titles well enough for search with `source` set to `<Entity>.md`. Cell links are dropped from the CSV, so a cell holding several links (for example "Dallas Cowboys ( 1990 - 2002 ) Arizona Cardinals …") relies on search. A possible later ablation is to add a `<column>_links` column.
- Each `request_tok` file has about 44–90 passages, which the existing session limits handle: no document-count cap, 1 MB per document and 500 KB per table.
- The `hybridqa` instruction gets a first-line short answer from the model consistently. This should be checked on the first 5-case run.
- The full-dev download and embedding volumes in §7 are extrapolated from one sample table.

**Limitations**
- **Strict, single-reference scoring:** "4" vs "four teams" fails EM. So does tokenisation spacing in gold answers ("1990 - 2002" normalises differently from "1990-2002"). `answer_contains` and a manual look at the low-scoring rows offset this. An LLM judge stays out of scope (as in Sprint 4).
- **Type labels come from string matching:** some `table` questions still need a passage hop for their conditions, so the two types measure where the answer lies, not whether the reasoning was single-hop.
- **Weak provenance:** `object_f1` gold is approximate, so it is reported but never used as a pass or fail threshold.
- **A 50-question sample is noisy:** about ±14 points (95%) per type at 25 questions [A, binomial estimate]. Compare configurations on the same sample, and use 200 or more for decisions.
- **Coverage gaps:** one table per question, so this sample does not test multi-table SQL, refusals or numbered-section citations. LakeQuest `bank_sample` and `shipments_v1` still cover those.
- **Licensing and data:** WikiTables-WithLinks has no license, so the bundles stay in the git-ignored `benchmark/` folder for internal use only. Results are internal and not leaderboard submissions.
