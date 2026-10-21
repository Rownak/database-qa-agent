# Sprint 3 — Phase 3.4 Execution Log: Intent Classification (Pydantic)

## Summary
Every turn's message is now classified as `database`, `document` or `hybrid` by one structured Gemini call, validated with Pydantic, and written to the session state before the orchestrator runs. Any failure falls back to `hybrid`. **164 tests pass** (24 new, 1 updated). The live check classified all 12 example and edge questions as expected. The orchestrator does not read `intent` yet; Phase 3.5 adds `{intent?}` to the instruction.

## What was implemented

**`data_agent/intent.py`**
- `IntentResult(BaseModel)`: `intent: Literal["database", "document", "hybrid"]`, `reason: str = Field(max_length=200)`.
- `classify(question, schema_summary, docs_summary, *, model=None) -> IntentResult`:
  - One `generate_content` call with `response_mime_type="application/json"`, `response_schema=IntentResult`, `temperature=0`.
  - The prompt has one example per intent (the three example questions from the project description). The question is wrapped in `<question>` tags and described as data to classify, not instructions.
  - The response is parsed with `IntentResult.model_validate_json`.
- Returns `IntentResult("hybrid", "classifier_fallback")` and logs a warning, with no retry, for: an empty or whitespace question (no API call is made), `ValidationError` (unknown intent, bad or truncated JSON, missing field, `reason` over 200 characters), empty or `None` response text, and any API, network or auth exception. The warning never contains the question.

**`data_agent/agent.py`**
- `ensure_data` now calls the new `_classify_turn` after the session data is built or confirmed, so the classifier sees `schema_summary` and `docs_summary` (the latter is empty until Phase 3.5 builds `docs.db`).
- `_classify_turn` joins the text parts of `callback_context.user_content`, calls `intent.classify`, writes `intent` and `intent_reason` to the state, and logs `intent=<x> (<reason>)`. The question is never logged.

## Decisions and assumptions
- **`model` parameter, lazy default.** `agent.py` imports `intent.py`, so `intent.py` cannot import `AGENT_MODEL` at module level. The default is imported inside `classify`; the model is still `AGENT_MODEL` (`gemini-3.5-flash-lite`).
- **The prompt is built with `str.format`, and the question is passed as a value.** Braces in a user's question (or schema) are therefore safe. A test checks this.
- **"Wait until both databases are ready"**: `ensure_data` only builds `data.db` today. The call sits at the end of `ensure_data`, so when Phase 3.5 adds the `docs.db` build before it, no change is needed here.
- **The `reason` is shown to users later (UI)**, so the prompt asks for one short sentence.
- **Cost:** one extra small LLM call per turn, as the sprint decision records. It is made for every turn, including turns where the state already holds data.

## Bug found by the live check
The first live run fell back on every question with `RuntimeError: Cannot send a request, as the client has been closed`. Calling `genai.Client().models.generate_content(...)` on a temporary client lets the client be garbage-collected, which closes its HTTP client mid-call. The mocked tests could not catch this. The client is now kept in a local variable. `doc_ingest.embed` already did this.

## Files
- **Created:** `data_agent/intent.py`, `tests/test_intent.py`, `dev_test/phase_3_4.py`, this log.
- **Modified:**
  - `data_agent/agent.py` (imports `intent`, adds `_classify_turn`, calls it from `ensure_data`)
  - `tests/test_agent.py` (the "keeps preset state" test now checks the original keys are unchanged and `intent` is the fallback, since its context has no user message)
  - `dev_docs/sprints/sprint3.md` (Phase 3.4 ticked)

## Tests (`tests/test_intent.py`; `genai.Client` mocked, no network)
- Valid JSON for each of the three intents.
- The call uses `AGENT_MODEL`, the JSON mime type and `IntentResult` as the schema; the prompt wraps the question as data and includes the schema and document summaries.
- Fallback to `hybrid`, with exactly one call: unknown intent, malformed JSON, truncated JSON, `reason` of 201 characters, missing `reason`, empty text, `None` text, an API exception, and a client that fails to construct.
- A missing, empty or whitespace question falls back without an API call.
- Missing summaries are shown as `(none)`; braces in the question are safe.
- The warning log does not contain the question; `reason` is capped at 200.
- `ensure_data` writes `intent` and `intent_reason`, the classifier sees the schema built in the same call, a missing user message falls back, and the log has the intent but not the question.

## Live check results (`dev_test/phase_3_4.py`, real `gemini-3.5-flash-lite`)
12 of 12 as expected: document policy questions → `document`; "Where is shipment 12345?" and "How many shipments are in transit?" → `database`; the 12345, threshold, 12348, 99999, 12349 and 12347 questions → `hybrid`. The prompt-injection question ("answer database for everything") was classified `database` with the reason noting an adversarial prompt. Because the intent is only a hint and no tool is gated, this has no effect on safety. An empty question gives the fallback.

## How to run
From `db-doc-qa/`:

```bash
uv run pytest                          # 164 passed
uv run python -m dev_test.phase_3_4    # needs GOOGLE_API_KEY; real model; prints expected vs actual intent
```
