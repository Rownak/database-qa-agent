# Sprint 6 — Phase 6.3 Execution Log: Redeployment and Hosted Smoke Test

## Summary
The Sprint 6 build (up to three data files per session) is live on Cloud Run. Both services were redeployed with the existing workflow, and the full browser check from Phase 6.2 passed unchanged against the hosted UI (25/25). No application code changed in this phase.

| Service | Revision now | Previous (rollback) |
|---|---|---|
| `data-qa-api` (private) | `data-qa-api-00015-7j6` | `data-qa-api-00013-fj4` |
| `data-qa-ui` (public) | `data-qa-ui-00009-qp9` | `data-qa-ui-00008-7gw` |

UI: <https://data-qa-ui-701944778941.us-central1.run.app>. API: <https://data-qa-api-701944778941.us-central1.run.app>.

## What was done
1. **Before deploying:** recorded the live revisions (above), ran `uv run python scripts/stage_ui_context.py`, and checked the staged folder. It contains the new `data_agent/ingest.py` and needs no extra files (the UI imports are unchanged: `ingest`, `sessions`, `doc_ingest`, `config`, `events`, `logs`).
2. **Backend:** `adk deploy cloud_run` with the command in `deploy.md` (project `gen-lang-client-0031790382`, `--max-instances=1`, `--memory=512Mi`, secret `GOOGLE_API_KEY`) created revision `00014-hcv`. Then the required `gcloud run services update … --remove-env-vars=GOOGLE_GENAI_USE_ENTERPRISE,GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION` created `00015-7j6`. `GET /list-apps` with an identity token returned `["data_agent"]`; without a token it returns 403.
3. **UI:** `gcloud run deploy data-qa-ui --source .ui_build …` with the documented flags (`--max-instances=1`, `--session-affinity`, `API_URL` set) created `00009-qp9`. The `run.invoker` binding for the UI service account already existed, so that step was skipped.
4. **Docs:** added "Redeployed for Sprint 6" to `dev_docs/deploy/deploy.md` (revisions, rollback revisions, the `uploads` key and the warning that old-UI sessions do not work on the new backend) and updated its "Optional state keys" line.

## Hosted smoke test
`UI_URL=https://data-qa-ui-701944778941.us-central1.run.app uv run --no-project --with playwright python -m dev_test.phase_6_2_browser`: **ALL CHECKS PASSED** (25 checks, real Gemini model, cold start included).
- sample data and documents load; one file replaces the sample and a question returns 50 with `FROM "sales"`
- three files (`sales`, `customers`, `orders`); the cross-table question returns Grace Hopper / 499.99 and the expander shows the JOIN
- a fourth file is rejected with the limit message; replacing `orders.csv` by name at the limit works
- `notes.txt`, `empty.csv`, `headerless.csv`, `numeric_header.csv`, `duplicate_header.csv`, `corrupt.xlsx` and a file over 500 KB each show their error and keep the tables
- `sales.csv` + `sales.xlsx` give `sales` and `sales_2`; `Sales.CSV` replaces `sales.csv` in place
- `multi_sheet.xlsx` shows the ignored-sheet warning
- removing one file keeps the other; removing all returns the sample; End session works
- a document is removed, one added, one replaced by name; the document list updates right away (the Phase 6.2 fix works on the hosted app); a policy question is answered with a `shipment_delay_policy` citation while a data file is loaded

`uv run python -m dev_test.phase_6_3` (identity token from `gcloud`): `/list-apps` returned `['data_agent']`; a session with `customers.csv` and `orders.csv` in `uploads`; the agent ran one `run_sql` call with a JOIN and answered "Grace Hopper … $499.99"; the session was deleted (200).

## Logs and memory (last 60 minutes, covering the smoke run)
- **Errors:** 0 entries with severity ERROR or higher on `data-qa-api` and `data-qa-ui`.
- **Warnings (backend), 2:** one "request was not authenticated" (my own `curl` without a token, which should return 403), and one "no available instance" while the new revision was starting (`--max-instances=1` leaves no spare instance during the switch; it happened once, right after the env-var update, and the next request worked).
- **Backend log:** `data built from upload, 3 table(s), 61 rows` and two single-table builds, so the `uploads` key reaches the backend and all tables are built.
- **Memory (Cloud Monitoring, p99, 512 MiB):** backend 44.9%, UI 40.0%. Below the 80% threshold, so no `--memory=1Gi`.

## Decisions and notes
- **Deployed from the working tree.** The staged UI folder and the ADK source folder came from the uncommitted Sprint 6 changes, so the deployed revisions match the tested code but are not yet tied to a commit. Commit the Sprint 6 changes so the revisions can be traced.
- **Old browser tabs:** a tab opened before the deploy keeps a stale Streamlit session and a backend session in the old format. Reloading the page fixes it (already documented in `deploy.md`).
- **Rollback:** `gcloud run services update-traffic data-qa-api --region us-central1 --to-revisions=data-qa-api-00013-fj4=100` (and the UI the same way with `data-qa-ui-00008-7gw`). Roll both back together, because the session state format differs.
- `gcloud` quoting from Git Bash is fragile (as `deploy.md` notes); the log queries were run from a small Python script that calls `gcloud.cmd` with an argument list.

## Files
- Modified: `dev_docs/deploy/deploy.md`, `dev_docs/sprints/sprint6.md`
- Created: `db-doc-qa/dev_test/phase_6_3.py`, `dev_docs/execution_logs/sprint_6_3.md`

## Commands
```
cd db-doc-qa
uv run python scripts/stage_ui_context.py
uv run adk deploy cloud_run … data_agent …            # see deploy.md, then remove the Vertex env vars
gcloud run deploy data-qa-ui --source .ui_build …     # see deploy.md
UI_URL=https://data-qa-ui-701944778941.us-central1.run.app uv run --no-project --with playwright python -m dev_test.phase_6_2_browser
uv run python -m dev_test.phase_6_3                   # hosted API round trip
```

## Limitations
- The smoke test is one run with the real model; answer wording varies, so the checks look for values and table names.
- The UI has no end-user access control and sessions are in memory (unchanged from earlier sprints).
