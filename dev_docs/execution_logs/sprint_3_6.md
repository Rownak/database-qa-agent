# Sprint 3 — Phase 3.6 Execution Log: UI Updates and Session Cleanup

## Summary
Users can now add, replace and remove policy documents in the Streamlit app. Each answer's "How this was answered" expander shows the classified intent and every tool call (the SQL, or the search query, filters and returned citations). Any accepted table or document change starts a new agent session and clears the chat. Cleanup needed no new code; tests now prove `docs.db` goes with the session folder. **214 tests pass** (37 new). A live run of the real app in local mode (real model) gave a `hybrid` answer with the expected expander data.

## What was implemented

**Document panel (`app.py`)**
- Below the table preview: a **Documents** list (name, pages for PDFs, chunk count) with a **Remove** button each, and a one-file uploader (`pdf`, `txt`, `md`).
- Each upload goes through `doc_ingest.prepare`. A `DocError` is shown with `st.error` and nothing else changes. The limit message is "Up to 3 documents per session. Remove one first.".
- A file with the name of a loaded document replaces it and keeps its place in the list. The limit counts distinct names, so replacing works with three documents loaded.
- The UI keeps the bytes of every document (`doc_entries`), so it can recreate the session.
- Documents start as the sample documents. While they are unchanged (`docs_custom = False`) no `docs` key is sent, so the backend loads the samples itself and the initial backend state is still `{}`. After the first change, the full `docs` list (`[]` when all are removed) is sent with every new session.

**New session on change**
- `_replace_session` is the one path for table uploads and document changes. API mode: a new backend session with `upload_*` and `docs`, then the old one is deleted. Local mode: the ADK session is recreated under the same id with the state, and the folder's old `docs.db` is deleted so the agent builds a new one on the next question. The chat is cleared in both.
- A backend error leaves documents, session and chat unchanged and shows "Could not send the documents to the service: …".
- **Recovery after a 404** recreates the session with the held upload and the current documents.

**Parsing tool responses and the intent (`ui_client.py`)**
- `parse_events` now returns `(answer, calls, intent)`. Each call is `{"name", "args", "response"}`; the response is matched by call id, or by name in call order when there is no id (`None` when none arrives). The intent is the last string `stateDelta["intent"]`.
- `ask` returns the same triple. If the events carry no intent it calls the new `get_intent`, which reads the session state and returns `None` instead of raising.
- Local mode dumps each ADK `Event` with `model_dump(mode="json", by_alias=True, exclude_none=True)` and uses the same `parse_events`, so both modes share one parser. Without a delta it reads the intent from the session state.
- `format_citation(passage)` builds `source, §number title, p. N` (`pp. a-b` for a range; no page for TXT/MD).

**Expander**: `_render_calls(calls, intent)` shows `Intent: …`, then per tool: `run_sql` as SQL (plus its error, if any); `search_documents` as query, filters, the list of returned citations, tool errors, "No passages returned." and the tool's note. No expander appears when there are no calls and no intent. Each stored message now carries its `intent`.

**Cleanup**: End session, the idle sweep and `purge_all` already delete the whole session folder; no code was added.

**Other**
- The title, page title and chat prompt now say "Database & Document Q&A".
- The UI image now imports `doc_ingest`, so `Dockerfile.ui` and `scripts/stage_ui_context.py` also copy `data_agent/doc_ingest.py` and `data_agent/logs.py`. (`numpy`, `pypdf` and `semchunk` were already available: pandas brings `numpy`, and the other two were added in Phase 3.1.)

## Decisions and assumptions
- **The first `docs` change is the point where `docs` starts being sent.** This keeps Phase 3.5's "missing key = sample" meaning and avoids sending about 1 MB of unchanged sample files on every session.
- **Local-mode folder:** the spec says to delete the old session "and its folder". In local mode the folder also holds the table's `data.db`, which is still in use, so only `docs.db` is removed on a document change. Table uploads still reuse the folder as before. In API mode the new session gets a new id; the old backend folder is left to the existing idle sweep, as in Sprint 2.
- **Remove acts immediately** (inside the render loop, followed by `st.rerun()`), because the left column must update the session before the chat is drawn.
- **Button indexes in tests changed**: the Remove buttons come before End session, so tests find it by label (`end_session_button` in `conftest.py`).
- **Not done (Phase 3.7):** README, redeploy, memory check. The generated `.ui_build/` folder is not regenerated here.

## Files
- **Created:** `tests/test_app_docs.py`, `dev_test/phase_3_6.py`, this log.
- **Modified:**
  - `app.py` (document panel, session replacement, `_ask`, `_render_calls`, wording)
  - `ui_client.py` (`parse_events`, `ask`, `get_intent`, `format_citation`)
  - `Dockerfile.ui`, `scripts/stage_ui_context.py`
  - `tests/test_app.py`, `tests/test_app_api.py`, `tests/test_ui_client.py`, `tests/test_sessions.py`, `tests/conftest.py`
  - `dev_docs/sprints/sprint3.md` (Phase 3.6 ticked)

## Tests
- **`test_app_docs.py` (AppTest, fake backend):**
  - sample documents listed, no `docs` key sent at start
  - remove one / remove all (`[]`): new session, old deleted, chat reset
  - add a document; same-name replace keeps its place; replace works at three documents
  - a fourth document is rejected with the exact message and nothing changes
  - five rejected files (scanned, empty, Latin-1, encrypted, 21 pages) each show one error and leave session, documents and chat as they were
  - the error clears after a good change; a backend failure keeps everything
  - table upload and document change each keep the other's state; 404 recovery resends the current documents
  - End session resets to the samples and does not reload the uploaded file
  - expander: intent, SQL, search query, filters, citations, note, search error, empty results, and no expander without calls
  - local mode: the document change recreates the ADK session with `docs` and removes the stale `docs.db`; the sample state has no `docs`; tool responses and intent are collected from real ADK `Event` objects; the intent is read from the session when no delta has it
- **`test_ui_client.py`:** response matching by id and by name, unanswered and stray responses, intent from `stateDelta` (and ignoring a non-string), `ask` reading the intent from the session, `get_intent` never raising, `format_citation`.
- **`test_sessions.py`, `test_app.py`:** `docs.db` is removed by `end_session`, `purge_all`, `sweep_stale`, End session and idle expiry.

## Live check (`dev_test/phase_3_6.py`, real app in local mode)
Removed one sample document (chat reset), then asked "Shipment 12345 is delayed. What should I do?". Result: intent `hybrid`; tools `run_sql` then `search_documents`, with five citations such as `shipment_delay_policy.pdf, §3.2 Express Shipments, pp. 1-2`; the expander was rendered.

## Limitations
- The browser itself was not driven (no real file-upload clicks); AppTest covers the Streamlit logic.
- Each document change makes the next question rebuild `docs.db` (one embedding call).

## How to run
From `db-doc-qa/`:

```bash
uv run pytest                              # 214 passed
uv run python -m dev_test.phase_3_6        # needs GOOGLE_API_KEY; real app + model
uv run streamlit run app.py                # try it by hand
```
