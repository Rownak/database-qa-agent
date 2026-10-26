# Phase 2.1 — GCP Project and Prerequisites

Step-by-step setup of the Google Cloud project for the Sprint 2 Cloud Run deployment
(`data-qa-api` and `data-qa-ui`). See [sprint2.md](../sprints/sprint2.md) for the plan.

**End state:** a project with billing, the required APIs enabled, two least-privilege service
accounts, `GOOGLE_API_KEY` in Secret Manager, and a budget alert.

Steps marked **Console** are done in https://console.cloud.google.com. Steps marked **CLI** use
`gcloud`. Commands are written for PowerShell (the repo's primary shell on Windows).

---

## 0. Before you start

- A Google account with a payment method (a free-trial billing account works).
- A Gemini API key from Google AI Studio (https://aistudio.google.com/apikey). This is the
  same value as `GOOGLE_API_KEY` in your local `.env`.
- Chosen values (used throughout this document):

| Name | Value |
|---|---|
| Project ID | `<PROJECT_ID>` (globally unique, e.g. `data-qa-demo-123456`; cannot be changed later) |
| Region | `us-central1` (Tier 1 pricing, required for the free tier) |
| Backend service account | `data-qa-api-sa` |
| UI service account | `data-qa-ui-sa` |
| Secret name | `GOOGLE_API_KEY` |

---

## 1. Install and log in to the gcloud CLI

1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install (Windows installer).
2. Open a new PowerShell window and verify: `gcloud --version`
3. Log in (opens a browser): `gcloud auth login`
4. Also create Application Default Credentials (used by local tools and client libraries):
   `gcloud auth application-default login`

   It may print `Cannot find a quota project to add to ADC`. This is expected before a project
   exists; ignore it. After step 2, run
   `gcloud auth application-default set-quota-project <PROJECT_ID>`.

---

## 2. Create the project

### Console
1. Open the project picker (top bar) → **New Project**.
2. **Project name:** `data-qa-demo` (any name). Edit the **Project ID** to your `<PROJECT_ID>`.
3. **Organization / Location:** leave as "No organization" for a personal account.
4. Click **Create**, then select the new project in the picker.

### CLI (alternative)
```powershell
gcloud projects create <PROJECT_ID> --name="data-qa-demo"
```

### Point gcloud at it
```powershell
gcloud config set project <PROJECT_ID>
gcloud config set run/region us-central1
gcloud config list
```

---

## 3. Link a billing account

Cloud Run, Cloud Build and Artifact Registry will not work without billing.

### Console
1. **Navigation menu → Billing** → **Link a billing account** (or **Billing → Account management**).
2. Pick your billing account and confirm. The project must show "Billing is enabled".

### CLI (alternative)
```powershell
gcloud billing accounts list
gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
```

Verify: `gcloud billing projects describe <PROJECT_ID>` shows `billingEnabled: true`.

---

## 4. Enable the APIs

### CLI
```powershell
gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  iam.googleapis.com `
  cloudbilling.googleapis.com `
  billingbudgets.googleapis.com
```

`cloudbilling` and `billingbudgets` are only needed if you create the budget from the CLI (step 8);
drop them if you use the Console.

### Console (alternative)
**APIs & Services → Library**, search for each API above, click **Enable**.

Verify: `gcloud services list --enabled`

---

## 5. Create the service accounts

Each service runs as its own identity, so the UI cannot read the secret and the backend cannot
invoke itself.

### Console
**IAM & Admin → Service Accounts → Create service account**
1. Name `data-qa-api-sa`, description "Runtime identity for data-qa-api". **Create and continue**.
2. Grant no roles here (roles are added per resource below). **Done**.
3. Repeat for `data-qa-ui-sa` ("Runtime identity for data-qa-ui").

### CLI (alternative)
```powershell
gcloud iam service-accounts create data-qa-api-sa --display-name="data-qa-api runtime"
gcloud iam service-accounts create data-qa-ui-sa  --display-name="data-qa-ui runtime"
```

Resulting emails:
- `data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com`
- `data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com`

---

## 6. Grant build permissions to the default compute service account

ADK's source deploy (`adk deploy cloud_run`) builds with Cloud Build, which runs as the default
Compute Engine service account on current projects. It needs `roles/cloudbuild.builds.builder`.

```powershell
$PROJECT_ID = "<PROJECT_ID>"
$PROJECT_NUMBER = gcloud projects describe $PROJECT_ID --format="value(projectNumber)"

gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" `
  --role="roles/cloudbuild.builds.builder"
```

### Console (alternative)
**IAM & Admin → IAM → Grant access** → principal
`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com` → role **Cloud Build Service Account**.

`<PROJECT_NUMBER>` is the numeric ID Google assigns to the project (e.g. `123456789012`), not the
project ID you chose. Find it under **Dashboard → Project info** or **IAM & Admin → Settings**, or
copy the full email from **IAM & Admin → Service Accounts** ("Compute Engine default service
account").

> The compute service account is created when the Compute Engine or Cloud Run API is first
> enabled. If the command reports it does not exist, wait a minute after step 4 and retry.

### Your own user (the deployer)
If you are the project owner you already have enough rights. If not, you also need
`roles/run.admin`, `roles/cloudbuild.builds.editor`, `roles/artifactregistry.writer`,
`roles/storage.admin` (for the Cloud Build source bucket) and `roles/iam.serviceAccountUser`
on `data-qa-api-sa` and `data-qa-ui-sa`.

---

## 7. Store the API key in Secret Manager

### CLI
`printf` does not exist in PowerShell, and `echo` appends a newline that would corrupt the key.
Write the key to a temp file without a trailing newline, create the secret, then delete the file:

```powershell
$tmp = Join-Path $env:TEMP "gak.txt"
[IO.File]::WriteAllText($tmp, "<GOOGLE_API_KEY>")        # no trailing newline
gcloud secrets create GOOGLE_API_KEY --replication-policy=automatic --data-file=$tmp
Remove-Item $tmp
```

(In Git Bash: `printf '%s' '<GOOGLE_API_KEY>' | gcloud secrets create GOOGLE_API_KEY --data-file=-`)

### Console (alternative)
**Security → Secret Manager → Create secret** → Name `GOOGLE_API_KEY`, paste the key as the
secret value, leave other settings default → **Create secret**.

### Grant the backend access to this secret only
```powershell
gcloud secrets add-iam-policy-binding GOOGLE_API_KEY `
  --member="serviceAccount:data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com" `
  --role="roles/secretmanager.secretAccessor"
```

```powershell
gcloud secrets add-iam-policy-binding GOOGLE_API_KEY `
  --member="serviceAccount:data-qa-api-sa@gen-lang-client-0031790382.iam.gserviceaccount.com" `
  --role="roles/secretmanager.secretAccessor"
```

Console: open the secret → **Permissions** tab → **Grant access** → principal
`data-qa-api-sa@...` → role **Secret Manager Secret Accessor**.

Do **not** grant this role to `data-qa-ui-sa` or at project level.

---

## 8. Create a budget alert

A budget **notifies** you; it does **not** cap spending.

### Console (recommended)
1. **Billing → Budgets & alerts → Create budget**.
2. **Name:** `data-qa-demo-monthly`.
3. **Scope:** Projects → select `<PROJECT_ID>`; leave services as "All services".
4. **Amount:** Budget type "Specified amount", **Target amount** `$5` (monthly).
5. **Actions:** thresholds at **50%**, **90%** and **100%** of budget (Actual spend).
   Keep "Email alerts to billing admins and users" ticked.
6. **Finish**.

### CLI (alternative)
```powershell
gcloud billing budgets create `
  --billing-account=<BILLING_ACCOUNT_ID> `
  --display-name="data-qa-demo-monthly" `
  --budget-amount=5USD `
  --filter-projects="projects/<PROJECT_ID>" `
  --threshold-rule=percent=0.5 `
  --threshold-rule=percent=0.9 `
  --threshold-rule=percent=1.0
```

---

## 9. Verify the setup

```powershell
gcloud config list
gcloud services list --enabled --filter="name:(run OR cloudbuild OR artifactregistry OR secretmanager OR iam)"
gcloud iam service-accounts list
gcloud secrets list
gcloud secrets get-iam-policy GOOGLE_API_KEY
gcloud billing budgets list --billing-account=<BILLING_ACCOUNT_ID>
```

Checklist:

- [ ] `gcloud config list` shows the right project and `run/region = us-central1`
- [ ] Billing is enabled on the project
- [ ] The five APIs are enabled
- [ ] `data-qa-api-sa` and `data-qa-ui-sa` exist
- [ ] Compute service account has `roles/cloudbuild.builds.builder`
- [ ] Secret `GOOGLE_API_KEY` exists; only `data-qa-api-sa` can access it
- [ ] Budget with 50% / 90% / 100% alerts exists

---

## 10. Record the results

Copy the template below into `dev_docs/deploy/deploy.md` and fill it in. **Never put the API key
or any secret value in this file.**

```markdown
# Deployment settings
- Project ID: <PROJECT_ID>
- Project number: <PROJECT_NUMBER>
- Region: us-central1
- Backend SA: data-qa-api-sa@<PROJECT_ID>.iam.gserviceaccount.com
- UI SA: data-qa-ui-sa@<PROJECT_ID>.iam.gserviceaccount.com
- Secret: GOOGLE_API_KEY (Secret Manager, latest version)
- Budget: $5/month, alerts at 50/90/100%
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Billing must be enabled` when enabling APIs | Step 3 not done, or wrong project selected: `gcloud config get project`. |
| `PERMISSION_DENIED` on `add-iam-policy-binding` | Your account lacks Owner / IAM admin on the project. |
| Compute service account not found | Wait a minute after enabling `run.googleapis.com`, then retry step 6. |
| Secret value contains a trailing newline | Recreate the version: `gcloud secrets versions add GOOGLE_API_KEY --data-file=<file>` using the no-newline method in step 7. |
| `gcloud billing budgets` unknown command | Update gcloud (`gcloud components update`) or use the Console. |

## Cleanup (if you abandon the project)

```powershell
gcloud projects delete <PROJECT_ID>
```

Deleting the project removes all resources; it is recoverable for 30 days.
