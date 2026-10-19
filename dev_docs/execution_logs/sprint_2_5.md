# Sprint 2 — Phase 2.5 Execution Log: Deploy the UI (`data-qa-ui`)

## What was done
- **Invoker grant:** `gcloud run services add-iam-policy-binding data-qa-api ... --member=serviceAccount:data-qa-ui-sa@... --role=roles/run.invoker`. The backend's IAM policy now lists only that account, so the backend stays private.
- **Build context:** `gcloud run deploy --source` only uses a file named `Dockerfile` and has no flag for another name, so I added `scripts/stage_ui_context.py`. It copies exactly the files `Dockerfile.ui` copies into `.ui_build/` (git-ignored) and names the Dockerfile `Dockerfile`. `tests/test_stage_ui_context.py` checks that the staged folder holds every file `Dockerfile.ui` copies and nothing else (no tests, no `.env`, no `agent.py`).
- **Deploy** (Cloud Build built the image, since Docker is not running locally): `gcloud run deploy data-qa-ui --source .ui_build --region=us-central1 --allow-unauthenticated --service-account=data-qa-ui-sa@... --set-env-vars=API_URL=<data-qa-api URL> --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --session-affinity --timeout=3600`.
- **UI URL:** https://data-qa-ui-701944778941.us-central1.run.app (revision `data-qa-ui-00002-r8m`).
- **Artifact Registry cleanup:** a cleanup policy on `cloud-run-source-deploy` (dry run off) deletes all versions except the 2 most recent per image. At that point it held one backend image and two UI images, so nothing was removed.

## Bug found during verification (and fixed)
**End session did not return to the sample.** In the deployed UI, after uploading a file and clicking End session, the page still showed the uploaded file. The same happens in local mode, so it dates from Sprint 1: Streamlit's file uploader keeps its file across `st.session_state.clear()`, so the fresh session immediately loaded it again (reproduced with `AppTest`).
- **Fix** (`app.py`): `_end_agent_session` now also clears the state and bumps `uploader_generation`, which is part of the uploader's `key`. Idle expiry uses the same path.
- **Tests:** one local and one API-mode `AppTest` (upload, End session, expect the sample).
- The UI was redeployed with the fix (revision 00002).

## Verification
- **Config** (`gcloud run services describe data-qa-ui`): service account `data-qa-ui-sa@...`, `API_URL` set, CPU 1, 512Mi, max 1, session affinity on, timeout 3600 s, `allUsers` invoker. Page `GET /` → 200, `/_stcore/health` → `ok`.
- **Browser check** with Playwright driving Chrome against the public URL (a throwaway script outside the repo):
  1. The page loads with `sales.csv (sample)` (first load after a new instance ≈ 6 s).
  2. "How many records are there?" → `There are 50 records in the table. Source: get_table_info` with the "How this was answered" expander.
  3. Uploading `duplicate_header.csv` shows `Duplicate column name` and leaves the data and chat unchanged.
  4. Uploading `multi_sheet.xlsx` replaces the data, clears the chat and "What is the total revenue?" returns `$10,604.82` with its SQL `Source:`.
  5. End session returns to the sample with an empty chat.
- Steps 2 and 4 prove the UI service account's ID token is accepted by the private backend (the Phase 2.3 token path now works in production). The backend log showed `data built from sample` and `from upload` for these sessions; no warnings in the UI logs.

## Upload "400" after a redeploy (investigated, not a code bug)
You reported `Request failed with status code 400` when uploading `WA_Fn-UseC_-HR-Employee-Attrition.csv` (227,977 bytes) on the deployed UI, which works locally.
- The Cloud Run log showed `PUT /_stcore/upload_file/<session>/<file>` returning 400 in about 2 ms, three times for the same session id. Streamlit answers 400 immediately ("Invalid session_id") when the upload's session is not an active websocket session on that server. That is unrelated to file size or content.
- The same file, and generated CSVs of 20, 60, 120 and 222 KB, upload fine (HTTP 204) from a freshly loaded page.
- Reproduced: open the page, replace the service instance (I added then removed a harmless env var, creating revisions 00003 and 00004), then upload from the already-open tab: `PUT` 400, no banner, and the page shows a fresh sample session. A tab kept open across my redeploy (or any instance replacement) is stale.
- **Fix for the user:** reload the page and upload again. Nothing was changed in the code. Possible follow-up: a caption under the uploader telling users to reload if an upload fails with a 400.
- The service config is back to the deployed one (only `API_URL` set); the live revision is `data-qa-ui-00004-jlz`.

## Notes and limitations
- The UI is public by design (`--allow-unauthenticated`): anyone with the URL can use it and spend Gemini quota. The budget alert only notifies; `--max-instances=1` limits scale. Consider closing it when not demoing.
- The UI image build was done by Cloud Build; a local `docker build` was still not possible (daemon not running).
- The lost-session path (backend instance replaced, UI recreates the session) was covered by unit tests but not yet exercised live; that is part of Phase 2.6's instance lifecycle check.
- `.ui_build/` is generated; re-run `scripts/stage_ui_context.py` before each UI deploy.

## Files
- Created: `scripts/stage_ui_context.py`, `tests/test_stage_ui_context.py`, this log
- Modified: `app.py` (End session fix, uploader key), `tests/test_app.py`, `tests/test_app_api.py`, `.gitignore` (`.ui_build/`), `dev_docs/deploy/deploy.md`, `dev_docs/sprints/sprint2.md` (Phase 2.5 ticked)

## Tests and commands
```
cd spreadsheet-qa
uv run pytest                                  # 75 passed (70 before this phase)
uv run python scripts/stage_ui_context.py      # then the gcloud run deploy command above
uv run python -m dev_test.phase_2_5            # UI 200 + health ok, API 403 without a token
```
