# Sprint 9 — Phase 9.1 Execution Log: Semantic Response Cache

## Summary
With `agent.cache.response.enabled: true`, a repeated or paraphrased **first** question is answered from process memory with no intent call, model call or tool call. With the cache off (the default) the agent builds and runs exactly as before. A live check on the shipments sample (`scope: dataset`) went from a 3.59 s miss to a 0.68 s hit (data and document build are still counted in that figure; the lookup itself took 3 ms).

## What changed
- **Config** (`data_agent/config.py`): `ResponseCacheConfig` (`enabled`, `semantic`, `threshold` 0.97, `entity_guard`, `shadow`, `ttl_s` 3600, `max_entries` 1000, `scope` session|dataset), wrapped in `CacheConfig` and exposed as `AgentConfig.cache`. All defaults are off; dotted overrides such as `agent.cache.response.enabled=true` work unchanged.
- **Shared module** (`data_agent/cache.py`, numpy only):
  - `TTLStore` (TTL + LRU, lock, injectable clock) and a registry (`get_store`, `drop(scope_id)`, `reset`).
  - `file_fp` (sha256, memoised by path, size, mtime), `cfg_fp` (config without `cache` / `latency_trace`, instruction, intent prompt and tool-declaration text, `CACHE_SCHEMA_VERSION`).
  - `normalize`, `text_hash`, `entity_tokens`, `cosine_best`.
- **Callbacks** (`data_agent/agent.py`): `_ensure_data` is split into `_prepare_session` (build) and `_classify_turn`; `_set_timing` writes or appends `turn_timing`. With the cache on, `before_agent_callback` is the list *build → lookup → classify* and an `after_agent_callback` stores the turn.
  - **Lookup:** eligibility (first user turn, non-empty, ≤ 2,000 chars, no time-relative word, no `load_error`, both fingerprints known), then the exact tier, then the semantic tier (question embedding, cosine, `threshold`, entity guard). A hit returns the stored answer as content, which ends the invocation.
  - **State:** `state["cache"]` is set on every lookup: `hit_exact` / `hit_semantic` (with `similarity`, `age_s`, `entry`, stored `calls`), `miss`, or `bypass` (with a `reason`). On a hit `intent` / `intent_reason` come from the stored turn.
  - **Store:** only an eligible miss whose turn ended with a non-empty answer, no tool error, no truncated SQL result and no call without a response. The stored call summary keeps names, args, SQL text and, for searches, chunk id / source / section / page only (no rows, no passage text).
  - **Shadow:** looks up the nearest neighbour, records `{"similarity", "guard", "nearest"}` in `state["cache"]["shadow"]` and logs it, never serves a semantic hit. The exact tier still serves.
- **Result and trace** (`events.py`, `runner.py`): `TurnResult.cache` is the last field. `parse_events` reads `stateDelta["cache"]` and, on a hit, takes `calls` from the stored summary (so the existing metrics apply, and the API path works the same). `turn_timing` skips the hit event, so a cached answer is not an `llm` stage; the `cache_lookup` stage appears when the lookup ran. `AgentSession.close` and `sessions.end_session` (which the idle sweep uses) call `cache.drop`.
- **Packaging:** `cache.py` is imported by `sessions.py`, so it was added to `Dockerfile.ui` and `scripts/stage_ui_context.py`.

## Key decisions
- **Off means the old code path.** With the cache off, `build_agent` still passes the single `make_ensure_data(cfg)` callback and no after-callback, so every existing test passes unchanged.
- **Process-level stores, not per agent.** The evaluation builds a new agent per case, so the store lives in the `cache.py` registry, keyed by scope, fingerprints and question hash.
- **Hit entries keep entity tokens, not the question.** The raw question text is never stored or logged.
- **Errors are misses.** Any exception in the lookup becomes `{"response": "miss", "error": <type>}` (logged by type only), and a turn with `error` is not stored.
- **Embeddings** go through a separate `embedding` store (also used by the store step, so a question is embedded once); 9.5 can reuse it.
- **Time-relative list:** today, tonight, tomorrow, yesterday, now, current(ly), latest, recent(ly), this/last/next week|month|year|quarter. It errs on the side of bypassing.

## Files
- Created: `data_agent/cache.py`, `tests/test_response_cache.py` (49 tests), `dev_test/phase_9_1.py`, this log.
- Modified: `data_agent/config.py`, `data_agent/agent.py`, `data_agent/events.py`, `data_agent/runner.py`, `data_agent/sessions.py`, `Dockerfile.ui`, `scripts/stage_ui_context.py`, `dev_docs/sprints/sprint9.md` (Phase 9.1 ticked).

## Tests
- Command (from `db-doc-qa/`): `uv run pytest` → **475 passed** (425 existing, unchanged, plus 49 new, plus the UI staging test that the new `cache.py` copy line touches).
- New tests cover: normalisation, keys and entity tokens; `TTLStore` TTL and LRU; `drop` and the shared scope; file and config fingerprints; config defaults and overrides; exact hit and miss; semantic hit, below-threshold miss, the guard blocking "client 252" → "client 253" (and passing without it); shadow output; a follow-up bypass; time-relative / empty / too-long / `load_error` / missing-fingerprint bypasses; invalidation by data, documents and configuration; session isolation and `drop` on close and sweep; unsafe turns not stored (tool error, truncation, empty answer, unanswered call); exceptions in the file hash, the embedding and the store; off being identical; a hit filling `calls`, `intent` and `cache`; no `llm` stage on a hit; and an end-to-end run through a real ADK `Runner` with a scripted model (a repeat makes no model or classifier call, and a follow-up in the same session runs the model).

## Run it
```bash
uv run pytest
uv run python -m dev_test.phase_9_1        # live: the same question twice, prints miss -> hit
```
Live output (abridged):
```
miss: latency=3.588s  cache={'response': 'miss'}                       stages=[data_build .025, docs_build .738, cache_lookup .003, intent .892, llm 1.268, tool .002, llm .658]
hit : latency=0.678s  cache={'response': 'hit_exact', 'similarity': 1.0, 'age_s': 0.687, ...}  stages=[data_build .019, docs_build .651, cache_lookup .003]
```

## Notes and limitations
- **Dataset scope needs identical `docs.db` bytes.** In the live check two separately built sessions of the sample produced the same fingerprint, so the hit worked. If a build ever stops being byte-stable the cache just misses.
- **Sessions that build their own databases** pay the build cost on a hit too (about 0.7 s for the sample documents); a prebuilt bundle (`state_file`, as in LakeQuest) does not.
- **Shadow with the exact tier:** shadow does not turn the exact tier off. Say so when reading the 9.2 arms.
- **Semantic threshold 0.97 is a placeholder** until the 9.2 sweep; the semantic tier is untested against real embeddings here (tests use fake vectors).
- The `follow_up` check counts `user` events in the ADK session; a session whose history was trimmed would look like a first turn.
- The cache is process memory only: a restart clears it, and each Cloud Run instance has its own.
