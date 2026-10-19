# database-qa-agent

A demo project for database question answering, built with the **Google Agent Development Kit (ADK)**. You ask questions in plain language about tabular data and get answers grounded in SQL. The current scope is CSV and Excel (`.xlsx`) files.

## Overview

- Starts every session with a bundled sample dataset (`sales.csv`), so you can ask questions right away.
- Upload your own CSV or Excel file to replace it. The file is validated first, and invalid files are rejected with a clear message.
- An ADK agent (`data_analyst`, Gemini model `gemini-3.5-flash-lite`) answers with four read-only tools: table info, sample rows, column statistics, and SQL `SELECT` queries.
- Answers end with a `Source:` line naming the tool or SQL used. The Streamlit UI also shows the tool calls under **How this was answered**.
- The agent says when the data can't answer a question (for example, a missing column) instead of guessing.

## Workflow

```
data_agent/sample_data/sales.csv ──┐
                        ├─► validate ─► SQLite table "data" (per session) ─► ADK agent ─► tools / read-only SQL ─► answer + Source
optional CSV/XLSX upload┘
```

## Prerequisites and setup

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

```bash
git clone https://github.com/Rownak/database-qa-agent.git
cd database-qa-agent/spreadsheet-qa
uv sync
```

Create `spreadsheet-qa/.env`:

```
GOOGLE_API_KEY=<your-gemini-api-key>
```

`.env` is git-ignored. Only API-key access was tested; Vertex AI credentials have not been verified.

## Run locally

Run all commands from `spreadsheet-qa/`.

**Streamlit app** (upload and chat):

```bash
uv run streamlit run app.py
```

**ADK Web** (chat only, with the sample data):

```bash
uv run adk web
```

Select `data_agent` in the ADK Web UI. Uploads are not supported here; each session uses `sales.csv`.

**Tests** (no API key needed):

```bash
uv run pytest
```

## Example questions

With the sample data (`order_id`, `order date`, `category`, `sales_amount`, `revenue`):

- How many records are there?
- What is the average sales amount?
- Which category has the highest total revenue?
- Show 5 sample rows.
- Give me statistics for the revenue column.
- What is the profit margin? *(explains that cost data is missing)*

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
- **Backend restarts:** sessions are in memory. If the backend instance is replaced, the UI recreates the session from the upload it still holds, clears the chat and shows a notice.
- **Stale browser tab:** a tab left open while the **UI** instance is replaced (a deploy, a crash, the 60-minute websocket limit) keeps a dead Streamlit session, and uploads then fail with `Request failed with status code 400`. Reload the page.
- **UI against a local API server:** run `uv run adk api_server`, then `API_URL=http://localhost:8000 uv run streamlit run app.py` (PowerShell: set `$env:API_URL` first). Without `API_URL` the UI runs the agent in-process.

**Cost notes** (verify on the [Cloud Run pricing page](https://cloud.google.com/run/pricing); this is not a $0 guarantee)
- Request-based billing has a monthly free tier (180,000 vCPU-seconds, 360,000 GiB-seconds, 2 million requests per billing account) in Tier 1 regions such as `us-central1`. With min instances at 0, idle time is free.
- **An open Streamlit tab holds a websocket request, so the UI instance is billed for as long as a tab is open.** Close tabs; `--max-instances=1` limits scale.
- Other charges: Cloud Build beyond 2,500 free minutes a month, Artifact Registry storage for old images, Secret Manager access operations, network egress, and Gemini API usage (billed separately by Google AI Studio). A budget alert only notifies you.

## Limitations

- **Uploads:** `.csv` and `.xlsx` only, up to 500 KB. The file needs a header row with unique, non-numeric column names. For Excel, only the first sheet is loaded and the other sheets are listed as ignored.
- **One table:** each session holds a single table. A valid upload replaces the current data and clears the chat. An invalid upload keeps the current data and chat.
- **Read-only SQL:** only single `SELECT`/`WITH` queries run, on a read-only connection. Results are capped at 100 rows and 200 characters per cell, and queries stop after about 5 seconds. Requests to change data are refused.
- **Sessions and cleanup:** data lives in a temp folder (`<system temp>/adk_sheet_qa/<session_id>/`) and is deleted by **End session**, after 30 minutes of inactivity (checked on the next interaction), or when the app restarts. Nothing is persisted. On Cloud Run the backend rebuilds a session's database from the upload held by the UI whenever its instance is replaced.
- **Answers:** answers are generated by an LLM, so wording and occasionally accuracy can vary. Check the SQL shown with each answer.
