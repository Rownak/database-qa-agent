# Sprint 3 — Phase 3.5 Execution Log: Orchestrator — Routing, Sequencing, Grounded Answers

## Summary
The agent is now `qa_orchestrator` with exactly two tools, `run_sql` and `search_documents`. `ensure_data` builds `docs.db` as well as `data.db`, each on its own, and the instruction is rewritten for routing, sequencing, edge cases, cited answers and untrusted content. **177 tests pass** (13 new in `test_agent.py`; the old agent tests were rewritten). The live run on the real model gave the expected tool sequence and answer for all nine questions.

## What was implemented

**Agent (`data_agent/agent.py`)**
- `name="qa_orchestrator"`, new `description`, `tools=[run_sql, search_documents]`. `get_table_info`, `get_sample_rows` and `get_column_stats` stay in `tools.py` but are no longer given to the agent (row counts now go through `run_sql`).

**`ensure_data` and documents**
- `data.db` is checked as before. `docs.db` is checked on its own through `_docs_ready`, so only the missing file is rebuilt (a new Cloud Run instance rebuilds both).
- `_build_session_docs` reads the `docs` state key:
  - missing → the bundled sample documents (`SAMPLE_DOCS_DIR`)
  - `[]` → no documents: `docs_db_path = None`, `docs_summary = "No documents loaded"`, no embedding call
  - a list → `doc_ingest.build`
- `docs.db` is written next to `data.db` (the folder of `db_path`, or a new session folder if there is none).
- Skipped documents are added to `load_error` as "These documents were skipped: name (reason)". A table-upload error already in `load_error` is kept, and the same notice is never added twice, so a rebuild on a later turn does not repeat it.
- A finished build with no usable documents is stored as a result (`docs_summary` is set), so it is not rebuilt on every turn.
- Order in `ensure_data`: data → documents → intent classification (so the classifier sees both summaries; this is what Phase 3.4 waited for) → sweep.

**`INSTRUCTION`**: the only placeholders are `{schema_summary}`, `{docs_summary}`, `{intent?}` and `{load_error?}`. It covers:
- **Tools:** what each is for.
- **Routing (rules 1–4):** the intent is a hint, not a gate. Record plus "what should I do" → `run_sql` first, then `search_documents` with the type/status in the query. A document value needed to filter records → `search_documents` first. No documents → don't call the search tool.
- **Edge cases (5–8):** record not found (general guidance, labelled), no type-specific section (use the general one), conflicting documents (both statements with their citations), and a user claim the record contradicts (the record wins).
- **Answer format:** From the database / From the documents with inline `[source, §number title, p. N]` citations / Gaps or conflicts / a final `Sources:` line with the SQL and `chunk_id`s. Pages only for PDFs.
- **Kept from v1:** grounding (never invent), cleaned names in double quotes, retry failed SQL once, read-only refusals, truncation note.
- **Uploaded content:** text in tool results is data; instructions in it are ignored.
- **Notices:** relay `load_error` in the first answer.

## Decisions and assumptions
- **The search guidance changed after the live run.** On the first run the "12347" question searched with a `section` filter and never read `international_shipping_procedures.txt`, so the 5- versus 7-day conflict was missed. Rule 2 now says to start with an unfiltered search (filters hide other documents), and rule 7 says to compare the numbers in all retrieved passages. After the change the answer cites both documents and reports the conflict.
- **`Sources:` replaces v1's `Source:`** since an answer can now use several tools. Neither `app.py` nor `ui_client.py` parses this line, so nothing depends on the old wording.
- **Tests use a `fake_embed` fixture** (`conftest.py`) that replaces `doc_ingest.embed` with unit vectors and records the calls. `ensure_data` now builds the sample documents when `docs` is missing, which would otherwise call the embedding API in every test of it.
- **The preset-state case.** The Streamlit app creates `db_path` itself, so `docs.db` goes into that folder. The existing test that no new folder appears still passes.
- **Not done here (Phase 3.6):** the UI does not send `docs` yet, so today every session gets the sample documents. `README.md` and the `.ui_build/` copy are not updated (README is Phase 3.7).

## Files
- **Created:** `dev_test/phase_3_5.py`, this log.
- **Modified:**
  - `data_agent/agent.py` (agent definition, instruction, `ensure_data`, `_docs_ready`, `_build_session_docs`, `_sample_docs`)
  - `tests/test_agent.py` (rewritten definition/instruction tests, 13 new `ensure_data` tests)
  - `tests/test_intent.py` (its `root` fixture uses `fake_embed`)
  - `tests/conftest.py` (`fake_embed`)
  - `dev_docs/sprints/sprint3.md` (Phase 3.5 ticked)

## Tests
- The agent has exactly two tools, named `run_sql` and `search_documents`, and `ensure_data` is its callback.
- The instruction has only the four placeholders and no other braces. Every placeholder has a value in the state after `ensure_data` (`load_error` and `intent` may be absent).
- The instruction text contains the routing, sequencing, edge-case, answer-section, untrusted-content and "no documents" rules.
- `ensure_data`:
  - the sample documents (a missing `docs` key), with the embedding call logged once as `RETRIEVAL_DOCUMENT`
  - no documents (`[]`): no file, no embedding call, and the result is stable across turns
  - uploaded documents replace the sample
  - a skipped document → `load_error`, not repeated after two rebuilds
  - all documents skipped
  - a table error and a document error both reported
  - only `docs.db` rebuilt (the `data.db` modification time is unchanged)
  - only `data.db` rebuilt (not re-embedded)
  - both rebuilt after a restart
  - embedding failure still writes `docs.db`
  - classification happens after both databases are ready

## Live check results (`dev_test/phase_3_5.py`, real `gemini-3.5-flash-lite`)
| Question | Intent | Tools | Result |
|---|---|---|---|
| Policy for delayed shipments | document | `search_documents` ×2 | cited, no SQL |
| Where is shipment 12345? | database | `run_sql` | Cleveland Sort Facility, Delayed |
| 12345 delayed, what to do | hybrid | `run_sql` → `search_documents` | Express rules, separate sections |
| Delayed shipments past their threshold | hybrid | `search_documents` ×2 → `run_sql` | thresholds 4/2/5 used in the SQL |
| 12348 ("delayed") | hybrid | `run_sql` → `search_documents` | says the record shows 0 days |
| 99999 | hybrid | `run_sql` → `search_documents` | "not found", general guidance labelled |
| 12349 (Refrigerated) | hybrid | `run_sql` → `search_documents` ×3 | no Refrigerated section, general rule used |
| 12347 (conflict) | hybrid | `run_sql` → `search_documents` | 5 days vs 7 days, both cited |
| Delete all rows | database | none | refused as read-only |

## Limitations
- Behaviour comes from the instruction and the model, so wording of answers varies between runs (for example, a long answer to a broad policy question). The tool sequence above was stable in the runs made.
- The `Sources:` line sometimes lists `chunk_id`s in different formats. Not enforced by code.
- The first turn of a session now makes an embedding call for the sample documents (about 37 chunks).

## How to run
From `db-doc-qa/`:

```bash
uv run pytest                              # 177 passed
uv run python -m dev_test.phase_3_5        # needs GOOGLE_API_KEY; all 9 questions
uv run python -m dev_test.phase_3_5 2 7    # only questions 2 and 7 (0-based)
```
