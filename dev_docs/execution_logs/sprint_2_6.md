# Sprint 2 — Phase 2.6 Execution Log: End-to-End Verification, Cost Check, README

## Summary
The Sprint 1.7 acceptance checks pass on the deployed UI, two browser windows stay isolated, and the UI recovers when the backend instance is replaced. Verification found one real problem (a cold backend start could exceed the UI's 30 s session timeout), fixed and re-verified. The billing report after a day of use is the one item left for you.

Everything ran against the real services with a real Chrome (Playwright, driven from throwaway scripts; two reusable scripts were added to `dev_test/`).

## Results

| Check | Result |
|---|---|
| Valid uploads | `sales.csv` (sample): 50 rows × 5; `sales.xlsx`: 50 × 5; `multi_sheet.xlsx`: ignored sheet `summary` listed |
| Invalid uploads | `empty.csv`, `headerless.csv`, `numeric_header.csv`, `duplicate_header.csv`, `corrupt.xlsx` each show their specific error and keep the current data. `notes.txt` is rejected by the browser uploader itself ("text/plain files are not allowed") before the app sees it |
| Example questions | count = 50; average sales = 279.34 (equals pandas); top revenue category = Clothing (equals pandas) with a `GROUP BY` query in the expander; 5 sample rows; revenue statistics |
| Profit margin | says the needed columns do not exist, gives no number |
| "Delete all rows" | refused ("data is read-only"); asking for the count again still gives 50 |
| `Source:` line | present on every answer, including the refusal (`Source: none (no data queried)`) |
| End session | chat cleared and the sample session restored |
| Session isolation | window A uploads a 5-row CSV, window B a 12-row CSV: A answers 5 and `alpha0`, B answers 12 |
| Instance lifecycle | see below |
| Memory | backend peak 45%, UI 53% of 512 MiB (99th percentile, Cloud Monitoring). Below the 80% threshold, so **no `--memory=1Gi` change** |
| Logs | see below |

`dev_test/phase_2_6.py` runs 27 checks (all PASS in the final run).

### Instance lifecycle
Tab open with an uploaded 7-row file and one answered question; then the backend instance was replaced (a new revision, which empties its in-memory sessions). Asking again: the UI got a 404, recreated the session from the upload it holds, showed "The service restarted, so the chat was reset", and answered with the same data (`gamma0`) in 6.9 s. Only the retried question was in the chat. (`dev_test/phase_2_6_lifecycle.py`.)

### Cold start (after both services scaled to zero)
| Request | Time |
|---|---|
| Backend, first request after idle: create session | 31.7 s |
| Backend, first `/run` | 24.0 s (loads the agent); the second `/run` took 3.1 s |
| UI, first `GET /` | 7.2 s; warm 0.14 s |
| Browser: cold page load until the sample is ready (after the fix below) | 30.0 s, no error |
| Browser: first question after that | 4.8 s |

Both services logged `Starting new instance ... AUTOSCALING` at 15:33, confirming they had scaled to zero. If this is too slow for a demo, `--min-instances=1` on the backend removes most of it at a small idle cost (not applied).

### Logs
One query over both services for `data_agent.*`, `ui_client` and warnings shows: `data built from sample|upload, N rows, Xs`, `upload rejected (...)` (reproduced by creating a session directly with a bad upload), `tool error: no such column: ...`, `swept N stale session folder(s)`, and the UI's `backend session not found: POST /run` (from the lifecycle test). I scanned all 4,821 log entries since the first deploy for: the API key (and its first 12 characters), `AIza`, `Bearer`, `upload_b64`, and distinctive cells from my test files (`gamma0`, `alpha0`, `customer_`, the sample's first row). **No hits** (a `beta1` match was the `v1beta1` Vertex API path from the first failed deploy).

### Cost check (partly done)
- Budget `data-qa-demo-monthly`: $10, alerts at 50/90/100%, present.
- Usage since the first deploy (Cloud Monitoring): backend 355 billable seconds and 194 requests; UI 2,932 billable seconds and 3,519 requests, against free tiers of 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests. The UI figure is mostly open test tabs.
- Artifact Registry: 776 MB (free tier 0.5 GB, so a few cents a month at most). I deleted the two oldest UI images by hand (the cleanup policy from 2.5 also trims them); two UI images and one backend image remain.
- **Not done:** the billing report "after a day of testing". The Billing console cannot be read from this session, and a day had not passed. Check Console → Billing → Reports, grouped by service (Cloud Run, Cloud Build, Artifact Registry, Secret Manager) tomorrow. The sprint item is left unticked for that reason.

## Failure found and fixed
**Cold backend start exceeded the UI's session timeout.** The first backend request after idle took 31.7 s, but `ui_client` allowed session calls only 30 s, so a user opening the UI after idle could see "Could not start a session on the service".
- `ui_client.SESSION_TIMEOUT_S` 30 → 90 s (the backend's own request timeout is 120 s).
- `app.py` shows a spinner ("Connecting to the service (the first request after idle can take a minute)...") while a backend session is created.
- Test: session calls allow at least 60 s. Re-verified with a real cold start (30 s to sample ready, no error).

## Other changes
- **UI error logging** (`ui_client`): warnings for backend errors, 404s, unreachable backend and token failures, with only method, path and status (a test checks that no question or upload text is logged). Needed so UI errors appear in Cloud Logging at all.
- **README:** new short **Deploy to Cloud Run** section: what runs where, links to the commands (setup in `phase_2_1_gcp_setup.md`; deploy, checks and tear down in `deploy.md`), how to open the UI, behaviours to know (cold starts, backend restarts, stale-tab 400, local API server) and cost notes. The commands themselves live in those two docs, not the README.
- `dev_docs/deploy/deploy.md`: current revisions and the measurements above.
- UI redeployed twice (revisions 00005, 00006). The backend got two temporary env-var revisions for the lifecycle test; its config is back to the Phase 2.4 state (current revision `data-qa-api-00008-6dc`).

## Notes and limitations
- The scripts' check for the profit-margin answer is a keyword heuristic; it first failed on a correct answer and was widened. Answers come from an LLM, so wording varies between runs.
- Only Chrome was tested (Playwright with the installed Chrome). I did not open the UI on a phone or in other browsers.
- The UI is public; anyone with the link can spend Gemini quota.
- The ID-token path is now verified in production (every question above went through it).

## Files
- Created: `dev_test/phase_2_6.py`, `dev_test/phase_2_6_lifecycle.py`, this log
- Modified: `ui_client.py` (logging, 90 s session timeout), `app.py` (connecting spinner), `tests/test_ui_client.py` (+2 tests), `README.md`, `dev_docs/deploy/deploy.md`, `dev_docs/sprints/sprint2.md` (Phase 2.6 ticked except the cost item)

## Tests and commands
```
cd spreadsheet-qa
uv run pytest                                   # 77 passed
# browser checks (need gcloud for the lifecycle script, and a local Chrome)
$env:UI_URL = "https://data-qa-ui-701944778941.us-central1.run.app"
uv run --no-project --with playwright --with pandas python -m dev_test.phase_2_6
$env:GCLOUD = "gcloud"
uv run --no-project --with playwright python -m dev_test.phase_2_6_lifecycle
```
