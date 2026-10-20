# Database & Document Q&A Agent — Project Description (v2)

This version evolves the deployed **Database Q&A demo** (release v0.1.1, see [project_description_v1.md](project_description_v1.md) and [design_v1.md](design_v1.md)) into a **database-and-document Q&A demo** built with Google ADK. A user asks questions in plain language. The agent answers from a spreadsheet (via read-only SQL), from uploaded documents (via hybrid retrieval), or from both. It keeps database facts and document guidance apart and cites its sources.

Labels used below: **[Existing]** works today and must keep working; **[Changed]** is existing behaviour that this version adjusts; **[New]** is a proposed addition.

---

## 1. Overview and Objectives

**Goal:** add document retrieval to the working database agent while keeping the system small: **one orchestrator agent, two tools the agent can call**.

Objectives:

- Answer three kinds of question: document questions, database questions, and hybrid questions that need both.
- Learn hybrid retrieval end to end: chunking, BM25, dense embeddings, metadata filters and rank fusion, all inside the current session.
- Let the agent decide which tool to call, and in what order, without a planner, a router or sub-agents.
- Produce grounded answers that keep **database facts**, **document guidance** and **gaps or conflicts** apart, and cite every document claim.
- Keep the current guarantees: read-only SQL, per-session isolation, cleanup, and the Cloud Run deployment.

## 2. Current Baseline [Existing]

| Area | Current behaviour (v0.1.1) |
|---|---|
| Agent | One ADK `Agent` (`data_analyst`, `gemini-3.5-flash-lite`). The table schema is placed in the instruction. A `before_agent_callback` (`ensure_data`) builds the session database. |
| Tools | `get_table_info`, `get_sample_rows`, `get_column_stats`, `run_sql` (one `SELECT`/`WITH` statement on a `mode=ro` connection, 100-row limit, 5 s time limit). |
| Inputs | One `.csv`/`.xlsx` file (≤ 500 KB, first sheet only, strict header checks), loaded as table `data`. Every session starts with `sales.csv`. |
| Deployment | Streamlit UI (`data-qa-ui`) calls the private ADK API server (`data-qa-api`) with an ID token. The upload is sent as base64 in the ADK session state. The backend rebuilds `/tmp/adk_sheet_qa/<session_id>/data.db` whenever the file is missing. There is also a local in-process mode. |
| Lifecycle | A valid upload creates a new ADK session and resets the chat. An invalid upload changes nothing. End session, a 30-minute idle sweep, and a purge at startup clean up. On a 404 the UI recreates the session. |
| Answers | Every answer ends with a `Source:` line. The UI shows tool calls under **How this was answered**. |

## 3. MVP Scope [New]

**Supported inputs per session**

| Slot | Formats | Limit | Behaviour |
|---|---|---|---|
| Table | `.csv`, `.xlsx` | 1 file, ≤ 500 KB | Unchanged from v1: a valid upload replaces the current table. |
| Documents | `.pdf` (text-based), `.txt` (UTF-8) | Up to 3 files, each ≤ 1 MB and ≤ 20 pages | Added one at a time. A file with the same name replaces the existing document. Each document can be removed on its own. |

**In scope**

- Two separate uploaders in the UI: **Table** and **Documents**. A `.txt` file is a document, never a table.
- Document ingestion: extract text with page numbers, split it into chunks, store chunks and metadata in SQLite, embed the chunks, and index them for BM25 and dense search.
- A document retrieval tool with hybrid search, simple metadata filters and rank fusion. It returns passages with citation details.
- An orchestrator instruction that covers routing, sequencing for hybrid questions, answer structure, citations, and how to report missing or conflicting information.
- Default sample data for the shipment scenario (§8), so all three question types work as soon as a session starts.

**Out of scope for this MVP**

- Scanned PDFs or OCR, `.docx`/`.md`/`.html` files, tables inside PDFs, and images.
- More than one table, joins across files, and SQL over document content.
- Persistent storage or indexes that survive the session, or sharing documents across sessions.
- Rerankers, query rewriting, sub-agents, and planning or critic loops.
- Authentication, and any new infrastructure beyond the two current Cloud Run services.

## 4. User Workflow and Routing

**Workflow**

1. **[Changed]** A new session starts with the sample shipments table and the sample policy documents loaded.
2. **[Existing]** The user can replace the table with their own CSV or XLSX file.
3. **[New]** The user can add, replace or remove documents (up to 3). The UI validates each file and shows its name, page count and chunk count. Any accepted change to the table or to the documents starts a fresh ADK session holding all current files, and the chat resets (the same rule as uploads today). A rejected file changes nothing.
4. The user asks a question. The agent picks a tool, or calls both in sequence, and then answers.
5. **[Existing]** End session, the idle timeout, or an app restart deletes everything belonging to the session.

**Routing (the agent decides from its instruction; there is no separate router)**

| Question | Type | Expected tool use |
|---|---|---|
| "What is the policy for delayed shipments?" | Document | `search_documents` only |
| "Where is shipment 12345?" | Database | `run_sql` only |
| "How many express shipments are delayed?" | Database | `run_sql` only |
| "Shipment 12345 is delayed. What should I do?" | Hybrid | 1) `run_sql` gets the record (type, status, delay, dates). 2) `search_documents` looks up the delay policy for **that shipment type**, using the type as a filter or query term. 3) The agent compares the record with the policy. |
| "Which delayed shipments have passed their escalation threshold?" | Hybrid | 1) `search_documents` gets the thresholds. 2) `run_sql` filters records using those thresholds. |

**Answer format [Changed]**

- **From the database:** the facts, together with the SQL used.
- **From the documents:** the guidance, with an inline citation for each claim, such as `[shipment_delay_policy.pdf, §3.2 Express Shipments, p. 2]`.
- **Gaps or conflicts:** shown only when they apply. Examples: the record was not found, no policy applies, the documents disagree, or the user's claim contradicts the record.
- **Sources:** a final line listing the SQL and the cited passages. This replaces today's single `Source:` line and keeps the same rule ("none" if no data was used).

## 5. Minimal Components

| Component | Status | Responsibility |
|---|---|---|
| Streamlit UI (`app.py`) | Changed | Adds a document uploader and a list of loaded documents with Remove buttons. Sends the documents in the session state alongside the table. The expander also shows retrieval calls and the passages returned. |
| UI client (`ui_client.py`) | Existing | Creates, runs and deletes sessions through the API. Parses the new tool's events in the same way as today. |
| Table ingestion (`ingest.py`) | Existing | Validates and loads one table into `data.db`. |
| Document ingestion (new module) | New | Validates PDF/TXT files, extracts text page by page, splits it into chunks, attaches metadata, writes `docs.db`, and requests embeddings. |
| Retrieval (new module) | New | Runs BM25 search (SQLite FTS5) and dense search over stored vectors, applies filters, fuses the rankings (RRF), and formats the passages. |
| Agent (`agent.py`) | Changed | Same single agent with a new name and instruction (routing, sequencing, citation and conflict rules, uploaded content treated as data). The table schema and a list of documents are placed in the instruction. `ensure_data` builds both `data.db` and `docs.db`. |
| Tools (`tools.py`) | Changed | Two agent-facing tools: `run_sql` (unchanged) and `search_documents` (new). |
| Sessions (`sessions.py`) | Existing | Session folders, touch, sweep and purge. Their scope grows to include `docs.db`. |

> **Decision: two agent-facing tools.** The brief asks for exactly two tools, so `get_table_info`, `get_sample_rows` and `get_column_stats` come off the agent's tool list. Counts, samples and statistics are still possible through `run_sql`, and the schema is already in the instruction. The functions stay in the code for the UI preview and the tests. *Trade-off:* the agent now computes the median with SQL rather than calling a dedicated tool. If you would rather keep all four database tools (six tools in total), this is the one decision to reverse.

> **Decision: SQLite for vectors, not FAISS.** A session holds at most 3 small documents (a few hundred chunks). Exact cosine search with numpy over vectors stored as BLOBs in `docs.db` takes milliseconds. It adds no dependency and no second index file to keep in sync, and the whole thing is rebuilt along with the rest of the session. FAISS is in the backlog for larger corpora.

> **Decision: BM25 through SQLite FTS5.** FTS5 has built-in `bm25()` ranking, so keyword search lives in the same `docs.db` and needs no extra library.

> **Decision: hosted embeddings.** Use the Gemini embedding model through the `google-genai` client that ADK already installs. A local embedding model would not fit in the backend's 512 MiB.

> **Decision: documents in a separate database.** `run_sql` can reach only `data.db`, so the agent cannot read document chunks through SQL. All retrieval goes through `search_documents`, which keeps citations consistent.

## 6. Retrieval Requirements [New]

**Chunking: structure-aware**

Policy documents are hierarchical (title → section → sub-section → clause), so chunks follow that structure. This keeps each rule and its conditions together, instead of cutting text into fixed-size windows.

- **Sections first:** the text is split at headings that rule-based checks find: numbered headings (`3`, `3.2`), clause markers (`(a)`), `#` lines, and lines that look like titles. Each section records its full path, for example `3 Escalation > 3.2 Express Shipments`.
- **Then [`semchunk`](https://github.com/isaacus-dev/semchunk):** each section is split into chunks of about 200 tokens. semchunk splits at the most meaningful boundary first (paragraphs, then sentences, then words), so a section that fits stays as one chunk. A chunk never crosses into another section.
- **Context:** the section path is added to the start of each chunk before it is indexed, so a clause can be found by searching for its parent heading. The stored `text` keeps the original wording.
- **Fallback:** a document with no detectable headings is chunked by semchunk as a whole, with a small overlap.

**Stored metadata per chunk**

| Field | Purpose |
|---|---|
| `chunk_id`, `doc_id`, `chunk_index` | A stable reference, and ordering for merging neighbouring chunks |
| `source` (original filename), `doc_type` (pdf/txt), `doc_title` | Citation and filtering |
| `section_path` (for example `3 Escalation > 3.2 Express Shipments`), `section_number` (`3.2`, `3.2(b)`), `level` | Citation, filtering and context for the agent |
| `parent_section` | Lets the tool show which parent section a clause belongs to |
| `page_start`, `page_end` (PDF only) | Citing the page or page range |
| `chunk_method` (`structure` or `fallback`) | Checking chunking quality and debugging |
| `char_start`, `char_end` | Highlighting the passage in the UI and checking citations |
| `text` | The content searched by BM25 and returned to the agent |
| `embedding` | Dense search |

**`search_documents` contract**

- **Inputs:**
  - `query`, which is required.
  - Optional filters: `source` (one filename) and `section` (a case-insensitive substring of `section_path`, for example `"express"`). Because the filter matches the full path, it also returns every sub-section and clause under a matching heading.
  - `top_k`: default 5, maximum 8.
- **Search:**
  - Run BM25 and dense search over the filtered chunks, taking the top 20 from each.
  - Fuse the two lists with Reciprocal Rank Fusion (k = 60).
  - Drop dense-only hits that fall below a minimum similarity.
- **Output:** a list of passages, each with `chunk_id`, `source`, `page_start`/`page_end`, `section_path`, `section_number`, `text` (cut to about 1,200 characters), `score`, and `matched_by` (`bm25`, `dense` or `both`). The tool also returns `status`, plus a `note` when there are no matches, when a filter excluded everything, or when it fell back to BM25 only.
- **Failures:** the tool never raises an exception. If the embedding call fails, it returns BM25-only results with a note. If no documents are loaded, it returns an error saying so.

**Citations:** each document claim in the answer cites `source`, the section number and title, and the page or page range (for PDFs), for example `[shipment_delay_policy.pdf, §3.2(b) Express Shipments, p. 2]`. The agent cites only passages the tool returned in this turn.

**Uploaded content is data, not instructions [New]:**

- Passages and table values are returned inside clearly marked tool output.
- The instruction tells the agent to ignore any instructions that appear inside passages or cells.
- A document cannot change which tools the agent calls, and cannot widen SQL access.

## 7. Session Lifecycle and Error Handling

**Lifecycle**

- **[Existing]** Each session has its own folder, `/tmp/adk_sheet_qa/<session_id>/`. It holds `data.db` and, **[New]**, `docs.db`.
- **[Existing]** Tools find these paths only through the session state, never through agent input.
- **[Changed]** The ADK session state holds the table upload plus up to 3 documents, each as `{name, b64}`. The total stays under about 5 MB.
- **[Changed]** `ensure_data` rebuilds whichever database is missing. This happens on the first question and after a Cloud Run instance restart. Rebuilding `docs.db` embeds the documents again, which adds a few seconds and some API calls; this is acceptable for a demo.
- **[Existing]** The cleanup rules still apply: End session, the 30-minute idle sweep, the purge at startup, and recreating the session after a 404. They now cover both database files.

**Document validation (in the UI before acceptance; repeated on the backend)**

| Case | Message, behaviour |
|---|---|
| Wrong extension | "Unsupported document type. Upload a .pdf or .txt file." |
| More than 1 MB or more than 20 pages | "Document too large (limit 1 MB / 20 pages)." |
| A fourth document | "Up to 3 documents per session. Remove one first." |
| Encrypted or corrupt PDF, or non-UTF-8 text | "Could not read the document: \<reason\>." |
| No extractable text (for example a scanned PDF) or an empty file | "No readable text found in \<name\>." |
| Same filename as a loaded document | The new file replaces the old one, with a notice. |

Any rejection leaves the current files, the session and the chat unchanged.

**Runtime errors**

- If a document fails while the backend is building `docs.db`, it is skipped, and `load_error` names it so the agent can tell the user. The other documents stay usable.
- **[Existing]** SQL errors are returned to the agent, which retries once. Write attempts are refused by the read-only connection.
- If the embedding service is unavailable, retrieval runs on BM25 only and the answer says so.

## 8. Sample Data Updates [New]

The current `sales.csv` cannot demonstrate hybrid questions. It stays in the repository as an upload and regression fixture, and a shipment scenario becomes the default.

**`data_agent/sample_data/shipments.csv`** (about 60 rows)

- **Columns:** `shipment_id`, `shipment_type` (Standard, Express, International, Refrigerated), `status` (In Transit, Delayed, Customs Hold, Delivered, Exception), `origin`, `destination`, `current_location`, `ship_date`, `expected_delivery_date`, `last_update_date`, `delivered_date`, `delay_days`.
- **Seeded records for the demo cases:**
  - **12345:** Express, Delayed, `delay_days` = 3. This is past the Express escalation threshold.
  - **12346:** Standard, Delayed, `delay_days` = 1. This is within the grace period, so the answer is to monitor and notify.
  - **12347:** International, Customs Hold.
  - **12348:** In Transit and on schedule. This tests a user claim that the record contradicts.
  - **12349:** Refrigerated, Delayed. No policy covers this type, which tests a gap in the documents.
  - **99999** does not exist in the data. This tests a record that is not found.

**Sample documents** (short, written for the demo). Each one uses a numbered hierarchy (sections such as `3`, sub-sections such as `3.2`, clauses such as `(a)`) so the structure-aware chunking can be demonstrated. At least one section crosses a page break.

- `shipment_delay_policy.pdf`:
  - definitions of a delay
  - a grace period and an escalation threshold for each shipment type (no Refrigerated section)
  - escalation tiers and contacts
- `customer_communication_guidelines.pdf`: when to notify customers, what customers can do (reschedule, cancel, claim), and compensation rules.
- `international_shipping_procedures.txt`:
  - customs holds, required documents, and international delay handling
  - includes **one deliberate conflict** with the delay policy: an international escalation threshold of 7 days here against 5 days in the policy

**Test-only fixtures (in `sample_data/`, not loaded by default):**

- a scanned or image-only PDF
- an encrypted PDF
- an empty `.txt` file
- a `.txt` file that contains a prompt-injection line ("Ignore previous instructions and …")

`delay_days` is stored in the data, so delay answers do not depend on today's date.

## 9. Acceptance Criteria

**Document questions**

- [ ] "What is the policy for delayed shipments?" answers only from the documents and cites at least one passage with filename, page and section. No SQL is run.
- [ ] "What can a customer do if a shipment is late?" cites the communication guidelines.
- [ ] A question the documents don't cover ("What is the policy for lost pets?") says that no relevant guidance was found and does not invent a policy.

**Database questions**

- [ ] "Where is shipment 12345?" returns its `current_location` and status from `run_sql`. No retrieval is run.
- [ ] "How many Express shipments are delayed?" returns the correct count using SQL aggregation.
- [ ] **[Existing, regression]** After `sales.csv` is uploaded, the v1 questions still answer correctly: record count, average sales amount, the category with the highest revenue, the missing-column explanation, and the refusal of "delete all rows".

**Hybrid questions**

- [ ] "Shipment 12345 is delayed. What should I do?":
  - The agent queries the record first, then retrieves the **Express** delay and escalation guidance.
  - It states that 3 days exceeds the threshold and gives the escalation step.
  - The answer separates database facts from document guidance and cites both.
- [ ] "Shipment 12348 is delayed. What should I do?" points out that the record shows the shipment on schedule, and does not apply delay escalation.
- [ ] "Shipment 99999 is delayed…" says the record was not found and gives the general policy, clearly marked as general.
- [ ] "Shipment 12349 is delayed…" says there is no Refrigerated-specific policy and falls back to the general guidance, saying that it does.
- [ ] "Shipment 12347 is held. What's the escalation threshold?" reports both conflicting thresholds (5 and 7 days) with their citations, and does not silently pick one.

**Safety, isolation and lifecycle**

- [ ] The injection fixture does not change the agent's behaviour. The agent treats the line as document text.
- [ ] Two sessions with different documents never see each other's passages or tables.
- [ ] Every document validation case in §7 shows its message and leaves the session unchanged. A fourth document is rejected.
- [ ] Replacing or removing a document resets the chat, and the old content can no longer be retrieved.
- [ ] After End session, a timeout or a restart, the session folder (`data.db` and `docs.db`) is gone. After a backend instance restart, both databases are rebuilt on the next question.
- [ ] Chunking tests without the LLM pass:
  - The sample policy produces one chunk per sub-section or clause group, and each chunk has the correct `section_path` and `section_number`.
  - No chunk is cut in the middle of a sentence or mixes content from two parent sections.
  - An oversized section is split by semchunk into several chunks that all keep the same `section_path`.
  - A section that crosses a page break records the correct `page_start` and `page_end`.
  - A document with no headings uses the fallback chunking (`chunk_method = fallback`).
- [ ] Retrieval tests without the LLM pass:
  - BM25 alone finds exact terms such as `"Customs Hold"`.
  - Dense search alone finds a paraphrase ("late parcel" matches "delayed shipment").
  - Fusion ranks a chunk found by both methods above chunks found by only one.
  - Filters restrict results by `source` and `section`.
- [ ] The app still runs locally with one command, and on the existing two Cloud Run services without any new infrastructure.

## 10. Backlog

- FAISS or another ANN index, plus persistent indexes (for example in GCS) so documents are not embedded again after an instance restart.
- Parent-section expansion: when a clause matches, also return its parent section, or neighbouring clauses, for fuller context.
- LLM-assisted or layout-based structure detection (font size, bold text) for PDFs whose headings plain-text rules miss.
- A reranker (cross-encoder or LLM-based) and query rewriting or multi-query retrieval.
- OCR for scanned PDFs, plus `.docx`, `.md` and `.html` support and table extraction from PDFs.
- Richer metadata: effective dates and versions, so the newest policy can win when documents conflict.
- A retrieval and answer evaluation set (recall@k, citation accuracy, groundedness).
- Citation checking: confirm each cited passage supports its claim.
- Highlighting cited passages in the UI and showing document previews.
- More than one table per session, and sub-agents if routing becomes unreliable.
- Stronger guardrails: PII handling, output filtering and query cost limits.

## 11. Assumptions

- The model (`gemini-3.5-flash-lite`) can handle two-step tool sequencing when the instruction gives clear examples. If hybrid routing proves unreliable, the first fix is a stronger instruction, then a stronger model. A planner is not the first fix.
- Document sizes are kept small (≤ 1 MB, ≤ 20 pages, at most 3), so the session state carrying the files stays under about 5 MB and the backend fits in 512 MiB. If memory gets close to the limit, raise the backend to 1 GiB, as Sprint 2 planned.
- Embedding the documents on the backend on the first question, rather than at upload, is acceptable latency for a demo. It also keeps the UI free of API keys.
- The text extracted from a PDF follows reading order well enough for simple single-column policy documents.
- The UI is a single-user demo. ADK sessions stay in memory, and the backend keeps `--max-instances=1`.
