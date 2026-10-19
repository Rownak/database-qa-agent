# Sprint 2 — Phase 2.4 Execution Log: Deploy the Backend (`data-qa-api`)

## What was done
Deployed the ADK API server to Cloud Run from `spreadsheet-qa/` with the command from the sprint plan (`adk deploy cloud_run ... data_agent -- --no-allow-unauthenticated --service-account=... --set-secrets=GOOGLE_API_KEY=GOOGLE_API_KEY:latest --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --concurrency=10 --timeout=120`, no `--with_ui`). The first deploy created the Artifact Registry repo `cloud-run-source-deploy` and built the image with Cloud Build. The deploy needed no code changes.

- **URL:** https://data-qa-api-701944778941.us-central1.run.app (the console also lists https://data-qa-api-6b3r2whd2q-uc.a.run.app; both work).
- **Revision:** `data-qa-api-00002-mmf` (00001 was the initial deploy; 00002 is the env-var fix below).

## Problem found and fixed
The first revision answered `/run` with HTTP 500. Cloud Logging showed `403 PERMISSION_DENIED ... Agent Platform API has not been used in project ...`. `adk deploy` had set `GOOGLE_GENAI_USE_ENTERPRISE=1`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` on the service, which switches google-genai from the Gemini API key to Vertex AI. Fix (a flag after `--` was not enough, so the sprint's fallback was used):

```
gcloud run services update data-qa-api --region us-central1 \
  --remove-env-vars=GOOGLE_GENAI_USE_ENTERPRISE,GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION
```
This must be repeated after every `adk deploy`; it is recorded in `dev_docs/deploy/deploy.md`.

## Verification
- **Configuration** (`gcloud run services describe`): service account `data-qa-api-sa@...`, `GOOGLE_API_KEY` from Secret Manager (`latest`), CPU 1, memory 512Mi, `maxScale` 1, no `minScale` annotation (default 0), concurrency 10, timeout 120 s. The only env var left is the secret.
- **Auth:** `GET /list-apps` without a token → `403`; with `gcloud auth print-identity-token` → `["data_agent"]`.
- **Questions** (user token):
  - Sample session: "How many records are there?" → `There are 50 records in the table. Source: get_table_info` (the expected 50).
  - Upload session (`multi_sheet.xlsx` as `upload_b64`): revenue answered; log `data built from upload, 50 rows`.
  - Four more questions (average, top category, sample rows, "Delete all rows") all returned HTTP 200 in 0.7–2.0 s.
  - A `/run` for an unknown session → `404` (the UI relies on this).
- **Logs:** startup lines (`Uvicorn running`, `STARTUP TCP probe succeeded`), `Found root_agent in data_agent.agent`, `No .env file found for data_agent` (confirms no `.env` was shipped), and `data_agent.agent: session ...: data built from sample|upload, 50 rows, ...s`.
- **Memory:** the Cloud Monitoring `memory/utilizations` 99th percentile was about **0.39** (39% of 512 MiB) after those questions, below the 80% threshold, so no `--memory=1Gi` change.
- **Latency (first look, to be refined in 2.6):** the first `/run` after a new instance took about 10 s (instance start plus agent load); warm requests 1–4 s.
- Smoke-test sessions were deleted afterwards.

## Notes and limitations
- Revision 00001 (the one with the broken env) is still listed; it receives no traffic.
- The `adk deploy` command asked to create the Artifact Registry repo (answered with the default Y). Source-deploy images accumulate there; Phase 2.5 covers cleanup.
- `gcloud` is not on the Git Bash PATH here; I appended its `bin` folder to `PATH` for each command (see `deploy.md`).
- The ID-token path of `ui_client` (service-account token) is still untested; this phase used a user token. Phase 2.5 grants `data-qa-ui-sa` the invoker role and tests it.
- This deploy costs money only per the free-tier notes in `sprint2.md`; the $10 budget alert from 2.1 is active.

## Files
- Modified: `dev_docs/deploy/deploy.md` (service URL, settings, the env-var fix), `dev_docs/sprints/sprint2.md` (Phase 2.4 ticked)
- Created: this log. No source code changed.

## Commands to re-run the checks
```
export PATH="$PATH:/c/Users/rowna/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"   # Git Bash only
gcloud run services describe data-qa-api --region us-central1
API=https://data-qa-api-701944778941.us-central1.run.app
TOK=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOK" $API/list-apps        # ["data_agent"]; without the header: 403
```
