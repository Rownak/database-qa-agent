# database-qa-agent

A demo project for hybrid database-and-document question answering, built with the **Google Agent Development Kit (ADK)**. You ask questions in plain language about a table of records and about policy documents. Answers from the table are grounded in SQL, and answers from the documents carry citations. The app lives in `db-doc-qa/`.

## Overview

- Every session starts with a bundled sample: a table of 60 shipments (`shipments.csv`) and three shipping-policy documents, so you can ask questions right away.
- Upload your own CSV or Excel (`.xlsx`) file to replace the table, and up to 3 documents (`.pdf`, `.txt` or `.md`) to replace or add to the policies. Files are validated first, and invalid files are rejected with a clear message.
- One ADK agent (`qa_orchestrator`, Gemini model `gemini-3.5-flash-lite`) has two read-only tools: `run_sql` for records, and `search_documents` for policy (keyword and semantic search over the documents).
- Before each turn a small structured Gemini call classifies the question as `database`, `document` or `hybrid`. The intent is a routing hint, not a restriction. **This adds one extra LLM call per turn.**
- Answers are split into **From the database**, **From the documents** (with citations such as `[shipment_delay_policy.pdf, §3.2 Express Shipments, p. 1]`), **Gaps or conflicts**, and a `Sources:` line. The Streamlit UI also shows the intent and every tool call under **How this was answered**.
- The agent says when the data can't answer a question (a missing record, no matching policy section, documents that disagree) instead of guessing. Text inside uploaded documents is treated as data, never as instructions.

## Workflow

```
shipments.csv or CSV/XLSX upload ──► validate ──► SQLite "data" ───────────────┐
                                                                                ├─► intent ─► ADK agent ─► run_sql / search_documents ─► cited answer
sample policies or PDF/TXT/MD upload ─► validate ─► chunk ─► embed ─► docs.db ──┘
```

## Prerequisites and setup

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

```bash
git clone https://github.com/Rownak/database-qa-agent.git
cd database-qa-agent/db-doc-qa
uv sync
```

Create `db-doc-qa/.env`:

```
GOOGLE_API_KEY=<your-gemini-api-key>
```

`.env` is git-ignored. Only API-key access was tested; Vertex AI credentials have not been verified.

## Run locally

Run all commands from `db-doc-qa/`.

**Streamlit app** (upload and chat):

```bash
uv run streamlit run app.py
```

**ADK Web** (chat only, with the sample data):

```bash
uv run adk web
```

Select `data_agent` in the ADK Web UI. Uploads are not supported here; each session uses the sample table and sample documents.

**Tests** (no API key needed):

```bash
uv run pytest
```

## Configuration and evaluation

Every setting that changes an answer lives in one validated model, `data_agent/config.py` (models, embedding, chunking, retrieval, intent on/off). The defaults equal the values the deployed agent uses, so nothing changes unless you set something.

| Folder | Purpose |
|---|---|
| `data_agent/` | The agent pipeline, deployed to Cloud Run as is: `config.py`, `agent.py`, `runner.py` (`AgentSession`, the in-process entry point used by the app and the evaluation), `events.py` (`TurnResult`), `tools.py`, `retrieval.py`, `doc_ingest.py`, `ingest.py`, `intent.py` |
| `configs/eval/` | Evaluation configs: `smoke.yaml` (full agent), `bm25_only.yaml` (retrieval ablation) |
| `evals/` | The evaluation runner, dataset (`evals/datasets/shipments_v1.yaml`), metrics and report writer. Not deployed |
| `runs/` | Evaluation outputs (git-ignored) |

**Change the settings of the app or the server.** Put an `agent:` block in a YAML file and point `DATA_AGENT_CONFIG` at it before starting `streamlit run app.py` or `adk web`. Leave it unset (the deployed server does) to use the defaults. An unknown key or an out-of-range value stops the start with a validation error.

**Run an evaluation** (from `db-doc-qa/`; the agent mode calls the model and needs `GOOGLE_API_KEY`):

```bash
uv run python -m evals configs/eval/smoke.yaml
uv run python -m evals configs/eval/smoke.yaml agent.retrieval.min_dense_sim=0.55 name=smoke_055
uv run python -m evals configs/eval/bm25_only.yaml      # retrieval only: no LLM call, the chunks are embedded once
```

Values are resolved as defaults, then the YAML file, then the `key=value` overrides. Each run writes `runs/<timestamp>_<name>/` with `config.yaml` (the resolved config, which reproduces the run), `results.jsonl` (one line per case) and `metrics.json`. Agent mode reports `tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision`; retrieval mode (`eval.mode: retrieval`) reports `section_recall` and `mrr`. Model output varies between runs, so use `eval.repeats` for comparisons.

## Example questions

With the sample data (table columns: `shipment_id`, `shipment_type`, `status`, `origin`, `destination`, `current_location`, `ship_date`, `expected_delivery_date`, `last_update_date`, `delivered_date`, `delay_days`):

| Question | Intent | Tools |
|---|---|---|
| What is the policy for delayed shipments? | document | `search_documents` |
| Where is shipment 12345? | database | `run_sql` |
| Shipment 12345 is delayed. What should I do? | hybrid | `run_sql`, then `search_documents` |
| Which delayed shipments have passed their escalation threshold? | hybrid | `search_documents`, then `run_sql` |
| Shipment 12349 is delayed. What should I do? | hybrid | no Refrigerated policy: the general rule is used and the gap is stated |
| Shipment 12347 is late. When do I escalate it? | hybrid | two documents give 5 and 7 days: both are cited as a conflict |
| Shipment 99999 is delayed. What should I do? | hybrid | record not found: only general guidance, labelled as such |
| Delete all rows | database | refused (read-only) |

## Deploy to Cloud Run

The demo runs on Google Cloud Run as two services:

- **`data-qa-api`**: the ADK agent behind `adk api_server`, deployed with `adk deploy cloud_run`. It is private (needs an ID token) and holds the secret `GOOGLE_API_KEY`.
- **`data-qa-ui`**: the Streamlit UI. It is public, validates and previews uploads itself, and calls the API as the service account `data-qa-ui-sa`.

This is a low-traffic development setup: no persistent storage, custom domain, CI/CD or multi-region support. Docker is not needed; Cloud Build builds the images.

**Commands**
- One-time project setup (project, billing, APIs, service accounts, secret, budget): [dev_docs/deploy/phase_2_1_gcp_setup.md](dev_docs/deploy/phase_2_1_gcp_setup.md)
- Deploying the backend and UI, checks, and tear down: [dev_docs/deploy/deploy.md](dev_docs/deploy/deploy.md) (section "Deploy and tear down commands"), along with this project's URLs and settings.

After `adk deploy`, the Vertex AI environment variables must be removed (the command is in `deploy.md`); otherwise the backend returns HTTP 500.

**Open the UI** at the URL printed by the deploy (`gcloud run services describe data-qa-ui --region us-central1 --format="value(status.url)"`). The UI is public, so anyone with the link can use it and spend Gemini quota; delete the service when you are not using it.

**Behaviour to know**
- **Cold starts:** with `--min-instances=0` the first request after idle can take about 30 seconds (measurements in [sprint_2_6.md](dev_docs/execution_logs/sprint_2_6.md)).
- **Backend restarts:** sessions are in memory. If the backend instance is replaced, the UI recreates the session from the table and documents it still holds, clears the chat and shows a notice. The next question rebuilds both databases (including an embedding call for the documents).
- **Stale browser tab:** a tab left open while the **UI** instance is replaced (a deploy, a crash, the 60-minute websocket limit) keeps a dead Streamlit session, and uploads then fail with `Request failed with status code 400`. Reload the page.
- **UI against a local API server:** run `uv run adk api_server`, then `API_URL=http://localhost:8000 uv run streamlit run app.py` (PowerShell: set `$env:API_URL` first). Without `API_URL` the UI runs the agent in-process.

**Cost notes** (verify on the [Cloud Run pricing page](https://cloud.google.com/run/pricing); this is not a $0 guarantee)
- Request-based billing has a monthly free tier (180,000 vCPU-seconds, 360,000 GiB-seconds, 2 million requests per billing account) in Tier 1 regions such as `us-central1`. With min instances at 0, idle time is free.
- **An open Streamlit tab holds a websocket request, so the UI instance is billed for as long as a tab is open.** Close tabs; `--max-instances=1` limits scale.
- Other charges: Cloud Build beyond 2,500 free minutes a month, Artifact Registry storage for old images, Secret Manager access operations, network egress, and Gemini API usage (billed separately by Google AI Studio). A budget alert only notifies you.

## Limitations

- **Table uploads:** `.csv` and `.xlsx` only, up to 500 KB. The file needs a header row with unique, non-numeric column names. For Excel, only the first sheet is loaded and the other sheets are listed as ignored.
- **Document uploads:** `.pdf`, `.txt` and `.md`, up to 1 MB and 20 pages each, at most 3 per session. A scanned (image-only) or password-protected PDF is rejected. Adding a file with the name of a loaded document replaces it. Any table or document change starts a new agent session and clears the chat.
- **One table:** each session holds a single table.
- **Read-only SQL:** only single `SELECT`/`WITH` queries run, on a read-only connection. Results are capped at 100 rows and 200 characters per cell, and queries stop after about 5 seconds. Requests to change data are refused.
- **Retrieval:** by default search returns at most 8 passages of at most 1,200 characters (`retrieval.max_top_k` and `max_passage_chars` in the configuration). If the embedding service is unavailable, search falls back to keyword matching only, and the tool says so.
- **Sessions and cleanup:** data lives in a temp folder (`<system temp>/adk_sheet_qa/<session_id>/`, holding `data.db` and `docs.db`) and is deleted by **End session**, after 30 minutes of inactivity (checked on the next interaction), or when the app restarts. Nothing is persisted. On Cloud Run the backend rebuilds both files from the state held by the UI whenever its instance is replaced.
- **Answers:** answers are generated by an LLM, so wording and occasionally accuracy can vary. Check the SQL and the citations shown with each answer.
