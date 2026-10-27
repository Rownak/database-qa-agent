# HybridQA Benchmark Report

**Scope:** what the HybridQA repository (`benchmark/HybridQA`) provides for evaluating our database-and-document agent, and the smallest adaptation needed to use it.

**Sources:**
- Local clone at commit `db22fda` (2 Jun 2023, origin `github.com/wenhuchen/HybridQA`)
- Its linked resources: [WikiTables-WithLinks](https://github.com/wenhuchen/WikiTables-WithLinks), the [S3 downloads](https://hybridqa.s3-us-west-2.amazonaws.com/), the [CodaLab competition](https://codalab.lisn.upsaclay.fr/competitions/7979) and the [Hugging Face copy](https://huggingface.co/datasets/wenhu/hybrid_qa)

Each finding is marked **[V]** (verified from a file, a command run or an official page) or **[A]** (assumption or inference). To check the formats, I downloaded one table and its passages (115 KB) from WikiTables-WithLinks to the session scratchpad. Nothing in the repo or its code was changed.

---

## 1. Summary

- **The question and answer files are in the repo (80 MB):** train, dev and test splits. Dev has gold answers and a table-vs-passage split for scoring. [V]
- **The tables and linked passages are not in the repo.** They are in WikiTables-WithLinks (about 800 MB as a git repo), but single tables can be fetched one at a time over raw HTTP. [V]
- **Every question is hybrid by design.** It needs one Wikipedia table of at most 20 rows, plus a hop into the passages linked from its cells (about 44 per table on average). This fits our per-session table-plus-documents model directly. [V for the data; A for the fit]
- **The official scorer is a small script with no dependencies:** `evaluate_script.py` (EM and F1), which we can reuse almost as-is. The original baseline cannot be reproduced: its preprocessed data and models on S3 now return 404, and it needs PyTorch 1.4 and a GPU. [V]
- **Minimum adaptation:**
  - a `prepare` adapter that builds one bundle per case (table plus passages as `.md`)
  - a per-case table and documents in `EvalCase`
  - a domain-neutral, short-answer agent profile
  - an `exact_match` metric using HybridQA's normalisation
- **Relation to TableRAG:** TableRAG's `my_dev.json` is a 305-question subset of this dev split, with all 305 ids matched. HybridQA is therefore the better source: more questions, gold labels and passage provenance. [V]

---

## 2. Datasets

### 2.1 What is in the repository [V]

| File | Size | Rows | Fields | Gold answer |
|---|---|---|---|---|
| `released_data/train.json` | 22 MB | 62,682 (12,374 tables) | `question_id`, `question`, `table_id`, `answer-text`, `question_postag` | yes |
| `released_data/dev.json` | 1.2 MB | 3,466 (3,053 tables) | same | yes |
| `released_data/test.json` | 1.1 MB | 3,463 (3,061 tables) | no `answer-text` | **hidden** (CodaLab only) |
| `released_data/{train,dev}.traced.json` | 53 / 2.9 MB | as above | adds `answer-node` | yes, plus weak provenance |
| `released_data/dev_reference.json` | 228 KB | 3,466 | `reference` {qid: answer}, `table` [qids], `passage` [qids] | yes |
| `test_answers.json` (repo root) | 2.9 MB | 3,463 | `pred`, `nodes`, `target` | **no.** It is an example submission of model predictions, not gold |

Other files:
- `evaluate_script.py`: the scorer
- `trace_answer.py`: builds `answer-node`
- `preprocessing.py`, `train_stage12.py`, `train_stage3.py`: the 2020 BERT baseline
- `turker_interface/`: the annotation UI
- `LICENSE`: MIT, © 2020 Wenhu Chen

**Example (`dev.traced.json`):**

```json
{"question_id": "00153f694413a536",
 "question": "What is the middle name of the player with the second most National Football League career rushing yards ?",
 "table_id": "List_of_National_Football_League_rushing_yards_leaders_0",
 "answer-text": "Jerry",
 "answer-node": [["Walter Payton", [1, 1], "/wiki/Walter_Payton", "passage"], ...]}
```

**`answer-node` structure:** each node is `[text, [row, col], wiki_link | null, "table" | "passage"]`. The README warns it is "approximated, not guaranteed to be correct". [V]

### 2.2 Ground truth in dev (3,466 questions) [V, computed]

- **Answer location in `dev_reference.json`:**
  - `table`: 1,349 questions, where the answer string occurs in a table cell (778 table-only and 571 also in a passage)
  - `passage`: 2,025 questions, where it occurs only in linked passages
  - 92 questions are in neither list. They count in `total` but in neither subset score.
- **Provenance:** 3,374 questions have at least one `answer-node`, and 1,484 have more than one (ambiguous trace).
- **Answer form:** short, averaging 2.1 words; 768 are purely numeric. There is one reference answer per question.
- **Not provided:** gold SQL, a question category (aggregation, comparison, …), or a gold reasoning path.

### 2.3 Tables and passages (not in the repo) [V]

**Source:** `git clone https://github.com/wenhuchen/WikiTables-WithLinks` (README §Preprocess). GitHub reports about 801 MB for the repo, with no license set.
- `tables_tok/<table_id>.json`:
  - `url`, `title`, `section_title`, `section_text`, `intro`, `uid`
  - `header`: `[[name, []], ...]`
  - `data`: rows of `[cell_text, [wiki_links]]`
  - Example: 20 rows by 3+ columns.
- `request_tok/<table_id>.json`: `{"/wiki/Entity": "passage text"}`. In the sample there were 89 passages, 34–392 words each (median 214). They look like the lead section of each Wikipedia article. [A that every file follows this pattern]
- **Per-table download:** files are reachable one at a time through `raw.githubusercontent.com/wenhuchen/WikiTables-WithLinks/master/{tables_tok,request_tok}/<table_id>.json` (about 115 KB per table in the sample). A 100-question sample needs only about 12 MB. [V for one table; A for the total]

**Alternatives:**
- [`wenhu/hybrid_qa`](https://huggingface.co/datasets/wenhu/hybrid_qa) on Hugging Face (194 MB, card license CC BY 4.0). It packs each table and a per-link `summary` into each example, but it uses a loading script and its viewer is disabled. Whether its summaries equal the full `request_tok` passages is not verified. [V for the card; A for passage equality]
- TableRAG's Drive `dev_excel.zip` (3,053 dev tables as `.xlsx`, already local in `benchmark/TableRAG`). It drops the cell links and the passages. [V, see `tablerag_benchmark_report.md`]

### 2.4 Access [V]

- **Dev and train:** public on GitHub, no registration.
- **Test:** answers are hidden. Scoring needs a CodaLab account and a zipped `test_answers.json` (`[{question_id, pred}]`). The competition's single phase runs until 2099.
- **S3 links:** `preprocessed_data.zip` and `models.zip` return **HTTP 404** (checked 2026-10-27).

---

## 3. Evaluation in the Repository [V]

- **Scorer:** `evaluate_script.py` (standard library only).

  ```
  python evaluate_script.py predictions.json released_data/dev_reference.json
  ```

  - **Input:** `predictions.json` is `[{"question_id", "pred"}]`.
  - **Output:** `table exact/f1`, `passage exact/f1`, `total exact/f1` and `total`.
  - **Normalisation:** SQuAD-style (lower case, no punctuation, no articles `a/an/the`, collapsed whitespace). EM is string equality, and F1 is token overlap against one gold answer.
  - **Missing ids:** the script does not handle them. Every `qid` in the reference must have a prediction, or the split means raise `KeyError`. A subset run needs a subset reference file.
- **Baseline (HYBRIDER):**
  - It has a three-stage BERT pipeline and requires `transformers==2.6.0`, `pytorch==1.4.0`, a CUDA GPU and the now-missing S3 files.
  - It is not relevant for our use beyond its published scores.
  - README leaderboard dev scores (EM/F1): HYBRIDER 43.5/50.6, up to S³HQA 68.4/75.3.

---

## 4. Integration Feasibility with Our Agent

### 4.1 Fit

| Question kind | Our tool | Fit |
|---|---|---|
| Table lookup, superlative or count over one table | `run_sql` (read-only SQLite; `.csv`/`.xlsx` ingest in `data_agent/ingest.py`) | **Good.** Tables have at most 20 rows and few columns; most hops are lookups and superlatives. [V for the size; A for the mix] |
| Passage facts about a linked entity | `search_documents` (BM25 + dense + RRF over `.md`/`.txt`/`.pdf`) | **Good** if each passage becomes one `.md` named after the entity, with the title as heading. Questions describe entities indirectly ("the player with the second most …"). [A] |
| Hybrid (table row → entity → passage) | both | **The core of HybridQA:** 2,025 dev questions have passage-only answers. [V] |
| Pure documents, refusals, multi-table SQL, policy-style documents with numbered sections | — | **Not covered** (one table per question). |

**Setting:** we use the gold table, which is the standard HybridQA setting (each question names its `table_id`). [V] The 3 files per session (Sprint 6) are enough, since one table is needed.

### 4.2 Gaps

1. **Per-case files:** `EvalDataset` (`evals/dataset.py`) has one `table`/`docs`/`state_file` per dataset; HybridQA needs its own table and passages per case. [V]
2. **Agent instruction:** the `profile` (`data_agent/config.py`) is only `shipments` or `lakequest`. We need a neutral instruction that asks for the short answer first. [V for the gap; A for the effect]
3. **Metrics:**
   - There is no `exact_match`.
   - The existing `answer_f1` uses LakeQuest tokenisation and keeps articles, so it would differ slightly from the official F1. [V]
4. **Answer length:** gold answers average 2.1 words, while our agent answers in sentences with sources. EM needs `final_answer`/`strip_sources` plus the short-answer instruction. [A]
5. **Cell links are lost in SQL:** a `.csv` keeps only `cell_text`. The table-to-passage hop must go through the entity name, which matches the passage file name and heading in most cases. [A]
6. **Indexing cost:** each case embeds about 44–90 passages when its session starts. A prebuilt `docs.db` per table (reusing the LakeQuest `state_file` approach) avoids repeating this. [A]

### 4.3 Minimum adaptation (reusing the Sprint 4/5 pipeline)

1. **`evals/hybridqa.py prepare`** (modelled on `evals/lakequest.py`):
   - **Inputs:** `released_data/dev.traced.json`, `dev_reference.json`, `--limit`, `--seed`, and stratification by `table`/`passage`.
   - **Download:** fetch only the needed `tables_tok`/`request_tok` files into `benchmark/hybridqa_prepared/<name>/raw/`, caching them and never cloning the 800 MB repo.
   - **Per case:**
     - `tables/<table_id>.csv`: header plus `cell_text`. Names must be made safe for Windows and SQL.
     - `docs/<table_id>/<Entity>.md`: `# Entity title` plus the passage.
   - **`dataset.yaml`:**
     - `reference_answers: [answer-text]`
     - `answer_contains` for numeric answers
     - `tags: [hybridqa, table|passage]`
     - `object_ids` from `answer-node` wiki links (weak gold)
   - **`reference/dev_reference.json` and `predictions.json`:** written in the official format for the chosen subset, so `evaluate_script.py` can cross-check our scores.
2. **`EvalCase`:** add optional `table`, `docs` (or `state_file`) that override the dataset level. `run_eval` passes them to `files_state`. This is a small change in `evals/dataset.py` and `evals/run.py`, and existing datasets keep working.
3. **Profile:** add `profile: generic` (short answer first, then sources).
4. **Metrics:**
   - Add `exact_match` and `answer_f1_squad` (the official `normalize_answer`).
   - Keep `object_f1`, computed from `answer-node`, as a weak provenance check.
   - Report the `table` and `passage` subsets through the existing per-tag means.
5. **Config:** `configs/eval/hybridqa_dev.yaml` (`profile: generic`, `intent_enabled: false`, `dataset.limit: 50`).

No GPU, BERT model or external service is needed beyond our existing LLM and embedding APIs.

---

## 5. Limitations

- **Licensing:**
  - The HybridQA code and annotations are MIT (`LICENSE`). [V]
  - WikiTables-WithLinks has no license on GitHub. [V]
  - The Hugging Face card says CC BY 4.0. [V]
  - The underlying Wikipedia text is normally CC BY-SA. [A]
  - Use is fine for internal evaluation. Do not commit the data; `benchmark/` is already in `.gitignore`. [V]
- **Missing or unavailable assets:**
  - Test gold answers are hidden.
  - The S3 preprocessed data and models return 404.
  - The tables and passages need a separate download.
  - There is no gold SQL or question-type label.
  - `answer-node` provenance is approximate (1,484 dev questions have several candidate nodes, and 92 have none).
- **External services:** GitHub raw downloads for the tables and passages, and CodaLab (login) for test scoring only. Our own run uses our existing LLM and embedding APIs.
- **Compute and cost:** none locally. Each question takes several LLM calls plus embedding of 44–90 passages, so start with 50–100 dev questions rather than all 3,466. [A]
- **Evaluation gaps:**
  - EM/F1 are strict: "4" vs "four teams" or a correct but longer answer score low. There is a single reference per question and no LLM judge.
  - Scores are comparable with the README leaderboard only on the full dev or test split, and only if formatting matches.
  - Questions are crowd-written, with some awkward phrasing ("the zoo … in the city of KIEM-TV").
  - The data comes from a 2019–2020 Wikipedia snapshot, so the passages, not current Wikipedia, must be the only source the agent sees.

---

## 6. Recommendation

- **Use HybridQA dev as our hybrid table→passage benchmark,** preferring it to the TableRAG copy. It has 3,466 gold-labelled questions, an official dependency-free scorer, a table/passage split and weak passage provenance. LakeQuest `bank_sample` covers multi-table SQL and policy documents.
- **First step:**
  1. Implement §4.3 with a stratified 50-question sample: 25 `table` and 25 `passage`.
  2. Check our `exact_match` against `evaluate_script.py` on a subset reference.
  3. Only then scale up.
