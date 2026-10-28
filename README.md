# Enterprise Data & Document QA Agent

A working demonstration of an enterprise assistant built with **Google Agent Development Kit (ADK)**. It answers natural-language questions across structured records and internal documents, combining SQL results with cited policy guidance when a question needs both.

## What it demonstrates

Enterprise answers often depend on information split between operational systems and documentation. This demo brings the two together in one conversational workflow:

- **Database questions:** Query shipment records, or up to three uploaded tables including joins across them, through a restricted, read-only SQL tool.
- **Document questions:** Retrieve relevant passages from uploaded policies using keyword and semantic search, with citations.
- **Hybrid questions:** Combine shipment facts and applicable policies, explicitly surfacing missing information or conflicting documents.
- **Inspectable reasoning:** The Streamlit interface shows detected intent and tool calls under **How this was answered**.

Each session starts with **60 sample shipments and three shipping-policy documents**. Users can replace the data with up to three CSV/Excel files (each becomes its own table, so questions can span tables) and upload their own documents. This is a **demo using uploaded snapshots**, not an integration with live enterprise systems.

## Architecture

```text
  CSV / XLSX ──► Validate ──► SQLite tables ──────────────────────┐
                                                                │
  PDF / TXT / MD ─► Validate ─► Chunk + embed ─► Document index ─┤
                                                                │
  User question ─► Structured intent classification ─────────────┤
                     (database / document / hybrid)              ▼
                                                       Google ADK agent
                                                       (Gemini + tools)
                                                                │
                                          ┌─────────────────────┴───────────────────┐
                                          ▼                                         ▼
                                    run_sql                                search_documents
                                   (read-only)                           (keyword + semantic)
                                          └─────────────────────┬───────────────────┘
                                                                ▼
                                                    Grounded response
                                         SQL evidence + document citations
                                         + explicit gaps / conflicts
```

**Key engineering decisions**

| Choice | Rationale and trade-off |
|---|---|
| **One ADK agent, two tools** | Keeps orchestration simple while allowing SQL, retrieval, or both; avoids unnecessary multi-agent coordination. |
| **Structured intent classification** | Provides a database/document/hybrid routing hint without restricting the agent's tool choice; adds one Gemini call per turn. |
| **SQLite per session** | Makes uploaded data immediately queryable with minimal setup; not a substitute for an enterprise operational database. |
| **Keyword + embedding retrieval** | Supports exact policy terms and semantic matching; falls back to keyword search if embeddings are unavailable. |
| **Evidence-oriented response format** | Separates database facts, document guidance, sources, and unresolved gaps; outputs still require verification. |

## Try the demo

**[Launch the Live Demo](https://data-qa-ui-701944778941.us-central1.run.app/)** to explore SQL queries, document retrieval, and hybrid question answering.

| Question | Demonstrated capability |
|---|---|
| **Where is shipment 12345?** | SQL lookup over the shipment table |
| **What is the policy for delayed shipments?** | Document retrieval with citations |
| **Shipment 12345 is delayed. What should I do?** | Combines shipment records with policy guidance |
| **Which delayed shipments have passed their escalation threshold?** | Applies document rules to structured data |
| **Shipment 12347 is late. When do I escalate it?** | Surfaces conflicting policy thresholds rather than silently choosing one |
| **Which customer has the highest total order amount?** (after uploading `customers.csv` and `orders.csv` from [`multi_db/`](db-doc-qa/sample_data/database/multi_db/), which also holds `sales.csv`) | Joins the uploaded tables with SQL |

The UI also demonstrates missing-record handling and refusal of write requests such as `Delete all rows`.

## Implemented safeguards

- **Upload validation:** CSV/XLSX data-file uploads and PDF/TXT/MD documents have format and size constraints; invalid files are rejected.
- **Restricted SQL:** Single read-only `SELECT`/`WITH` statements; 100-row output cap, 200-character cell cap, and approximately 5-second query limit.
- **Session isolation and cleanup:** Session-specific temporary SQLite databases; cleanup on **End session**, after 30 minutes of inactivity (checked at the next interaction), or on app restart. No durable session storage.
- **Configuration:** Validated settings in `data_agent/config.py`, with optional YAML overrides via `DATA_AGENT_CONFIG`.
- **Evaluation harness:** Reproducible configurations and per-case reports for tool sequencing, retrieval, answer checks, and citation precision; includes retrieval-only ablation testing.

<!-- **Measured benchmark — LakeQuest bank sample:** An internal 40-case run (20 hybrid, 10 database, 10 document) reported **0.97 tool-sequence match**, **0.79 section recall**, **2.8 s p50**, and **6.3 s p95** latency. Overall token-level `answer_f1` was **0.34**; hybrid cases were the principal weakness, including incorrect policy selection. These are internal sample results, **not a production SLA or public benchmark leaderboard score**. See the evaluation commands below. -->

## Evaluation

** [HybridQA Dataset](https://github.com/wenhuchen/HybridQA) **

Evaluated on a 50-question sample (25 table, 25 passage) of the public HybridQA **dev set**, not the full dev set or the hidden test set, using Gemini 3.5 Flash-Lite, with local `nomic-embed-text` embeddings (Ollama). Each question gets its own Wikipedia table and the articles linked from its cells (the standard "gold table" setting).
(0 errors, p50 4.6 s, p95 24.5 s). **HybridQA metrics are percentages (0–100)**, as in the official scorer:

| Type | EM | F1 | `answer_contains` |
|---|---|---|---|
| table (25) | 68.0 | 78.2 | 88 |
| passage (25) | 56.0 | 69.3 | 76 |
| **overall** | **62.0** | **73.8** | **82** |

EM and F1 score the first line of the answer and match the official HybridQA scorer exactly. `answer_contains` is the lenient check: the gold answer appears anywhere in the answer. Of the 19 questions with EM 0, 8 were right but worded differently (an added unit or word, such as `104 kg` for `104`), 5 had the right article but the wrong span, 4 used the wrong table row, and 2 used the wrong or a missing article.

**Key finding:** Answer wording, not retrieval, is the largest single cause of lost EM. Results are one run on a small internal sample (one question is 2 points), not a leaderboard score. The data is internal-use only.

** [LakeQuest Dataset](https://github.com/michael0402/LakeQuest/) **

Evaluated on 40 LakeQuest bank cases (20 hybrid, 10 database, 10 document) using Gemini 3.5 Flash-Lite.
(0 errors, p50 2.8 s, p95 6.3 s;):

| Type | `answer_f1` | `object_f1` | `tool_sequence_match` | `section_recall` |
|---|---|---|---|---|
| hybrid (20) | 0.33 | 0.59 | 0.95 | 0.58 |
| table (10) | 0.31 | 0.66 | 1.00 | – |
| document (10) | 0.38 | 1.00 | 1.00 | 1.00 |
| **overall** | **0.34** | **0.71** | **0.97** | **0.79** |

Token F1 rewards wording, not correctness: table and section answers are right (`answer_contains` 1.00) but their one-token references keep `answer_f1` low. The weak spot is the hybrid type, where the agent sometimes picks the wrong policy.

**Key finding:** Tool orchestration was reliable, but hybrid question answering remains the main improvement area, particularly selecting the correct policy. Results are from a small internal benchmark, not a production evaluation. These LakeQuest results are from the intent-classifier-off run: `configs/eval/lakequest_bank.yaml` now turns the classifier on with a bank-domain prompt (`agent.intent_prompt_file`), so add `agent.intent_enabled=false` to reproduce them.

** Latency trace (`shipments_v1`) **

Opt-in per-stage timing (`agent.latency_trace: true`) on 6 cases × 5 repeats, Gemini 3.5 Flash-Lite, in-process. Median query time (without document ingestion) was 3.7 s, p95 7.4 s.

| Finding | Measurement |
|---|---|
| Model calls dominate | 47–66 % of a turn, about 0.7 s per call; latency follows the number of calls (1–6 per turn) |
| Intent classifier | About 0.8 s per turn; turning it off cut the median by 22 % (3.7 s → 2.8 s), with 1 extra tool call in 30 turns |
| Document search | 0.31 s per call, almost all query embedding; the search itself is under 1 ms |
| Slowest case | Record with no matching policy: 6 model calls, 7.0 s median |
| Noise | Two identical runs differ by about 0.5 s, so smaller gains are not meaningful |

**Caveat:** These numbers come from a very small dataset (6 questions, 30 turns per run), so they show where time goes, not a production latency estimate.

Details: [latency_report.md](dev_docs/design/latency_report.md).

## Local quick start

**Requirements:** Python 3.12, [uv](https://docs.astral.sh/uv/), and a [Google AI Studio API key](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/Rownak/database-qa-agent.git
cd database-qa-agent/db-doc-qa
uv sync
```

Create `db-doc-qa/.env` (from the repository root) with:

```dotenv
GOOGLE_API_KEY=your-gemini-api-key
```

Run from `db-doc-qa/`:

```bash
uv run streamlit run app.py    # Upload files and chat
uv run pytest                  # Local tests; no API key required
```

Alternatively, `uv run adk web` opens the ADK interface: select `data_agent`. This mode uses only the bundled samples and does not support uploads. The `.env` file is git-ignored. **API-key authentication is tested; Vertex AI credentials are unverified.**

### Evaluation

```bash
uv run python -m evals configs/eval/smoke.yaml
uv run python -m evals configs/eval/bm25_only.yaml
```

Runs save resolved configuration, per-case results, and metrics under `runs/`. The extended LakeQuest workflow requires separately downloaded benchmark data; it is not part of this quick start.

**Latency trace** (off by default; needs the API key, about 3 minutes):

```bash
uv run python -m evals configs/eval/latency_shipments.yaml                         # tracing on, 30 turns
uv run python -m evals configs/eval/smoke.yaml agent.latency_trace=true dataset.limit=1   # quick check
```

The summary prints a `stage p50` line and per-tag latency. Each `results.jsonl` row gets a `timing` object and `metrics.json` gets `latency_stages`. Add `agent.intent_enabled=false` (or any other `key=value`) to compare a change.

**Embedding rate limits:** indexing many documents for a benchmark can hit the Gemini embedding rate limit (`ClientError`, retrieval falls back to BM25 only). For evaluation, the run config can choose a local Ollama embedding model instead (`agent.embedding.provider: ollama`, `model: nomic-embed-text:latest`, see `configs/eval/hybridqa_dev50_ollama.yaml`; needs `ollama pull nomic-embed-text`). The default stays Gemini, and a bundle must be rebuilt when the provider changes.

## Deployment status and next steps

**Demonstrated GCP deployment:** Two Cloud Run services: a private ADK API (`data-qa-api`) and a public Streamlit UI (`data-qa-ui`) that invokes it using a service account. The API key is stored as a secret. Deployment instructions are in [GCP setup](db-doc-qa/dev_docs/deploy/phase_2_1_gcp_setup.md) and [deployment guide](db-doc-qa/dev_docs/deploy/deploy.md). These links assume this README is at the repository root.

**Not production-ready:** The public UI has no end-user access control; anyone with the URL can consume quota. Sessions are in memory and must be rebuilt after backend restarts. Cold starts can be significant (approximately 30 seconds in the documented setup). Storage is temporary; there is no CI/CD, durable state, or multi-region deployment.

**Next steps:**

1. Integrate authorized enterprise databases and **real-time APIs** (not implemented); enforce user-level data permissions.
2. Add authenticated UI access, persistent session/state storage, production monitoring, and deployment automation.
3. Improve hybrid policy selection and expand evaluation using representative real-world tasks, security tests, and latency budgets.

**Demo constraints:**

- **Data files:** up to three per session, each its own table (CSV/XLSX up to 500 KB; first Excel sheet only).
- **Table names:** taken from the file name (`Sales 2024.csv` becomes `sales_2024`; a leading digit adds `t_`; a repeated name adds `_2`, `_3`).
- **Replacing files:** uploading a file with a loaded file's name replaces it.
- **Sample data:** the first upload replaces the sample table (`data`); removing every file restores it.
- **Documents:** up to three (PDF/TXT/MD, up to 1 MB and 20 pages each).
- **Unsupported:** scanned or password-protected PDFs.
- **Conversation:** replacing data resets it.
- **Accuracy:** LLM outputs can still be inaccurate; SQL and citations are available for inspection.
