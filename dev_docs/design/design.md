# Database & Document Q&A Agent — Technical Design (v2)

This design implements [project_description.md](project_description.md). It extends the v0.1.1 code described in [design_v1.md](design_v1.md), and everything in v1 that is not mentioned here stays as it is. For each decision it picks one approach.

---

## 1. Architecture

The deployment is the same as today: Streamlit UI → private ADK API server (or an in-process `Runner` in local mode). Every file the session uses travels in the ADK session state. The backend builds two per-session SQLite files from that state.

```
              ┌──────────── Streamlit UI (app.py) ────────────┐
 table ──────▶│ ingest.load_file      → preview, validate     │
 documents ──▶│ doc_ingest.prepare    → pages, chunks, errors │
              └───────────────┬───────────────────────────────┘
                              │ create_session(state: upload_*, docs[])
                              ▼
              ┌──────── ADK API server / Runner ──────────────┐
              │ before_agent_callback ensure_data             │
              │   ├─ data.db  ← ingest.load_file              │
              │   └─ docs.db  ← doc_ingest.build (+ embed)    │
              │                                               │
 question ───▶│ Agent "qa_orchestrator" (Gemini)              │
              │   ├─ run_sql ──────────── read-only ─▶ data.db │
              │   └─ search_documents ─── read-only ─▶ docs.db │
              │          BM25 (FTS5) + dense (numpy) → RRF    │
              └───────────────┬───────────────────────────────┘
                              ▼
               answer: DB facts │ document guidance │ gaps │ Sources
```

Both files are stored in `/tmp/adk_sheet_qa/<session_id>/`.

## 2. Project Structure and Dependencies

```
db-doc-qa/
├── app.py                 # changed: document uploader, doc list, expander shows passages
├── ui_client.py           # changed: parse_events also returns tool responses
├── data_agent/
│   ├── agent.py           # changed: orchestrator instruction, ensure_data builds docs.db
│   ├── tools.py           # changed: agent gets run_sql + search_documents
│   ├── ingest.py          # unchanged (table)
│   ├── doc_ingest.py      # new: validate, extract, structure-aware chunk, embed, write docs.db
│   ├── retrieval.py       # new: BM25, dense, filters, RRF
│   ├── sessions.py        # unchanged (whole folder is swept)
│   └── sample_data/default/   # loaded at session start: shipments.csv + docs/ (2 PDFs, 1 TXT)
├── sample_data/           # test fixtures only: database/ (tables, incl. sales.csv) and documents/ (PDF, TXT, MD)
└── scripts/make_sample_pdfs.py   # dev only: renders the sample policy PDFs
```

| Dependency | Where | Why |
|---|---|---|
| `pypdf` | backend + UI | Pure-Python extraction of PDF text page by page; also detects encrypted files and counts pages |
| `semchunk` | backend + UI | Splits sections into chunks (the UI uses it for the preview chunk count) |
| `numpy` | already installed with pandas | Cosine similarity over stored vectors |
| `google-genai` | already installed with `google-adk` | Gemini embedding calls |
| `fpdf2` | `dev` group only | Generates the sample PDFs |

Add them with `uv add pypdf semchunk` and `uv add --dev fpdf2`. Copy the pinned versions from `uv.lock` into `data_agent/requirements.txt` and `requirements-ui.txt`, as Sprint 2 did. Skipped on purpose: FAISS, `rank_bm25`, `tiktoken`, and local embedding models.

## 3. Session State

| Key | Set by | Meaning |
|---|---|---|
| `upload_name`, `upload_b64` | UI (existing) | The table upload. Missing means the sample `shipments.csv` |
| `docs` | UI (new) | A list of up to 3 `{name, b64}`. **A missing key means the sample documents; `[]` means no documents** (the user removed them all) |
| `db_path`, `column_map`, `schema_summary` | `ensure_data` (existing) | Table database and schema text for the instruction |
| `docs_db_path`, `docs_summary` | `ensure_data` (new) | Documents database and one line per document (name, title, pages, top-level sections) for the instruction |
| `load_error` | `ensure_data` | The failed table upload or skipped documents, relayed to the user by the agent |

`ensure_data` checks each database on its own. If a file is missing (the first turn, or a new Cloud Run instance), it rebuilds that file from the state and leaves the other untouched. When there is nothing to load, the agent's instruction shows "No documents loaded".

## 4. Upload Validation and Ingestion

**Table:** unchanged (`ingest.load_file`). The only change is that the default sample becomes `shipments.csv`.

**Documents:** `doc_ingest` has two entry points that share one pipeline.

- `prepare(name, bytes)`: validate → extract → chunk. The UI calls it before accepting a file and shows the page and chunk counts. It does not embed.
- `build(docs, session_dir)`: runs `prepare` for each document, then embeds the chunks and writes `docs.db`. `ensure_data` calls it.

**Validation (in order; the first failure stops):**

1. The extension is `.pdf`, `.txt` or `.md`.
2. The file is at most 1 MB.
3. The file can be read:
   - **PDF:** `pypdf` must open it, it must not be encrypted, and it must have at most 20 pages.
   - **TXT and MD:** it must decode as UTF-8 (`utf-8-sig`). Markdown is read as plain text; its `#` lines are the headings.
4. The extracted text is not empty after trimming. A scanned PDF fails here.

The UI enforces the limit of 3 documents and replaces a document that has the same name. Messages are those in the project description (§7). A failure returns a `DocError` (the same pattern as `IngestError`), and the session is left unchanged.

**Structure-aware chunking:**

1. **Extract text with page markers**, so every character offset maps back to a page.
2. **Find headings line by line with regular expressions:**
   - numbered headings `^\d+(\.\d+)*\s+\S`, where the depth is the number of dots plus 1
   - clause markers `^\(?[a-z]{1,3}\)`
   - `#` headings in TXT and MD files, where the level is the number of `#`
   - short lines in title case or all capitals, with no closing full stop
3. **Build the section paths with a stack.** Each heading closes any open heading at the same or deeper level, so every heading knows its parent, for example `3 Escalation > 3.2 Express Shipments`.
4. **Split each section with `semchunk`**:
   - Chunk size is 160 words, which is about 200 tokens.
   - The token counter is a whitespace word count, so no tokenizer dependency is needed.
   - There is no overlap inside structured sections.
5. **Fall back when no headings are found:** the whole document goes through semchunk with an overlap of about 15%. The section path is set to the document title, or to the filename if there is no title. The chunk is tagged `chunk_method = fallback`.
6. **Record pages for each chunk:** `page_start` and `page_end` come from the chunk's offsets.

**Embedding:**

- **Model:** `gemini-embedding-001` with `output_dimensionality = 768`.
- **Task types:** `RETRIEVAL_DOCUMENT` for chunks and `RETRIEVAL_QUERY` for queries.
- **What is embedded:** each chunk's text with its section path added to the front.
- **Calls:** chunks are sent in batches of up to 100.
- **Storage:** vectors are L2-normalised and stored as float32 BLOBs.
- **If embedding fails:** `docs.db` is still written, with null embeddings, and retrieval falls back to BM25 only.

**`docs.db` schema:**

- **`documents`:** `doc_id`, `source`, `doc_type`, `doc_title`, `pages`.
- **`chunks`:** every metadata field listed in project description §6, plus `embedding`.
- **`chunks_fts`:** an FTS5 table holding the chunk text with its section path at the front, linked by `rowid` to `chunks`, using the `porter unicode61` tokenizer.

The file is written to a temporary file and moved into place with `os.replace`, as `data.db` is.

## 5. Agent and Tools

**Agent:**

- One `Agent(name="qa_orchestrator", model="gemini-3.5-flash-lite", tools=[run_sql, search_documents], before_agent_callback=ensure_data)`.
- No sub-agents, planner or output schema.
- The functions `get_table_info`, `get_sample_rows` and `get_column_stats` stay in `tools.py`, for tests and possible later reuse, but are not given to the agent.

**Tools.** Both tools find their database path in `tool_context.state`, open a read-only connection (`mode=ro`) for each call, return `{"status": ...}`, and never raise exceptions.

| Tool | Inputs | Output |
|---|---|---|
| `run_sql` (existing) | `query: str` | `columns`, `rows`, `row_count`, `truncated` (100-row limit, 5 s time limit, single `SELECT`/`WITH`) |
| `search_documents` (new) | `query: str`, `source: str \| None`, `section: str \| None`, `top_k: int = 5` (max 8) | `passages` (a list of `chunk_id`, `source`, `section_path`, `section_number`, `page_start`, `page_end`, `text` ≤ 1,200 characters, `score`, `matched_by`), plus `note` (no match, filter excluded everything, or BM25-only) |

**`search_documents` steps (`retrieval.py`):**

1. **Filter:** `source` must match the filename exactly. `section` is a case-insensitive `LIKE` match on `section_path`, so it also includes every subsection and clause under a matching heading.
2. **BM25 search:**
   - Turn the query into FTS5 syntax by joining its words, each quoted, with `OR`. This keeps punctuation in the query from breaking the FTS5 syntax.
   - Take the top 20 chunks, ranked by `bm25()`.
3. **Dense search:**
   - Embed the query once.
   - Take the dot product with the stored vectors of the filtered chunks, and keep the top 20.
4. **Fuse with RRF (k = 60):** dense-only hits with cosine similarity below 0.55 are dropped. This threshold is a constant to tune against the sample documents.
5. **Return the results:** the top `top_k` chunks, in fused order.

**Routing and sequencing.** The routing rules are written in the instruction, and the model chooses the tools. No code decides the route. The schema `{schema_summary}` and the document list `{docs_summary}` are placed in the instruction, so the agent knows what each source holds before its first call. Rules:

1. **Records, counts or values** (IDs, statuses, locations, aggregates) → `run_sql`.
2. **Policy, procedure, rules or "what should I do"** → `search_documents`.
3. **A question about a specific record and what to do about it** → call `run_sql` first. Then call `search_documents`, putting record details such as shipment type and status into the query or the `section` filter. Finally, compare the record with the policy.
4. **A question that needs a document value to filter records** (for example a threshold) → call `search_documents` first, then `run_sql`.
5. **Record not found** → say so, and give only general guidance, marked as general.
6. **No type-specific section** → say so, and use the general section.
7. **Documents disagree** → report both statements with their citations, and don't pick one.
8. **The user's claim contradicts the record** → say so, and answer according to the record.

**Answer format.** The answer has these sections:

1. **From the database:** facts, with the SQL used.
2. **From the documents:** guidance, with an inline citation `[source, §section_number title, p. N]` for each claim.
3. **Gaps or conflicts:** only when there are any.
4. **Sources:** a final line naming the SQL and the `chunk_id`s used.

The agent cites only passages returned in the current turn, and never states a number or rule that no tool returned.

**Uploaded content is data, not instructions:**

- The instruction says that anything inside tool results (passages, cells) is data, and any instructions written there must be ignored.
- Tools take no paths and no SQL against `docs.db`, so the content of a document cannot widen access.

## 6. Isolation and Cleanup

- **Isolation:** each session has its own folder, its own `data.db` and `docs.db`, and its paths come only from state.
- **No cross-session data:** retrieval never caches vectors across sessions. The vectors are loaded from the session's `docs.db` on every call; this is fast at a few hundred chunks.
- **Cleanup:** the existing mechanisms (End session, `sweep_stale` after 30 minutes, `purge_all` at startup) delete the whole session folder, so `docs.db` is cleaned up with no new code.
- **Changing files:** any accepted change to the table or documents creates a new ADK session, using the existing `_api_new_session` path with the full `docs` list, and the chat is reset. The old session is deleted with its folder.
- **Recovery after a 404:** the UI holds the current table and document bytes, so it can recreate the session, as it does today.

## 7. UI Changes (`app.py`)

- **Documents uploader:** one file at a time (`type=["pdf", "txt", "md"]`). Accepted files are listed with their pages and chunk counts and a **Remove** button. The 3-document limit and replacing same-name files are checked here.
- **Expander:** "How this was answered" also shows each `search_documents` call (its query and filters) and the citations of the passages it returned. Two code changes support this:
  - `ui_client.parse_events`, and the event loop used in local mode, also collect `functionResponse` parts and match each one to its call.
  - `_render_calls` displays a call according to its tool name: SQL for `run_sql`, and the query plus the list of citations for `search_documents`. Today it renders any `query` argument as SQL.
- **Local mode:** works without `API_URL`, as today.

## 8. Verification Checklist

**Without the LLM** (`uv run pytest`):

- [ ] `doc_ingest.prepare` accepts the sample PDFs, TXT file and Markdown fixture. It rejects the wrong extension, oversize, more than 20 pages, encrypted, scanned and empty files, and non-UTF-8 text, each with its message.
- [ ] Chunking gives the expected `section_path`, `section_number` and page range for known sections of the sample policy. The no-heading fixture uses the fallback.
- [ ] `retrieval`:
  - BM25 finds `"Customs Hold"`.
  - Dense search finds a paraphrase (using mocked or recorded embeddings).
  - RRF ranks chunks found by both methods higher.
  - The `source` and `section` filters restrict results.
  - With null embeddings, search returns BM25 results and a note.
- [ ] `ensure_data` builds both databases, rebuilds only the missing one, uses the sample documents when `docs` is missing, and loads no documents when `docs` is `[]`.
- [ ] Existing table, SQL-guard and session tests still pass.

**End to end** (`uv run streamlit run app.py`, local and then on Cloud Run):

- [ ] **Document question:** "What is the policy for delayed shipments?" uses only `search_documents`, with citations.
- [ ] **Database question:** "Where is shipment 12345?" uses only `run_sql`.
- [ ] **Hybrid question:** "Shipment 12345 is delayed. What should I do?" calls `run_sql` and then `search_documents` (Express). It answers with separate database and document sections and cites both.
- [ ] Hybrid edge cases give the expected gap or conflict message: 12348 (the record shows on schedule), 99999 (not found), 12349 (no Refrigerated policy) and 12347 (5 vs 7 days).
- [ ] The injection fixture is treated as text, and "delete all rows" is still refused.
- [ ] A fourth document is rejected. Removing a document resets the chat, and its content can no longer be found.
- [ ] After End session or a timeout, the session folder is gone. After a Cloud Run instance restart, both databases are rebuilt on the next question, and memory stays under about 80% of 512 MiB.
