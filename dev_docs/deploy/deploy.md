# Deployment settings
- Project ID: gen-lang-client-0031790382
- Project number: 701944778941
- Region: us-central1
- Backend SA: data-qa-api-sa@gen-lang-client-0031790382.iam.gserviceaccount.com
- UI SA: data-qa-ui-sa@gen-lang-client-0031790382.iam.gserviceaccount.com
- Secret: GOOGLE_API_KEY (Secret Manager, latest version)
- Budget: $10/month, alerts at 50/90/100%

# ADK API server shape (verified locally in Phase 2.2)
App name is the agent folder: `data_agent`. Interactive docs at `/docs`.
- Create session: `POST /apps/data_agent/users/{user}/sessions/{session}`. The JSON body **is** the initial state, e.g. `{"upload_name": "x.csv", "upload_b64": "<base64>"}` (do not wrap it in `"state"`). Returns the session; 200.
- Run: `POST /run` with `{"app_name": "data_agent", "user_id": "...", "session_id": "...", "new_message": {"role": "user", "parts": [{"text": "..."}]}}`. Returns a JSON **list** of events. Tool calls are `content.parts[].functionCall` (`name`, `args`); tool results are `functionResponse`; the answer is the last event's `content.parts[].text` (ends with a `Source:` line).
- Delete session: `DELETE /apps/data_agent/users/{user}/sessions/{session}`.
- An unknown session on `/run` returns 404 (used by the UI to detect a lost session).
- Optional state keys: `upload_name`, `upload_b64` (backend builds `data.db` from them). The backend sets `db_path`, `column_map`, `schema_summary`, and `load_error` when the upload was rejected.
- The generated Dockerfile uses `python:3.11-slim` and installs `google-adk` itself, so `data_agent/requirements.txt` lists only pandas, openpyxl and python-dotenv.

# Backend deployed (Phase 2.4, 2026-10-19)
- Service: `data-qa-api`, region us-central1, private (`--no-allow-unauthenticated`), revision `data-qa-api-00002-mmf` serves 100%.
- URL (use this one as `API_URL` and as the ID-token audience): https://data-qa-api-701944778941.us-central1.run.app
  (the console also shows https://data-qa-api-6b3r2whd2q-uc.a.run.app; both work)
- Settings: 1 CPU, 512Mi, min 0, max 1, concurrency 10, timeout 120s, runs as `data-qa-api-sa`, `GOOGLE_API_KEY` mounted from Secret Manager (`latest`).
- Artifact Registry repo `cloud-run-source-deploy` (us-central1) was created by the first deploy.
- **After every `adk deploy cloud_run`, run:**
  `gcloud run services update data-qa-api --region us-central1 --remove-env-vars=GOOGLE_GENAI_USE_ENTERPRISE,GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION`
  ADK sets these, which switches google-genai to Vertex AI (not enabled here) and `/run` returns 500 (`Agent Platform API has not been used`).
- gcloud on this machine is not on the Git Bash PATH: `export PATH="$PATH:/c/Users/rowna/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"`.
