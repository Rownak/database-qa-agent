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

This is a low-traffic development setup. It has no persistent storage, custom domain, CI/CD or multi-region support. Details and the project's own settings are in [dev_docs/deploy/deploy.md](dev_docs/deploy/deploy.md).

### Prerequisites

- A Google Cloud project with billing, and the [gcloud CLI](https://cloud.google.com/sdk/docs/install) logged in (`gcloud auth login`).
- A Gemini API key (see above) and the project's `uv` environment (`uv sync` in `spreadsheet-qa/`).
- Docker is **not** needed; Cloud Build builds the images.

### One-time setup

Step-by-step with Console alternatives: [dev_docs/deploy/phase_2_1_gcp_setup.md](dev_docs/deploy/phase_2_1_gcp_setup.md). The commands, with `<PROJECT_ID>` and `<PROJECT_NUMBER>` replaced:

```bash
gcloud config set project <PROJECT_ID>
gcloud config set run/region us-central1

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com

gcloud iam service-accounts create data-qa-api-sa --display-name="data-qa-api runtime"
gcloud iam service-accounts create data-qa-ui-sa  --display-name="data-qa-ui runtime"

# ADK source builds run as the default compute service account
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder"

# Store the key (Git Bash; see the setup guide for PowerShell) and let only the backend read it
printf '%s' '<GOOGLE_API_KEY>' | gcloud secrets create GOOGLE_API_KEY --data-file=-
gcloud secrets add-iam-policy-binding GOOGLE_API_KEY \
  --member="serviceAccount:data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Also create a budget alert (Console: **Billing → Budgets & alerts**). It notifies you; it does not cap spending.

### Deploy the backend

Run from `spreadsheet-qa/`:

```bash
uv run adk deploy cloud_run --project=<PROJECT_ID> --region=us-central1 \
  --service_name=data-qa-api --app_name=data_agent --session_service_uri=memory:// \
  data_agent -- --no-allow-unauthenticated \
  --service-account=data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --set-secrets=GOOGLE_API_KEY=GOOGLE_API_KEY:latest \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --concurrency=10 --timeout=120

# Required after every adk deploy: ADK sets variables that switch google-genai to Vertex AI,
# which makes /run return HTTP 500 when only an API key is configured.
gcloud run services update data-qa-api --region us-central1 \
  --remove-env-vars=GOOGLE_GENAI_USE_ENTERPRISE,GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION
```

Check it with a user token (expect `["data_agent"]`; without the header you get 403):

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" <API_URL>/list-apps
```

`--max-instances=1` matters: sessions live in the instance's memory, so every session must reach the same instance.

### Deploy the UI

```bash
gcloud run services add-iam-policy-binding data-qa-api --region us-central1 \
  --member=serviceAccount:data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --role=roles/run.invoker

uv run python scripts/stage_ui_context.py      # copies Dockerfile.ui and its files into .ui_build/
gcloud run deploy data-qa-ui --source .ui_build --region=us-central1 \
  --allow-unauthenticated --service-account=data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --set-env-vars=API_URL=<API_URL> \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --session-affinity --timeout=3600
```

To keep only the two newest images per service in Artifact Registry, add a cleanup policy to the `cloud-run-source-deploy` repository (`gcloud artifacts repositories set-cleanup-policies`).

### Open the UI

Open the URL printed by the deploy (`gcloud run services describe data-qa-ui --region us-central1 --format="value(status.url)"`). The sample data loads, and "How many records are there?" returns 50 with a `Source:` line. The UI is public, so anyone with the link can use it and spend Gemini quota; delete the service or remove `allUsers` access when you are not using it.

### Behaviour to know

- **Cold starts:** with `--min-instances=0` the first request after idle waits for an instance to start (see the numbers in [dev_docs/execution_logs/sprint_2_6.md](dev_docs/execution_logs/sprint_2_6.md)).
- **Backend restarts:** sessions are in memory. If the backend instance is replaced, the UI recreates the session from the upload it still holds, clears the chat and shows a notice.
- **Stale browser tab:** a tab left open while the **UI** instance is replaced (a deploy, a crash, the 60-minute websocket limit) keeps a dead Streamlit session, and uploads then fail with `Request failed with status code 400`. Reload the page.
- **Run the UI against a local API server** (no GCP needed): start `uv run adk api_server`, then in another terminal run `API_URL=http://localhost:8000 uv run streamlit run app.py` (PowerShell: `$env:API_URL = "http://localhost:8000"` first). Without `API_URL` the UI runs the agent in-process.

### Cost notes

Verify against the [Cloud Run pricing page](https://cloud.google.com/run/pricing); this is not a $0 guarantee.

- Cloud Run request-based billing includes a monthly free tier (180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per billing account) in Tier 1 regions such as `us-central1`. With min instances at 0, idle time is free.
- **An open Streamlit tab holds a websocket request, so the UI instance is billed for as long as a tab is open**, not only during clicks. Close tabs; `--max-instances=1` limits scale.
- Other charges: Cloud Build beyond 2,500 free minutes per month, Artifact Registry storage for old images, Secret Manager access operations, network egress, and Gemini API usage (billed separately by Google AI Studio).
- A budget alert only notifies you.

### Tear down

```bash
gcloud run services delete data-qa-ui  --region us-central1
gcloud run services delete data-qa-api --region us-central1
gcloud secrets delete GOOGLE_API_KEY
gcloud artifacts repositories delete cloud-run-source-deploy --location us-central1
gcloud iam service-accounts delete data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com
gcloud iam service-accounts delete data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com
```

Or delete the whole project: `gcloud projects delete <PROJECT_ID>`.

## Limitations

- **Uploads:** `.csv` and `.xlsx` only, up to 500 KB. The file needs a header row with unique, non-numeric column names. For Excel, only the first sheet is loaded and the other sheets are listed as ignored.
- **One table:** each session holds a single table. A valid upload replaces the current data and clears the chat. An invalid upload keeps the current data and chat.
- **Read-only SQL:** only single `SELECT`/`WITH` queries run, on a read-only connection. Results are capped at 100 rows and 200 characters per cell, and queries stop after about 5 seconds. Requests to change data are refused.
- **Sessions and cleanup:** data lives in a temp folder (`<system temp>/adk_sheet_qa/<session_id>/`) and is deleted by **End session**, after 30 minutes of inactivity (checked on the next interaction), or when the app restarts. Nothing is persisted. On Cloud Run the backend rebuilds a session's database from the upload held by the UI whenever its instance is replaced.
- **Answers:** answers are generated by an LLM, so wording and occasionally accuracy can vary. Check the SQL shown with each answer.
