# Sprint 9 — Caching (response, prefix, retrieval)

**Goal:** add three independent, opt-in caches and measure each one on LakeQuest with the existing evaluation and latency tracing, as described in [caching_design.md](../design/caching_design.md). One cache per implementation phase, each followed by its own evaluation phase; the last phase measures them together.

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use fakes, with no network
- a short script `db-doc-qa/dev_test/phase_9_N.py`, run with `uv run python -m dev_test.phase_9_N`
- an entry in `dev_docs/execution_logs/sprint_9_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 9.1 | Semantic response cache |
| 9.2 | LakeQuest evaluation of the response cache (builds the shared cache workload) |
| 9.3 | Prompt/prefix cache (provider implicit and explicit caching) |
| 9.4 | LakeQuest evaluation of the prefix cache |
| 9.5 | Retrieval result cache |
| 9.6 | LakeQuest evaluation of the retrieval cache |
| 9.7 | Caches combined, final report |

**Rules for all phases**

- **Off by default, identical when off.** Each cache has its own `agent.cache.<name>` block, defaulting to today's behaviour. With every cache off, the app, the deployed agent and all existing configs behave exactly as before, `results.jsonl` and `metrics.json` keep their keys, and the existing tests pass unchanged.
- **Independently runnable.** Each implementation phase leaves the pipeline working with its cache on or off and needs no other cache. Each evaluation phase needs only its own cache and the workload built in 9.2. Evaluation arms differ only in dotted overrides.
- **Dataset-agnostic.** No dataset name in `data_agent/` or `evals/{run,report,metrics}.py`. LakeQuest-specific material lives only in the derived dataset, its config and dev scripts.
- **A cache never decides whether a question is answered.** Any cache error is a miss and is logged by exception type only. Every key carries the data, document and config fingerprints. Benchmark labels are used only for scoring.
- **Reuse, don't restructure:** `AgentConfig` and dotted overrides, the agent callbacks, `latency_trace` (stages only when they ran), `run_eval`, per-tag metrics, per-case `state_file`, `report.write_run`. Process memory only; no new dependency.
- **Privacy:** no questions, answers or passages in logs. The retrieval cache stores ids, scores and citation metadata, never passage text.
- **Standard evaluation record** (every evaluation phase), always split by pass and never pooled:
  - **cold vs warm latency:** p50 / p95 / max per pass and route. Cold is pass U in a fresh process; warm is the later passes in the same process.
  - **hit rates** of the cache under test
  - **answer quality:** the existing metrics, compared per case with the baseline
  - **invalidation:** pass S (stale probes) and pass N (near misses) served from cache must both be **0**
- **Baseline:** in 9.2 and 9.6 the baseline is "no app caches, provider default". In 9.4 and 9.7 it also defeats provider caching (`prefix.mode: bust`). Differences under the measured ±0.5 s run-to-run spread are reported as noise.

---

## Phase 9.1 — Semantic Response Cache

**Goal:** a repeated or paraphrased first question in a session is answered from cache, without the intent call, model calls or tools (design §4).

- [ ] **Config** (`data_agent/config.py`): `CacheConfig` in `AgentConfig.cache` with a `response` block: `enabled: false`, `semantic: false`, `threshold: 0.97`, `entity_guard: true`, `shadow: false`, `ttl_s: 3600`, `max_entries: 1000`, `scope: session | dataset`.
- [ ] **Shared cache module** `data_agent/cache.py` (numpy only), reused by 9.3 and 9.5:
  - a TTL + LRU store with a lock, and a namespace registry with `drop(scope_id)`
  - fingerprints: `data_fp` and `docs_fp` (sha256, memoised by path, size and mtime) and `cfg_fp` (config, resolved instruction and intent-prompt text, `CACHE_SCHEMA_VERSION`)
  - `normalize` and entity-token extraction
- [ ] **Lookup and store** (`data_agent/agent.py`):
  - The callback list becomes build → lookup → classify. The lookup checks eligibility (first user turn, no time-relative words, no `load_error`, fingerprints present), then the exact tier, then the semantic tier (embedding, cosine, threshold plus entity guard).
  - A hit returns the stored answer and writes `state["cache"]` with the stored call summary.
  - `after_agent_callback` stores eligible turns: no error, non-empty answer, no tool error or truncation.
  - `shadow` logs the nearest similarity, the guard result and the nearest entry id, and never serves.
  - Entries are dropped when the session closes or is swept.
- [ ] **Result and trace** (`events.py`, `runner.py`): `TurnResult.cache` as the last field; on a hit, `calls` come from the stored summary so the existing metrics apply. The trace gets a `cache_lookup` setup stage when the lookup ran, and the hit answer is not counted as an `llm` stage.
- [ ] **Tests:**
  - keys and normalisation; exact and semantic hit and miss
  - the guard blocks "client 252" → "client 253"
  - a follow-up (two turns in one fake session) bypasses
  - TTL and LRU; session isolation and `drop`; invalidation by each fingerprint
  - fallback on injected exceptions; off is identical
  - a hit fills `calls`
- [ ] **Dev script** `phase_9_1.py`: ask one question twice with `scope: dataset` (live) and print miss → hit, latency and `state["cache"]`.

## Phase 9.2 — LakeQuest Evaluation: Response Cache

**Goal:** the response cache measured on repeated and unique LakeQuest questions, with the threshold set from data (design §7). This phase also builds the workload and the arm comparison reused by 9.4, 9.6 and 9.7.

- [ ] **Workload** (built once by `dev_test/phase_9_2_build.py`; nothing under `benchmark/` is committed): `benchmark/lakequest_prepared/bank_sample_cache/dataset.yaml`, keeping each original's `expected` and run in YAML order.
  - **U:** the 40 originals.
  - **E:** 20 verbatim copies of half A.
  - **P:** 20 frozen paraphrases of half B, with ids, names and numbers checked equal to the original and read once by hand.
  - **N:** 10 near-miss entity swaps.
  - **S:** 4 stale probes on mutated copies of `bank.db` and `docs.db`, via per-case `state_file`.
  - Each case is tagged with its pass (`pass_unique`, `pass_exact`, `pass_paraphrase`, `pass_near_miss`, `pass_stale`).
- [ ] **Config** `configs/eval/lakequest_cache.yaml`: `lakequest_bank.yaml` with the derived dataset, `latency_trace: true`, every cache off and `scope: dataset`.
- [ ] **Hit-rate aggregation** (`evals/run.py`, `__main__.py`): `metrics["cache"]` counts the outcomes in `TurnResult.cache` per tag, generically for any cache, plus one summary line. Tested with a fake session.
- [ ] **Runs and record:** baseline, `response.enabled=true` (exact), and `semantic=true shadow=true`. Record the standard set, plus model calls and tokens saved and a threshold sweep (0.90–0.99) from the shadow logs: pass-P hits and pass-N false hits with and without the guard.
- [ ] **Report:** start `dev_docs/design/caching_report.md` with a §Response section: unique (cold) overhead, repeat (warm) benefit, quality, invalidation, the chosen threshold, and whether the semantic tier passes its ~9 % break-even.
- [ ] **Dev script** `phase_9_2.py`: compare two run folders per pass (hit rate, p50 / p95, guarded metrics); 9.4, 9.6 and 9.7 reuse it.

## Phase 9.3 — Prompt/Prefix Cache

**Goal:** maximise provider prefix reuse, add explicit shared caching for large stable prompts, and add an off switch, using only Gemini / ADK capabilities (design §5). Implicit caching stays the default.

- [ ] **Config:** a `cache.prefix` block with `mode: implicit | explicit | bust` (default `implicit`, today's behaviour; `bust` is evaluation-only "off"), `stable_layout: false`, `min_tokens: 4096` and `ttl_s: 3600`.
- [ ] **Trace:** `cached_tok` on `llm` stages from `usageMetadata.cachedContentTokenCount`, and the cached share of input tokens in `latency_stages`.
- [ ] **Bust and stable layout** (`agent.py`):
  - `bust` is a `before_model_callback` that puts a unique line first in every system instruction.
  - `stable_layout` moves the `{intent?}` and `{load_error?}` lines to the end of the instruction, below a fixed marker. It applies to built-in profiles and to instruction files that contain those lines.
- [ ] **Explicit mode:**
  - First check whether ADK `ContextCacheConfig` caches the first model call of a one-question session, and whether a request may still set `system_instruction` or `tools` alongside `cached_content`. Use ADK if it fits.
  - Otherwise add a `before_model_callback` that holds one `cachedContents` per (model, sha256 of the stable part: instruction above the marker plus tools). It is created lazily with a TTL and scoped like the other caches. The dynamic tail moves into the request contents.
  - Skip prompts below `min_tokens`. Any API error sends the request uncached.
- [ ] **Tests** (fake client):
  - the default request is byte-identical to today's; `bust` makes each system instruction unique
  - `stable_layout` only moves the two lines, and both instructions still resolve with a real state
  - explicit mode builds the expected request, shares one cache across sessions, skips small prompts and falls back on error
  - `cached_tok` is parsed
- [ ] **Dev script** `phase_9_3.py`: three LakeQuest questions in fresh sessions per mode; print prompt and cached tokens per call.

## Phase 9.4 — LakeQuest Evaluation: Prefix Cache

**Goal:** the cached-token share, cost and latency effect of each prefix mode, and a decision on the default mode and layout.

- [ ] **Runs:** `mode=bust` (baseline), `implicit` (today), `implicit` with `stable_layout`, and `explicit` with `stable_layout`, all with the other caches off.
- [ ] **Record:**
  - the standard set
  - the cached share of input tokens per pass and per call position (first call vs. later calls)
  - tokens and cost, with prices given to the report script
  - invalidation: a changed schema or document summary yields a new prefix, and the number of explicit caches created
  - quality per case vs. baseline, required because `stable_layout` is a prompt change
- [ ] **Report:** a §Prefix section in `caching_report.md`, with the recommended default mode and layout.
- [ ] **Tests:** none unless code changes; confirm `uv run pytest` passes.
- [ ] **Dev script** `phase_9_4.py`: print the cached-token share per call position for two run folders.

## Phase 9.5 — Retrieval Result Cache

**Goal:** a repeated search (same query, filters, `k`, settings and documents) skips the query embedding and the ranking, and returns identical passages (design §6).

- [ ] **Config:** a `cache.retrieval` block with `enabled: false`, `embeddings: true`, `ttl_s: 86400`, `max_entries: 5000` and `scope`.
- [ ] **Implementation** (`tools.py`, `retrieval.py`):
  - `search_documents` checks the result cache first. Its key is scope, `docs_fp`, normalised query, `source`, `section`, effective `k`, retrieval settings, embedding settings and reranker id (none today). The value is the ranking plus citation metadata.
  - On a hit, `retrieval.hydrate(ids)` (factored out of `search`, no behaviour change) reads the text, and the existing truncation and notes apply.
  - On a miss: query-embedding cache, then search, then store.
  - Per-layer hit and miss counters go into `state["cache"]`.
- [ ] **Tests:**
  - a hit returns the same ids, order, text, truncation and notes as uncached
  - any change of filter, `k`, setting or `docs_fp` misses
  - an embedding is reused across filters; no passage text is stored
  - scope isolation; fallback; off is identical
- [ ] **Dev script** `phase_9_5.py`: the same search twice on the LakeQuest bundle (offline); print miss → hit and the milliseconds.

## Phase 9.6 — LakeQuest Evaluation: Retrieval Cache

**Goal:** what the retrieval cache saves per search and per turn, with rankings proven identical.

- [ ] **Runs:** baseline vs. `retrieval.enabled=true`.
- [ ] **Record:**
  - the standard set
  - result and embedding hit rates per search call and per pass
  - the `tool` stage p50
  - invalidation: the document stale probes must miss
  - correctness: recompute 20 % of result hits uncached; the rankings must be identical
- [ ] **Report:** a §Retrieval section in `caching_report.md`.
- [ ] **Tests:** none unless code changes; confirm `uv run pytest` passes.
- [ ] **Dev script** `phase_9_6.py`: the correctness recompute on a run folder.

## Phase 9.7 — Caches Combined and Final Report

**Goal:** each cache alone and the caches stacked, on one workload, ending in a recommendation.

- [ ] **Runs** (each a fresh process, in shuffled order):
  - **baseline:** all off, `prefix.mode=bust`
  - **independent:** response, prefix (the mode chosen in 9.4), retrieval
  - **cumulative:** response → response + retrieval → response + retrieval + prefix
- [ ] **Record:** the standard set for every arm, and interactions: response hits remove searches, so the retrieval gain on top of the response cache is measured, not assumed.
- [ ] **Final report:** complete `caching_report.md`.
  - **Tables:** unique (cold) overhead, repeat (warm) benefit, and a projection for repeat ratios of 0, 10, 30 and 50 %.
  - **Results:** hit rates, quality, invalidation.
  - **Recommended defaults:** app `scope: session`; shared datasets `scope: dataset`.
  - **Caveat line.**
- [ ] **Docs:** a concise caching section in `README.md` with the run commands. Record the chosen threshold and prefix mode in `caching_design.md`.
- [ ] **Tests:** none unless code changes; confirm `uv run pytest` passes.
- [ ] **Dev script** `phase_9_7.py`: print the arm × pass matrix (p50, hit rate, quality) from the run folders.

---

## Out of Scope

- Persistence across restarts and a shared cache across Cloud Run instances.
- Multi-turn context in the LakeQuest evaluation (the follow-up bypass is unit-tested only).
- A reranker, SQL-result caching, HybridQA runs and turning any cache on by default in the app.
