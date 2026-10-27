# TableRAG Benchmark Report

**Scope:** what the TableRAG repository (`benchmark/TableRAG`) provides for evaluating our database-and-document agent, and the smallest adaptation needed to use it.

**Sources:**
- Local clone at commit `f8d798f` (23 Jul 2025, origin `github.com/yxh-y/TableRAG`)
- The paper, [arXiv 2506.10380 v2](https://arxiv.org/html/2506.10380v2) (EMNLP 2025)
- The authors' [Google Drive folder](https://drive.google.com/drive/folders/1Pea6kiUZv0UP8k7Ohv19KorBdBaUrouE)
- [HybridQA](https://github.com/wenhuchen/HybridQA), [WikiTables-WithLinks](https://github.com/wenhuchen/WikiTables-WithLinks) and the TableRAG GitHub issues

Each finding is marked **[V]** (verified from a file, a command run or an official page) or **[A]** (assumption or inference). I downloaded only `my_dev.json` (92 KB) to the session scratchpad, not to the repo. Nothing in the repo or its code was changed.

---

## 1. Summary

- **The data in the repo is a HybridQA dev subset, not HeteQA.** [V/A]
  - The repo and Drive contain 305 questions with HybridQA-style fields, 3,053 Wikipedia tables and a 32 MB passage archive.
  - HeteQA, the paper's own benchmark (304 questions), does not appear to have been released.
- **Every question is hybrid by construction:** it needs one small table (at most 20 rows) and its linked Wikipedia passages. This fits our agent's per-session model (a table plus documents) well.
- **The repo's evaluation pipeline is not usable as shipped.**
  - It needs MySQL, a Flask NL2SQL service, a GPU for BGE-M3 and its reranker, and OpenAI-compatible LLM endpoints.
  - `main.py` has several bugs that crash at runtime (§3.4).
  - The scorer is a single LLM-as-judge prompt (0/1).
- **We should reuse the data, not the code.** The minimum adaptation is:
  - a `prepare` adapter, built like `evals/lakequest.py`
  - a per-case table and documents in `EvalCase`
  - an `exact_match` metric, with HybridQA normalisation, next to the existing `answer_f1`
- **The license is a blocker for anything beyond internal research use.** [V]
  - The repo has no license file (issue #2 is open and unanswered).
  - HybridQA itself is MIT-licensed. The license of the Wikipedia-derived tables and passages is not stated in either repo.

---

## 2. Datasets

### 2.1 What exists

| Asset | Location | Size | Content | Status |
|---|---|---|---|---|
| `dev_excel.zip` | in repo: `offline_data_ingestion_and_query_interface/dataset/hybridqa/` (also on Drive) | 16.3 MB (17.9 MB unzipped) | 3,053 `.xlsx` tables, one `Sheet1` each | present [V] |
| `my_dev.json` | Drive only | 92 KB | 305 questions | not in repo; downloaded to scratchpad for inspection [V] |
| `dev_doc.zip` | Drive only | 32.1 MB | linked Wikipedia passages (see below) | not downloaded [V] |
| HeteQA (304 questions, SQL and answers) | none found | — | — | not released [A] |

**`dev_doc.zip` contents:** inferred from the code, not inspected. [A] It holds one JSON file per table, mapping each wiki link to its passage text (`{"/wiki/X": "passage …"}`). Evidence: `MixedDocRetriever.load_hybrid_dataset` in `online_inference/tools/retriever.py` reads every file in `doc_dir` as a key→text dict, and `relate_to_table` maps `*.json` to `*.xlsx`. This matches the WikiTables-WithLinks `request_tok/` folder.

**HeteQA release:** issue #3 (14 Jun 2025, unanswered) says HeteQA "is shared through Google Drive". The Drive folder contains only the three files above, and `my_dev.json` has no SQL or pandas field, which the paper says every HeteQA case has. [V for the folder contents; A that `my_dev.json` is not HeteQA]

### 2.2 Formats [V]

**Questions:** `my_dev.json` is a JSON list of 305 objects.

```json
{"question_id": "00153f694413a536",
 "question": "What is the middle name of the player with the second most National Football League career rushing yards ?",
 "table_id": "List_of_National_Football_League_rushing_yards_leaders_0",
 "answer-text": "Jerry",
 "question_postag": "WP VBZ DT JJ NN ..."}
```

- **Coverage:** 269 distinct tables, 65 answers that are purely numeric, about 2.0 words per answer on average.
- **Format matches HybridQA:** the 16-hex `question_id`, `answer-text` and `question_postag` are HybridQA's own fields. The tokenised spacing (`Card 's`) suggests the questions are HybridQA's human-written ones. [A]

**Tables:** Excel files named `<Wikipedia_page>_<n>.xlsx`, with a header row.
- 5–20 data rows (median 18) and 3–8 columns (median 4).
- Hyperlinks from the original HybridQA cells are lost; only the cell text remains.
- Example: `Alain_Mimoun_0.xlsx`, with columns Year, Competition, Venue, Position, Event.

**Ground truth:**
- Only `answer-text`, one short string per question.
- There is no gold SQL, gold passage, answer type (table vs passage) or question category.
- HybridQA's own `released_data/dev.json` and `dev_reference.json` have the table/passage split and `answer-node`. Joining them by `question_id` should work but is not verified. [A]

### 2.3 Data issues found [V]

- **5 of the 269 question tables have mismatched file names.** The JSON uses `__` or `_` where the zip uses `:` or `,`. For example:
  - JSON: `List_of_microcars_by_country_of_origin__J_0`
  - zip: `List_of_microcars_by_country_of_origin:_J_0.xlsx`
- **`:` is not allowed in Windows file names.** Unzipping `dev_excel.zip` on our Windows machine will fail or rename these files. The adapter must read from the zip directly.
- **The code expects CSV files the data does not include.** `main.py` (`construct_initial_prompt`) looks for `<table>.csv` in `excel_dir`, but only `.xlsx` files are provided. The prompt then contains "Can NOT find table content!" (open issue #6, Jan 2026, no maintainer reply).

### 2.4 Access

- **Drive folder:** public, no sign-in needed. [V]
- **HybridQA:** GitHub plus an S3 bucket, no registration. Test answers are hidden behind a CodaLab competition, but dev answers are public. [V]

---

## 3. Evaluation Pipeline in the Repository

### 3.1 Components [V]

| Step | Code | What it does |
|---|---|---|
| Offline ingestion | `offline_data_ingestion_and_query_interface/src/data_persistent.py` | Reads each `.xlsx` with pandas, infers types, cleans names (`transfer_name`), inserts the table into **MySQL 8.0.24** and writes a schema JSON (column, type, 3 sample values) |
| SQL service | `src/interface.py` (Flask, port 5000), `service.py`, `handle_requests.py` | `POST /get_tablerag_response {table_name_list, query}` → NL2SQL with an LLM (DeepSeek-V3 by default) → runs the SQL on MySQL → returns `sql_str`, `sql_execution_result`, `nl2sql_prompt` |
| Online inference | `online_inference/main.py` | Retrieves the top table with BGE-M3 and `bge-reranker-v2-m3` over table and passage chunks (1,000 characters, 200 overlap, FAISS). Up to 5 rounds of `solve_subquery`. Each round combines 3 passages with the SQL result in `COMBINE_PROMPT` and ends with `<Answer>` |
| Scoring | `online_inference/evaluation/hybrid_eval.py` | LLM judge (`EVALUATION_PRONPT`): rates `tablerag_answer` against `answer-text` as `[[0]]` or `[[1]]`; prints the mean and writes an `.xlsx` report |

### 3.2 Commands (from the README) [V]

```
cd offline_data_ingestion_and_query_interface/src && python data_persistent.py   # MySQL ingestion
python interface.py                                                              # SQL service
cd online_inference && python3 main.py --backbone v3 --data_file_path ./data/my_dev.json \
   --doc_dir <dev_doc> --excel_dir <dev_excel> --bge_dir <dir with bge-m3, bge-reranker-v2-m3> \
   --save_file_path out.jsonl --max_iter 5
python evaluation/hybrid_eval.py --result_file_path out.jsonl
```

The README omits `--doc_dir`, `--excel_dir` and `--bge_dir`, but `main.py` requires them.

### 3.3 Metrics

- **Primary:** the paper reports LLM-judge binary accuracy (Qwen-2.5-72B judge) as its main metric, and exact match only in Appendix D.1. [V, paper]
- **Repo judge:** the repo's judge uses `v3_config` (DeepSeek), even though its docstring says "qwen 72b". [V]
- **No exact-match code:** none is shipped. HybridQA's official `evaluate_script.py` computes EM and F1 (lower-case, no punctuation, no articles), overall and split by table/passage. [V]
- **Paper's TableRAG results (LLM-judge):**
  - HybridQA: 47.9–48.5
  - WikiTQ: 78.0–84.6
  - HeteQA: 38.8–44.9

  Backbones: Claude-3.5-Sonnet, DeepSeek-V3, Qwen-2.5-72B. The paper filters HybridQA to tables with more than 100 cells and does not say how many samples it used. [V, paper]

### 3.4 Defects that block running it as shipped [V, read from code; not executed]

- **`main.py` cannot run a single question:**
  - `self.repo_id` is used but its definition is commented out.
  - `get_excel_rag_response_plain` is called with 3 arguments but takes 2.
  - `get_excel_rag_response['nl2sql_prompt']` indexes a function.
  - The fallback then unpacks one string into three variables.
  - `logger.init_logger(...)` is called on a `logging.Logger`.
- **Other bugs:**
  - `--rerun` indexes a `set`.
  - The output key is misspelt `tablerage_messages`.
  - `--backbone` defaults to `gpt-4o`, which is not in `config_mapping` (only `v3` is).
  - `handle_requests.py` posts chat `messages` to a `/v1/completions` URL. The configured endpoint must be a chat-completions endpoint. [A]
- **GPU and dependencies:**
  - `Embedder` hard-codes `cuda:1`, and the retriever creates `faiss.StandardGpuResources()`, so it needs CUDA and faiss-gpu.
  - `requirements.txt` lists none of `faiss`, `langchain_text_splitters`, `nltk`, `more_itertools` or `openpyxl`, although the code imports them.

---

## 4. Integration Feasibility with Our Agent

### 4.1 Fit

| Question kind | Our tool | Fit with this data |
|---|---|---|
| SQL over the table | `run_sql` (read-only SQLite; `.xlsx` is supported directly by `ingest.py`) | **Good.** Tables are small and clean. Questions are mostly lookups and superlatives ("second most …"), with few aggregations. [V for formats; A for the question mix] |
| Passages | `search_documents` (BM25 + dense + RRF over `.md`/`.txt`/`.pdf`) | **Good** once the passages are written as `.md` with the entity title in the text, because questions name the entity only indirectly. [A] |
| Hybrid (table → linked entity → passage fact) | both | **This is the core of HybridQA.** It tests the multi-hop behaviour our agent was designed for. [A] |
| Pure document questions, refusals, multi-table joins | — | **Not covered.** Each question has exactly one table. |

**Setting:** the TableRAG code effectively gives the gold table. It appends "The given table is in {table_id}" to the query before retrieval (`main.py`, `_run`). [V] Our equivalent is to load the gold table and its passages into each session, the "oracle table" setting. Open-corpus table retrieval over 3,053 tables is outside our agent's design and the Sprint 6 limit of 3 files per session.

### 4.2 Gaps

1. **Per-case files:** `EvalDataset` has one `table`/`docs`/`state_file` per dataset (`evals/dataset.py`), but here each case has its own table and passages. [V]
2. **Agent instruction:** the `profile` is `shipments` or `lakequest` (`data_agent/config.py`). Both instructions name domain-specific concepts (shipments, policy ids). We need a neutral one. [V; how much the wording matters is A]
3. **Metric:**
   - `answer_f1` exists but uses LakeQuest tokenisation, without HybridQA's article removal. [V]
   - There is no `exact_match`.
   - `tool_sequence_match`, `section_recall` and `citation_precision` have no gold data here, so they return `None`.
4. **Answer style:**
   - Gold answers are about 2 words. Our agent answers in full sentences with citations.
   - EM will be near zero unless the answer is short. `final_answer` and `strip_sources` help.
   - A short-answer line in the instruction is needed. [A]
5. **Data hygiene:** the name mismatch and Windows-illegal names (§2.3), and the passages are not yet downloaded.

### 4.3 Minimum adaptation (reusing the Sprint 4/5 pipeline)

1. **`evals/tablerag.py prepare`** (modelled on `evals/lakequest.py`). Inputs: `my_dev.json`, `dev_excel.zip`, `dev_doc.zip`. Output: `benchmark/tablerag_prepared/hybridqa_dev/`.
   - Read tables straight from the zip. Match a `table_id` by normalising non-alphanumeric characters on both sides, and write each table as `tables/<case>.xlsx` (or `.csv`) under a safe name.
   - Convert each table's passage JSON to one `docs/<case>/<entity>.md`, with the entity title as heading.
   - Write `dataset.yaml`:
     - `reference_answers: [answer-text]`
     - `answer_contains` for numeric answers
     - `tags: [hybridqa]`, plus `table`/`passage` when joined with HybridQA `dev.json`
   - Add a `--limit` / `--seed` sample option. Start with about 50 cases, as the paper's runs are slow and expensive (about 18–46 s per question, §5).
2. **`EvalCase`:** add optional `table` and `docs`, overriding the dataset level. `run_eval` passes them to `files_state`. This is a small change in `evals/dataset.py` and `evals/run.py`, and the existing datasets stay valid.
3. **Profile:** add `profile: generic`, a domain-neutral instruction ending with "give the short answer first". Alternatively, check whether `LAKE_INSTRUCTION` is already neutral enough.
4. **Metrics:**
   - Add `exact_match` with HybridQA normalisation, beside the existing `answer_f1`.
   - Optionally add an LLM-judge metric later, for comparison with the paper. Sprint 4 put LLM-as-judge out of scope.
5. **Config:** `configs/eval/tablerag_hybridqa.yaml` (`profile: generic`, `intent_enabled: false`, metrics `exact_match`, `answer_f1`, `answer_contains`, `dataset.limit`).

No MySQL, Flask, FAISS or GPU is needed: our agent replaces the whole TableRAG runtime.

---

## 5. Limitations

- **Licensing:**
  - TableRAG has no license (issue #2 is open), so by default its code and Drive data have no granted reuse rights. [V]
  - HybridQA's code and annotations are MIT. [V]
  - Wikipedia content is normally CC BY-SA, but neither repo says so. [A]
  - Safe use is internal benchmarking only, without redistributing data in our repo. `benchmark/` is already in `.gitignore`. [V]
- **Missing assets:**
  - HeteQA, the paper's headline benchmark, was not found. [A]
  - `my_dev.json` and `dev_doc.zip` are Drive-only.
  - There are no CSVs (issue #6), no gold SQL, no gold passages and no question categories.
  - WikiTQ data is not in the repo.
- **External services in the original pipeline:**
  - MySQL 8.0.24 and a Flask service
  - OpenAI-compatible LLM endpoints (DeepSeek-V3 for NL2SQL, answering and judging)
  - Hugging Face downloads of `bge-m3` and `bge-reranker-v2-m3`
- **Compute:**
  - The original pipeline needs a CUDA GPU (it hard-codes `cuda:1`; faiss-gpu).
  - With our agent there is no GPU, but there is LLM cost: several model calls per question.
  - For reference, the paper measured about 18 s (WikiTQ) to 46 s (HeteQA) per question with TableRAG on Qwen-2.5-72B. Hardware is not reported. [V, paper]
- **Evaluation gaps:**
  - Answers are short entity or number strings. EM and F1 under-credit correct but wordy answers, while the paper's LLM judge is lenient. Our numbers will **not** be comparable with the paper's table.
  - Without the HybridQA `dev.json` join there is no table-vs-passage breakdown.
  - With one table per question, the set does not test multi-table SQL, policy-style documents with numbered sections, citations or refusals. LakeQuest `bank_sample` and `shipments_v1` remain our coverage for those.
- **Data drift:** the questions come from 2020 Wikipedia snapshots. Some passages may be missing from `dev_doc.zip`. This is unverified until it is downloaded. [A]

---

## 6. Recommendation

- **Use the HybridQA subset as a secondary, internal benchmark** of table→passage multi-hop reasoning, in the oracle-table setting, with EM and F1. It complements LakeQuest `bank_sample`.
- **Next steps:**
  1. Download `dev_doc.zip` (32 MB) to `benchmark/TableRAG/data/`, and check it contains one passage JSON per table.
  2. Optionally fetch HybridQA `released_data/dev.json` to tag table vs passage questions.
  3. Implement §4.3 as a short sprint.
- **Do not port the TableRAG code.** If an external baseline is needed, cite the paper's numbers and note the different metric.
