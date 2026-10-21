# Deployment settings
- Project ID: gen-lang-client-0031790382
- Project number: 701944778941
- Region: us-central1
- Backend SA: data-qa-api-sa@gen-lang-client-0031790382.iam.gserviceaccount.com
- UI SA: data-qa-ui-sa@gen-lang-client-0031790382.iam.gserviceaccount.com
- Secret: GOOGLE_API_KEY (Secret Manager, latest version)
- Budget: $10/month, alerts at 50/90/100%

# Deploy and tear down commands
Run from `db-doc-qa/`. One-time project setup is in [phase_2_1_gcp_setup.md](phase_2_1_gcp_setup.md). Replace `<PROJECT_ID>`, `<API_URL>`.

**1. Backend (`data-qa-api`)**
```
uv run adk deploy cloud_run --project=<PROJECT_ID> --region=us-central1   --service_name=data-qa-api --app_name=data_agent --session_service_uri=memory://   data_agent -- --no-allow-unauthenticated   --service-account=data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com   --set-secrets=GOOGLE_API_KEY=GOOGLE_API_KEY:latest   --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --concurrency=10 --timeout=120

# Required after every adk deploy (ADK switches google-genai to Vertex AI, so /run returns 500):
gcloud run services update data-qa-api --region us-central1   --remove-env-vars=GOOGLE_GENAI_USE_ENTERPRISE,GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION

# Check (expect ["data_agent"]; 403 without the header):
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" <API_URL>/list-apps
```
`--max-instances=1` matters: sessions live in the instance's memory.

**2. UI (`data-qa-ui`)**
```
gcloud run services add-iam-policy-binding data-qa-api --region us-central1   --member=serviceAccount:data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/run.invoker

uv run python scripts/stage_ui_context.py      # copies Dockerfile.ui and its files into .ui_build/
gcloud run deploy data-qa-ui --source .ui_build --region=us-central1   --allow-unauthenticated --service-account=data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com   --set-env-vars=API_URL=<API_URL>   --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --session-affinity --timeout=3600
```
(`gcloud run deploy --source` only uses a file named `Dockerfile`, so the script stages `Dockerfile.ui` as `Dockerfile`.)
Old images: add a cleanup policy to the `cloud-run-source-deploy` repository that keeps the 2 newest versions per image (`gcloud artifacts repositories set-cleanup-policies`).

**3. Tear down**
```
gcloud run services delete data-qa-ui  --region us-central1
gcloud run services delete data-qa-api --region us-central1
gcloud secrets delete GOOGLE_API_KEY
gcloud artifacts repositories delete cloud-run-source-deploy --location us-central1
gcloud iam service-accounts delete data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com
gcloud iam service-accounts delete data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com
```
Or delete everything: `gcloud projects delete <PROJECT_ID>`.

# ADK API server shape (verified locally in Phase 2.2)
App name is the agent folder: `data_agent`. Interactive docs at `/docs`.
- Create session: `POST /apps/data_agent/users/{user}/sessions/{session}`. The JSON body **is** the initial state, e.g. `{"upload_name": "x.csv", "upload_b64": "<base64>"}` (do not wrap it in `"state"`). Returns the session; 200.
- Run: `POST /run` with `{"app_name": "data_agent", "user_id": "...", "session_id": "...", "new_message": {"role": "user", "parts": [{"text": "..."}]}}`. Returns a JSON **list** of events. Tool calls are `content.parts[].functionCall` (`name`, `args`); tool results are `functionResponse`; the answer is the last event's `content.parts[].text` (ends with a `Source:` line).
- Delete session: `DELETE /apps/data_agent/users/{user}/sessions/{session}`.
- An unknown session on `/run` returns 404 (used by the UI to detect a lost session).
- Optional state keys: `upload_name`, `upload_b64` (backend builds `data.db` from them). The backend sets `db_path`, `column_map`, `schema_summary`, and `load_error` when the upload was rejected.
- The generated Dockerfile uses `python:3.11-slim` and installs `google-adk` itself, so `data_agent/requirements.txt` lists only pandas, openpyxl and python-dotenv.

# Backend deployed (Phase 2.4, 2026-10-19)
- Service: `data-qa-api`, region us-central1, private (`--no-allow-unauthenticated`), revision `data-qa-api-00008-6dc` serves 100% (revisions after 00002 only added and removed test env vars; 00002 is the env-var fix).
- URL (use this one as `API_URL` and as the ID-token audience): https://data-qa-api-701944778941.us-central1.run.app
  (the console also shows https://data-qa-api-6b3r2whd2q-uc.a.run.app; both work)
- Settings: 1 CPU, 512Mi, min 0, max 1, concurrency 10, timeout 120s, runs as `data-qa-api-sa`, `GOOGLE_API_KEY` mounted from Secret Manager (`latest`).
- Artifact Registry repo `cloud-run-source-deploy` (us-central1) was created by the first deploy.
- After every `adk deploy cloud_run`, remove the Vertex AI env vars (command above); otherwise `/run` returns 500 (`Agent Platform API has not been used`).
- gcloud on this machine is not on the Git Bash PATH: `export PATH="$PATH:/c/Users/rowna/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"`.

# UI deployed (Phase 2.5, 2026-10-19)
- Service: `data-qa-ui`, region us-central1, public (`--allow-unauthenticated`), runs as `data-qa-ui-sa`, revision `data-qa-ui-00006-7ft` serves 100% (00003/00004: a test env var added and removed; 00005: UI error logging; 00006: longer session timeout and a connecting spinner).
- URL: https://data-qa-ui-701944778941.us-central1.run.app
- Settings: 1 CPU, 512Mi, min 0, max 1, session affinity, timeout 3600s, concurrency 80 (default), env `API_URL=https://data-qa-api-701944778941.us-central1.run.app`.
- `data-qa-ui-sa` has `roles/run.invoker` on `data-qa-api` only. The backend stays private.
- Deploy command: see "Deploy and tear down commands" above.
- Artifact Registry `cloud-run-source-deploy` has a cleanup policy: delete all versions except the 2 most recent per image.
- **Known behaviour:** a browser tab left open while the UI instance is replaced (a deploy, a crash, the 3600 s websocket limit) keeps a stale Streamlit session. Uploads from that tab fail with `Request failed with status code 400` (server log: `PUT /_stcore/upload_file/... 400`, "Invalid session_id"). Reload the page to fix it; it is not related to file size.

# Verification and measurements (Phase 2.6, 2026-10-19)
- Browser checks (`dev_test/phase_2_6.py`, `dev_test/phase_2_6_lifecycle.py`) pass on the deployed UI; see `dev_docs/execution_logs/sprint_2_6.md`.
- Cold start after scale-to-zero (measured): backend first request 31.7 s (create session) and 24.0 s (first `/run`), later requests 1-3 s; UI first page request 7.2 s. `ui_client` session calls therefore use a 90 s read timeout.
- Peak memory (99th percentile): backend 45%, UI 53% of 512 MiB. No `--memory=1Gi` change needed.
- Free-tier usage so far is tiny (about 3,000 billable UI seconds and 355 backend seconds against 180,000 vCPU-seconds).
- Cost check still to do by hand after a day of use: Console -> Billing -> Reports, grouped by service (Cloud Run, Cloud Build, Artifact Registry, Secret Manager). Budget `data-qa-demo-monthly`: $10, alerts at 50/90/100%.

# Redeployed for Sprint 3 (Phase 3.7, 2026-10-20)
- Backend `data-qa-api`: revision `data-qa-api-00009-25m` from `adk deploy`, then `00010` after removing the Vertex env vars. Later revisions (`00011`-`00013`) only added or changed the test env var `RESTART_MARK`, to force a new instance. Settings unchanged (1 CPU, 512Mi, max 1 instance).
- UI `data-qa-ui`: revision `data-qa-ui-00008-7gw`. The first Sprint 3 revision (`00007`) crashed at start because the staged context lacked `data_agent/sample_data/default/docs/`; `scripts/stage_ui_context.py` now stages those files.
- Session state keys added in Sprint 3: `docs` (list of `{name, b64}`; a missing key means the sample documents, `[]` means none). The backend also sets `docs_db_path`, `docs_summary`, `intent` and `intent_reason`. `load_error` now also lists skipped documents.
- Tool results: `functionResponse` events carry the tool output. The UI reads the classified intent from `actions.stateDelta.intent` and, if absent, from `GET` on the session (`state.intent`).
- Memory (99th percentile, Cloud Monitoring, 1.5 h covering the browser runs): backend 46%, UI 42% of 512 MiB. Below the 80% threshold, so no `--memory=1Gi`.
- `gcloud` from Git Bash: the Cloud SDK path contains a space, so call it as `cmd //c "gcloud ..."` (with `bin` on `PATH`), or put the command in a `.cmd` file when it has quoted arguments.
- Remove the `RESTART_MARK` test variable when convenient: `gcloud run services update data-qa-api --region us-central1 --remove-env-vars=RESTART_MARK`.
