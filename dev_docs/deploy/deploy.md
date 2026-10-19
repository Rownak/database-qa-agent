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
