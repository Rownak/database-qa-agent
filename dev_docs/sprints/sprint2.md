# Sprint 2 — Deploy to Google Cloud Run

**Goal:** run the database Q&A demo on GCP as two Cloud Run services: an ADK agent API (`data-qa-api`) deployed with `adk deploy cloud_run`, and the Streamlit UI (`data-qa-ui`) calling it with authentication. The setup targets low-traffic development and testing. Out of scope: persistent storage, custom domains, CI/CD, and multi-region.

| Phase | Focus |
|---|---|
| 2.1 | GCP project, APIs, accounts, secret, budget |
| 2.2 | Backend ready for Cloud Run |
| 2.3 | UI as an API client |
| 2.4 | Deploy the backend |
| 2.5 | Deploy the UI |
| 2.6 | End-to-end verification, cost check, README |

## Decisions (local design → Cloud Run)

| Local design (Sprint 1) | Cloud Run conflict | Decision for this demo |
|---|---|---|
| UI runs the agent in-process | Goal is two services | UI calls the ADK API server (`POST /apps/data_agent/users/{user}/sessions/{session}`, `POST /run`, `DELETE` session) with an ID token. |
| `load_file` writes `data.db` in the UI's session folder | The backend holds the database; the ADK API has no upload endpoint | The UI validates the file and shows the preview locally, then creates the ADK session with state `upload_name` and `upload_b64` (the 500 KB limit keeps this small). A backend `before_agent_callback` builds `/tmp/adk_sheet_qa/<session_id>/data.db` from that state, or from the sample when there is no upload. |
| Folders survive while the process lives | `/tmp` is in-memory, counts toward the 512 MiB, and is lost when an instance stops | The callback rebuilds `data.db` whenever the file is missing. `sweep_stale` (30 min) runs in the callback. |
| `InMemorySessionService` per browser session | ADK sessions (`memory://`) are lost when the instance scales to 0 or is replaced | Backend `--max-instances=1` so all sessions share one instance. If `/run` returns 404, the UI recreates the session from the upload bytes it still holds and tells the user the chat was reset. No database-backed session store. |
| `sample_data/` sits beside the package | `adk deploy` uploads only the agent folder | Move `sales.csv` to `data_agent/sample_data/`. |
| `.env` holds `GOOGLE_API_KEY` | Secrets must not go in images | Store it in Secret Manager and mount it with `--set-secrets` on the backend only. Add a `.gcloudignore` so `.env` is never uploaded. |

**Sizing:** each service uses 1 vCPU, 512 MiB, `--min-instances=0`. 512 MiB with 1 vCPU is valid (the gen2 minimum is 512 MiB; 1 vCPU allows up to 4 GiB). The backend is the risk: ADK, google-genai and pandas, plus in-memory `/tmp` databases, all share 512 MiB. If phase 2.6 shows memory above about 80% or out-of-memory restarts, the smallest fix is `--memory=1Gi` on the backend only. Keep the UI at 512 MiB.

**Cost (verify on the pricing pages before deploying; this is not a $0 guarantee):**
- Cloud Run request-based billing includes a monthly free tier of 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per billing account, priced on Tier 1 regions. Use `us-central1`. With min instances at 0, idle time is free.
- An open Streamlit tab holds a WebSocket request, so the UI instance is billed for the whole time a tab is open, not just during clicks. Keep `--max-instances=1` and close tabs.
- Other costs:
  - Cloud Build: 2,500 free build-minutes per month on the default `e2-standard-2` pool.
  - Artifact Registry storage: the source-deploy images accumulate, so delete old ones.
  - Secret Manager: access operations.
  - Network egress.
  - Gemini API usage, billed by Google AI Studio and separate from Cloud Run.
- A budget alert notifies you but does not cap spending.

---

## Phase 2.1 — GCP Project and Prerequisites

**Goal:** a project with billing, the required APIs, least-privilege service accounts, the API key in Secret Manager, and a budget alert.

- [x] Install and log in to the gcloud CLI: `gcloud auth login`, `gcloud config set project <PROJECT_ID>`, `gcloud config set run/region us-central1`.
- [x] Enable the APIs: `gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com`.
- [x] Create the service accounts `data-qa-api-sa` (backend runtime) and `data-qa-ui-sa` (UI runtime).
- [x] Grant the default compute service account `roles/cloudbuild.builds.builder`, as the ADK Cloud Run guide requires for source builds.
- [x] Store the key: `printf '%s' '<GOOGLE_API_KEY>' | gcloud secrets create GOOGLE_API_KEY --data-file=-`. Then grant `data-qa-api-sa` `roles/secretmanager.secretAccessor` on that secret only.
- [x] Create a Cloud Billing budget for the project, for example $5 a month, with alerts at 50%, 90% and 100%.
- [x] Record the project ID, region and service account emails in `dev_docs/deploy.md`. No secrets go in that file.

## Phase 2.2 — Backend Ready for Cloud Run (`data_agent/`)

**Goal:** the agent folder deploys on its own, builds its database from session state, and runs behind `adk api_server` locally.

- [x] Set `data_agent/__init__.py` to `from . import agent`. Add `data_agent/requirements.txt` with `pandas`, `openpyxl` and `python-dotenv`, pinned to the `uv.lock` versions. Don't list `google-adk`; check whether the generated Dockerfile installs it.
- [x] Move `sample_data/sales.csv` to `data_agent/sample_data/sales.csv` and update `SAMPLE_FILE`. Keep the other sample files where they are for the tests.
- [x] Replace `seed_state_if_missing` with `ensure_data(callback_context)`:
  - If `db_path` is missing, or its file doesn't exist on this instance, load `upload_b64` and `upload_name` (decoded), or the sample, into `/tmp/adk_sheet_qa/<session_id>/` with `load_file`.
  - Then set `db_path`, `column_map` and `schema_summary`.
  - On an `IngestError`, fall back to the sample and set `load_error` in state, so the agent can explain it.
  - Then call `touch` and `sweep_stale`.
- [x] Add `logging` (INFO): session data built (sample or upload, rows, seconds), ingest errors, sweep removals, tool errors. Don't log file contents or the API key.
- [x] Tests: `ensure_data` covers the sample, a valid upload, an invalid upload falling back to the sample, rebuilding after `data.db` is deleted, and doing nothing when `data.db` is present. Run `uv run pytest`.
- [x] Local check: `uv run adk api_server` from `spreadsheet-qa/`. Create a session with an upload in state through `curl`, call `POST /run`, and confirm the answer and the `Source:` line. Note the `/run` request and response shape in `deploy.md` (also visible at `/docs`).
- [x] Add `dev_test/phase_2_2.py`, which runs that create-session and `/run` round trip against `http://localhost:8000`.

## Phase 2.3 — UI as an API Client (`app.py`, `ui_client.py`)

**Goal:** Streamlit talks only to the backend API, with an ID token, and keeps all Sprint 1 UI behaviour.

- [ ] Add `ui_client.py` with `create_session(sid, state)`, `ask(sid, question)` (returns the answer and tool calls from the `/run` events), and `delete_session(sid)`.
  - Use `requests` with a timeout.
  - When `API_URL` is not localhost, send `Authorization: Bearer <token>` from `google.oauth2.id_token.fetch_id_token(Request(), API_URL)`.
  - Cache the token and refresh it before it expires (about 1 hour).
- [ ] Update `app.py`:
  - Upload: validate and preview with `load_file` into the UI's `/tmp` folder, then send `upload_name` and `upload_b64` in a new session's state and delete the old session.
  - End session: delete the session through the API, then delete the local folder.
  - On a 404 from `ask`, recreate the session from the held upload, clear the chat, and show `st.info`.
  - Keep the split layout, the expander and the error messages.
- [ ] Keep a local mode: if `API_URL` is unset, use the in-process `Runner` as today, so `uv run streamlit run app.py` still works without GCP.
- [ ] Add `google-auth` and `requests` to the project. Add `Dockerfile.ui`:
  - base image `python:3.12-slim`
  - install the UI dependencies only (`streamlit`, `pandas`, `openpyxl`, `requests`, `google-auth`)
  - copy `app.py`, `ui_client.py`, `data_agent/ingest.py`, `data_agent/sessions.py` and `data_agent/sample_data/`
  - start with `CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true`
- [ ] Add `.gcloudignore` (and `.dockerignore`) excluding `.env`, `.venv/`, `tests/`, `dev_test/`, `tmp_session*/` and `__pycache__/`.
- [ ] Tests: mock `requests` for `ui_client` (headers, 404 handling, parsing `/run` events). The `AppTest` tests run in local mode. Run `uv run pytest`.
- [ ] Local check: run `adk api_server` and `API_URL=http://localhost:8000 uv run streamlit run app.py` together. Upload, ask and end the session.

## Phase 2.4 — Deploy the Backend (`data-qa-api`)

**Goal:** a private Cloud Run service running the ADK API server.

- [ ] Deploy from `spreadsheet-qa/` with ADK's built-in command:
  ```
  uv run adk deploy cloud_run --project=<PROJECT_ID> --region=us-central1 \
    --service_name=data-qa-api --app_name=data_agent --session_service_uri=memory:// \
    data_agent -- --no-allow-unauthenticated \
    --service-account=data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com \
    --set-secrets=GOOGLE_API_KEY=GOOGLE_API_KEY:latest \
    --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --concurrency=10 --timeout=120
  ```
  Don't add `--with_ui`. Everything after `--` is handed to `gcloud run deploy` (the installed ADK has `extra_gcloud_args`). If a flag doesn't take effect, apply it with `gcloud run services update data-qa-api ...`.
- [ ] Confirm that `gcloud run services describe data-qa-api` shows the service account, the secret, 1 CPU, 512Mi, min 0 and max 1.
- [ ] Smoke test with a user token:
  - Run `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $API_URL/list-apps`; it should return `data_agent`.
  - Create a session, then call `/run` with "How many records are there?" (expected answer: 50).
  - Without a token, the API should return 403.
- [ ] Check Cloud Logging for the startup log and the "session data built" log line. Look at the memory-utilization metric after a few questions.

## Phase 2.5 — Deploy the UI (`data-qa-ui`)

**Goal:** a public Streamlit service that calls the private backend as `data-qa-ui-sa`.

- [ ] Grant `data-qa-ui-sa` `roles/run.invoker` on `data-qa-api` only: `gcloud run services add-iam-policy-binding data-qa-api --member=serviceAccount:data-qa-ui-sa@... --role=roles/run.invoker`.
- [ ] Build and deploy the UI. Either point the deploy at `Dockerfile.ui` through Cloud Build, or name it `Dockerfile` in a small `ui/` build context:
  ```
  gcloud run deploy data-qa-ui --source <ui build context> --region=us-central1 \
    --allow-unauthenticated --service-account=data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com \
    --set-env-vars=API_URL=<data-qa-api URL> \
    --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --session-affinity --timeout=3600
  ```
  Use `--session-affinity` and the longer timeout to keep the Streamlit WebSocket on one instance. The backend stays private.
- [ ] Open the UI URL. The sample data loads, and "How many records are there?" returns 50 with a `Source:` line.
- [ ] Delete old images in Artifact Registry (`cloud-run-source-deploy`) after a successful deploy, or add a cleanup policy that keeps the last 2 images.

## Phase 2.6 — End-to-End Verification, Cost Check, README

**Goal:** Sprint 1's acceptance criteria pass on Cloud Run, costs and limits are confirmed, and the README explains deployment.

- [ ] Rerun the Sprint 1.7 checks on the UI URL:
  - valid and invalid uploads
  - the five example questions
  - the delete refusal
  - the `Source:` line on every answer
  - **End session**
- [ ] Session isolation: two browser windows with different uploads each get their own answers.
- [ ] Instance lifecycle:
  1. Wait until both services scale to 0, or force a new revision.
  2. Ask again.
  3. The UI recreates the session and shows the reset notice, and the answer uses the same uploaded data.
- [ ] Cold start: record the first-request latency for each service. Record the backend's peak memory. Apply `--memory=1Gi` to the backend only if needed, and note it.
- [ ] Logs: confirm one Cloud Logging query shows backend ingest, tool and error lines and UI errors, with no file contents or secrets.
- [ ] Cost check after a day of testing: review the billing report for Cloud Run, Cloud Build, Artifact Registry and Secret Manager. Confirm the budget alert is active.
- [ ] Update `README.md` with a short **Deploy to Cloud Run** section:
  - prerequisites
  - the phase 2.1 setup commands
  - both deploy commands
  - how to open the UI
  - cost notes (free tier conditions, open tabs being billed, the budget alert)
  - how to tear down: `gcloud run services delete`, `gcloud secrets delete`, delete Artifact Registry images
- [ ] Record any failures, fixes and remaining limitations in `dev_docs/execution_logs/sprint_2_6.md`.
- [ ] Run `uv run pytest` from `spreadsheet-qa/` and confirm all tests pass.
