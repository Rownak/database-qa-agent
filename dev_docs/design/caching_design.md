# Caching Design — Database & Document Q&A Agent

## 1. Goal and what the measurements say

Cut latency and model cost for **repeated** work without changing answers, using three independent caches that can each be switched on in YAML, and measure each one on the LakeQuest sample with the existing latency tracing. Design only; nothing here is implemented.

**Where a LakeQuest turn spends its time** ([latency_report_lakequest.md](latency_report_lakequest.md), 120 turns, seconds unless noted) and what each cache can reach:

| Stage | Measured | Reached by |
|---|---|---|
| Whole turn | p50 3.63 (hybrid 5.02, table 2.81, document 3.36) | response cache (a hit skips everything below) |
| Intent classifier | p50 1.09 | response cache (lookup runs before it) |
| Model calls | 3.4 per hybrid turn, ≈ 1 s each; `llm` stage p50 2.22 (hybrid 3.37) | response cache; prefix cache (cost, maybe a little latency) |
| Query embedding inside a search | ≈ 0.32 | retrieval cache |
| Search itself (BM25 + dense + RRF) | 12.5 ms filtered, 24.3 ms over all 2,481 chunks | retrieval cache (small) |
| SQL | ≤ 0.5 ms | not worth caching |
| Input tokens | ≈ 7.0k on the first call, 15–25k per turn | prefix cache (cost) |

So the response cache is the only one that can remove seconds, the retrieval cache removes about 0.34 s per repeated search (1.08 searches per turn), and the prefix cache is mostly a cost lever. SQL results are deliberately **not** cached.

**Non-goals:** persistence across restarts, a shared cache across Cloud Run instances, a vector database, new dependencies, caching of SQL results or of passages' text.

## 2. Verified facts (installed SDK, model, ADK)

Checked on 2026-11-02 against `google-genai` 2.31.0, `google-adk` 2.12.0 and `gemini-3.5-flash-lite` with scratch probes (not kept in the repo; about 100 short requests, a few cents).

| Question | Finding |
|---|---|
| Is cache use visible? | Yes. `usage_metadata.cached_content_token_count` (SDK) and `usageMetadata.cachedContentTokenCount` in ADK events. Our trace records only prompt and output tokens today. |
| Does the model cache implicitly? | **Yes, best-effort.** An identical 11,673-token request repeated 5× reported 8,166 cached tokens from the 2nd request on; 5,152 tokens reported 2,033; 2,295 and 3,587 tokens reported none. Cached amounts come in blocks of ≈ 2,040 tokens, and the practical minimum sits between 3.6k and 5.2k tokens (the docs give 4,096 for Gemini 3 Pro). The shipments prompt (≈ 2k tokens) is too small; LakeQuest (≈ 7k) is large enough. |
| Is a shared prefix reused when only the final question differs? | **Not observed.** An 11.7k-token system prefix with a different question each time: 0 hits in ~40 requests (gaps 2–15 s, system or user layout). This is weaker than the provider's "keep the common prefix first" advice, so it is treated as an open measurement, not an assumption. |
| What happens on the real agent path? | LakeQuest, 6 turns (first call 7,018–7,054 tokens): **44 % of input tokens cached** (24–66 % per turn). Hits came on calls 2…n of a turn (4–6k cached) and on repeats of an earlier question; the first call of a brand-new question got none, though ≈ 6.9k tokens of instruction and schema were shared with earlier turns. |
| Is explicit caching available? | Yes. `client.caches.create` accepted prefixes from 1,034 to 11,665 tokens, and a request using it reported 11,665 of 11,673 tokens cached. |
| Does ADK's own context caching fit? | `ContextCacheConfig` (experimental) is set on an `App`, starts on the second turn of a session at the earliest, and has a model minimum (2,048 for 2.5, 4,096 for 3). Our evaluation uses one question per session and `AgentSession` builds `Runner(agent=…)`, not an `App`, so it would never trigger there. |
| Can a callback short-circuit a turn? | Yes. A `before_agent_callback` that returns content ends the invocation with that single event and no model call; callbacks can be a list, run in order. |
| Can a callback see the final answer? | Yes. In `after_agent_callback`, `callback_context.session.events[-1]` holds the final model event with the answer text. |

**Context caching is not KV-cache control.** Gemini's implicit and explicit caching reuse the provider's already-processed *input prefix*, keyed by content; we can influence it only through prompt layout, request identity and the lifetime of an explicit cache resource. We cannot pin, evict or address KV blocks, or read the attention tensors. That level (prefix caching with cache salts, slot save/restore) exists only on self-hosted inference servers, which this project does not run for generation, so it is out of scope.

## 3. Common design

### 3.1 Where each cache sits

```text
question
  └─ ensure_data (build data.db / docs.db, unchanged)
      └─ [1] response-cache lookup ─ hit ─▶ answer (no intent call, no model call, no tools)
          └─ intent classification (split into its own callback, after the lookup)
              └─ ADK agent: model calls ········ [2] provider prefix cache (implicit, no code)
                    └─ search_documents ········ [3] retrieval cache: result ranking → query embedding
              └─ final answer ─ after_agent_callback ─ [1] store (eligible turns only)
```

Both agent hooks already exist in the code (`before_agent_callback=make_ensure_data(cfg)`); the lookup is a second callback in the list and the intent step becomes a third.

### 3.2 Fingerprints and keys

| Fingerprint | Computed from | Used by |
|---|---|---|
| `data_fp` | sha256 of `data.db` bytes (memoised per path, size, mtime) | response |
| `docs_fp` | sha256 of `docs.db` bytes (documents, chunks and embeddings, so the documents, the chunking and the embedding model are all covered) | response, retrieval |
| `cfg_fp` | canonical JSON of `AgentConfig` without `cache` and `latency_trace`, plus the sha256 of the **resolved instruction text** and **intent prompt text** (file contents, not paths), the tool declarations, and a `CACHE_SCHEMA_VERSION` constant | response |

| Cache | Key | Value |
|---|---|---|
| Response | `scope_id`, `data_fp`, `docs_fp`, `cfg_fp`, normalised question (exact tier) or its embedding (semantic tier) | answer, intent, tool-call summary, question embedding |
| Retrieval result | `scope_id`, `docs_fp`, normalised query, `source`, `section`, effective `k`, retrieval settings (`mode`, `top_n`, `rrf_k`, `min_dense_sim`), embedding settings, reranker id (none today) | ranked `(chunk_id, score, matched_by, dense_sim)`, `candidates`, `dense_used`, `sources`, citation metadata |
| Query embedding | `scope_id`, embedding provider, model, dim, task type, normalised text | vector |
| Prefix | provider-managed (content of the request) | — |

Normalisation: NFKC, case-fold, collapsed whitespace. Keys are hashes; raw questions are stored only inside response entries (for the entity guard) and never logged.

### 3.3 Session and access isolation

- **`scope: session` (default, and the app).** `scope_id` is the ADK session id, so entries are unreachable from any other session and are dropped when the session ends (`AgentSession.close`, `sessions.end_session`, the idle sweep). Uploaded data is private to its session, so this is the only safe default.
- **`scope: dataset` (opt-in).** `scope_id` is empty: sessions share entries only when `data_fp`, `docs_fp` and `cfg_fp` are all identical. Use it where every user of the data has the same access (bundled sample, evaluation bundles, a single-tenant deployment). Each LakeQuest case is a new session, so the evaluation needs it.
- Storage is process memory only, bounded by `max_entries`. Answers never go to logs (the rule in `logs.py`). The retrieval cache stores ids, scores and citation metadata, not passage text, so passages are re-read from `docs.db` on every hit. On Cloud Run each instance has its own cache; restarts clear it, and hit rates dilute with more instances.

### 3.4 TTL and invalidation

| Event | Response | Retrieval / embedding | Prefix |
|---|---|---|---|
| Data file added, removed or replaced | new `data_fp` → old entries unreachable | unaffected | `schema_summary` changes, prefix changes automatically |
| Document added, removed or replaced | new `docs_fp` → unreachable | new `docs_fp` → unreachable (embeddings unaffected) | `docs_summary` changes |
| Chunking, embedding or retrieval settings | via `cfg_fp` and `docs_fp` | via key fields / `docs_fp` | none |
| Model, temperature, instruction or intent prompt edited | via `cfg_fp` | unaffected | provider keys on model and content |
| Code or cached-value format change | bump `CACHE_SCHEMA_VERSION` | same | none |
| Session ends / idle sweep | dropped (session scope) | dropped | provider-managed |
| Follow-up question (conversation context) | **bypassed**, never matched | unaffected: keyed by the query the agent wrote, not by history | history is part of the request |
| Time-relative question | bypassed | unaffected | none |
| Manual | `clear()`; setting a toggle off bypasses without clearing | same | — |

TTLs only bound memory and staleness that fingerprints cannot see: response **1 h**, retrieval and embedding **24 h** (their keys are content-addressed), LRU on `max_entries`. The provider's implicit cache is out of our hands; an explicit cache (§5) gets a 1 h TTL.

### 3.5 Configuration

A new frozen `CacheConfig` in `AgentConfig.cache` (`extra="forbid"`, like the other models). Everything defaults **off**, so the app, the deployed agent and every existing config behave exactly as before. It is recorded in `config.yaml` and in `state["agent_config"]` (tools read it from there) and is changed with the existing dotted overrides, for example `agent.cache.response.enabled=true`.

```yaml
agent:
  cache:
    response:                    # §4
      enabled: false
      semantic: false            # false = exact tier only (no embedding call)
      threshold: 0.97            # cosine of question embeddings
      entity_guard: true
      shadow: false              # log what would hit, never serve (threshold sweeps)
      ttl_s: 3600
      max_entries: 1000
      scope: session             # session | dataset
    retrieval:                   # §6
      enabled: false
      embeddings: true           # also cache query embeddings
      ttl_s: 86400
      max_entries: 5000
      scope: session
    prefix:                      # §5
      bust: false                # evaluation only: defeats provider caching
```

### 3.6 Fallback behaviour

A cache is an optimisation and must never decide whether a question is answered. Any exception, corrupt or oversized entry, failed embedding, or missing fingerprint is treated as a **miss** (or a bypass), logged by exception type only, and the turn continues uncached. A failed store is ignored. If the semantic embedding fails, the exact tier still applies. With a toggle off the code path is skipped entirely.

## 4. Semantic response cache (query → final answer)

**Lookup, in order.** (1) Eligible? (2) exact tier: hash of the normalised question inside the namespace, with no model or embedding call. (3) If `semantic` is on: embed the question (the embedding cache applies; ≈ 0.32 s on a miss), take the most similar entry of the namespace by brute-force cosine (≤ 1,000 × 768 floats), and hit only if similarity ≥ `threshold` **and** the entity guard passes.

**Eligibility.**

| | Rule |
|---|---|
| Lookup | cache on; **first user turn of the session** (a follow-up depends on history); question non-empty and ≤ 2,000 characters; no time-relative word (today, yesterday, now, current, latest, this week/month/year, recent); `data_fp` and `docs_fp` available; no `load_error` in the state (partial data) |
| Store | same preconditions; the turn finished without an exception; non-empty answer; no tool call ended in an error; no `truncated` SQL result |

**Entity guard.** Embeddings barely distinguish "client 252" from "client 253", which is the dangerous failure. A semantic hit therefore also requires the identifier-like tokens of both questions to be equal: numbers (ids, amounts), quoted strings, file-name-like tokens, and capitalised words that are not at a sentence start (names). A paraphrase that rewrites `$10,000` as `10000` costs a hit, never a wrong answer.

**Threshold.** Default 0.97, to be set from data: with `shadow: true` every lookup logs its nearest similarity, the guard result and the nearest entry's source case, so a sweep over 0.90–0.99 needs no extra model calls (§7).

**Entry and hit.** An entry holds the answer, the intent, the question embedding and a **tool-call summary** (names, SQL text, and for searches only source, section and chunk id; no rows, no passage text). On a hit the callback returns the stored answer as content and writes `state["cache"] = {"response": "hit_exact" | "hit_semantic", "similarity", "age_s"}` plus the stored calls; `TurnResult` gets `cache` and `calls` from it, so every existing metric applies unchanged and the UI can label the answer as cached. The answer event of a hit is not an `llm` stage in the trace.

**Cost and benefit.** A semantic lookup adds one embedding (t_l ≈ 0.32 s) to every miss. With average turn time T ≈ 3.6 s and hit rate p, the expected gain per query is p·T − t_l, so semantic mode pays only above p ≈ t_l / T ≈ **9 %**. The exact tier costs microseconds and has no break-even.

**Risks.** A wrong hit (near miss); one sampled answer replayed for every later asker (consistent, but variety is lost); changes invisible to the fingerprints (none for local files today, relevant if live APIs are added).

## 5. Prompt/prefix caching

**Position: use the provider's implicit caching, measure it, change the prompt only where it is cheap, and treat explicit caching as an optional later arm.**

What the request is made of, and what is stable:

| Part | Stability |
|---|---|
| Instruction rules (tools, routing, answer format, safety) | static per config |
| `schema_summary`, `docs_summary` | stable per dataset (LakeQuest: the bulk of ≈ 7k tokens) |
| `{intent?}`, `{load_error?}` | change per turn **and sit mid-prompt**, so the static rules after them cannot be shared across intents |
| Tool declarations | static |
| Question, tool calls and results | change every turn |

- **Prompt edits (one guardrail run).** Move `{intent?}` and `{load_error?}` to the end of the instruction and keep the summaries deterministic (sorted, no timestamps). It is a prompt change, so it needs the same quality guardrail as Phase 8.2. The expected benefit is uncertain: the probes saw reuse mainly on extended or repeated prompts, not on a shared prefix under a new question.
- **Measure, don't assume.** Add `cached_tok` to each `llm` stage of the trace and report the cached share of input tokens per pass. Implicit hits give a billing discount, not a latency guarantee. Model-call time was flat in input size in the shipments traces (median 0.72 s for the lower half of calls by input tokens, 0.75 s for the upper half), so prefill is not the dominant part and any latency effect must be measured, not claimed.
- **The off switch.** Implicit caching cannot be disabled, so the uncached baseline defeats it: `prefix.bust: true` puts a unique line first in the system prompt of every model call (a `before_model_callback`, evaluation only, never in production). That also removes hits within a turn, which is the point of the baseline.
- **Explicit caching (optional arm).** Worth trying only if implicit reuse across questions stays near zero and cost matters: one `cachedContents` resource per `(model, cfg_fp, docs_fp, data_fp)`, created lazily for shared datasets, 1 h TTL with refresh, passed through `generate_content_config`. It needs the cached prefix to hold the stable instruction and tools and the per-turn fields to move into the user message, and ADK's `ContextCacheConfig` cannot be used as is (§2). It also adds a resource lifecycle and storage cost, so it is not part of the first round.
- **Small prompts.** Below ≈ 4k tokens (the shipments sample) implicit caching does not engage, so those datasets get nothing from this cache.

## 6. Retrieval/reranking cache

Placed in `search_documents`, in two layers:

1. **Result cache** (key in §3.2). Checked first. On a hit there is no embedding call, no BM25, no dense scan and no fusion: the tool reads the passage text for the cached ranked ids from `docs.db`, then runs the existing truncation, filter notes and untrusted-text notice unchanged. Saves ≈ 0.34 s per hit.
2. **Query-embedding cache** (key in §3.2). On a result miss, it saves the embedding call when the same text comes back with a different filter or `k`. It is also used by the semantic response lookup.

**Content-addressed, so no explicit invalidation.** `docs_fp` is in the key; a changed document, chunking or embedding model produces new keys, and the TTL only bounds memory. Retrieval-setting fields are in the key, so an ablation (`mode=bm25`, another `top_n`) cannot hit another configuration's entries.

**Reranking.** No reranker exists today (hybrid BM25 + dense + RRF only). If one is added, its id, version and parameters join the key, and its output order and scores are cached the same way, since it would be the most expensive deterministic stage.

**Expected hits.** Identical agent-written search strings: repeats of a user question (the model does not always write the same query, which is why this is measured), repeated searches inside one turn (up to 11 in HybridQA), and shared documents under `scope: dataset`. Paraphrased user questions will hit less here than in the response cache. Gain is bounded by ≈ 0.34 s × searches per turn (1.08 on LakeQuest).

## 7. Evaluation on LakeQuest

Reuses the existing runner, `EvalCase` schema, per-case `state_file`, per-tag metrics and `agent.latency_trace`. **The pipeline stays dataset-agnostic**: everything LakeQuest-specific is in a derived dataset and a config.

### 7.1 Workload: duplicate and vary the questions and answers

The 40 benchmark cases are all unique, so a cache cannot show any effect on them. A derived dataset (`benchmark/lakequest_prepared/bank_sample_cache/dataset.yaml`, git-ignored, built once by a dev script) adds repeats with the **same `expected` answers**, in this order (YAML order is run order):

| Pass | Turns | Content | Tag | Question it answers |
|---|---|---|---|---|
| U unique | 40 | the original cases | `pass_unique` | cost of a cache when nothing repeats (cold; all misses by construction) |
| E exact | 20 | verbatim copies of half A (10 hybrid, 5 table, 5 document) | `pass_exact` | benefit of exact repeats |
| P paraphrase | 20 | paraphrases of half B (the other 20) | `pass_paraphrase` | benefit of semantic matching |
| N near miss | 10 | an original with one entity swapped (another client id and name, or another policy id) | `pass_near_miss` | false hits; **must be 0** |
| S stale | 4 | 2 data and 2 document probes run on mutated copies of the bundle through per-case `state_file`, with `expected` set to the post-change truth | `pass_stale` | stale answers; **must be 0** |

Construction rules: paraphrases are generated once with an instruction to keep every id, name and number, then checked (identifier tokens equal to the original, plus a manual read) and **frozen in the YAML**, so every arm sees identical text. Near-miss cases need no expected answer (we score "was it served from cache?"). Stale copies are made from copies of `bank.db` (delete 10 transactions of a client behind a count question, so the correct count changes) and of `docs.db` (remove one policy that a question cited); the benchmark bundle is never touched, and the changed bytes give a new fingerprint. Halves A and B are disjoint so that exact and semantic hits are measured separately. Because every case is a new session, evaluation runs use `scope: dataset`.

Per arm: 94 turns, roughly 6–10 minutes (fewer on arms where hits skip the model); six arms about an hour. Conversation-context eligibility (a follow-up must bypass) needs two turns in one session, which the dataset schema does not express, so it is covered by unit tests with a fake session.

### 7.2 Arms

Each arm is one CLI run of `configs/eval/lakequest_cache.yaml` (a copy of `lakequest_bank.yaml` with the derived dataset, `latency_trace: true`, `scope: dataset`) plus dotted overrides. Each run is a fresh process, so it starts cold.

| Arm | Response | Retrieval | Prefix | Isolates |
|---|---|---|---|---|
| **B0** | off | off | `bust` on | true uncached baseline |
| **R-exact** | exact tier | off | `bust` | exact response cache |
| **R-sem** | exact + semantic | off | `bust` | semantic response cache |
| **V** | off | on (both layers) | `bust` | retrieval cache |
| **P** | off | off | off (provider default) | prefix caching; this is also today's behaviour |
| **ALL** | exact + semantic | on | off | everything together |

Optional **P2** repeats P after the instruction reorder of §5. **Cold vs warm:** cold is pass U in a fresh process; warm is E, P, N and S afterwards in the same process. Provider-side warmth cannot be controlled, which is why the other arms set `bust`. Run order is shuffled and compared against the ±0.5 s run-to-run spread already measured.

### 7.3 What is measured

| Measure | Source |
|---|---|
| p50 / p95 / max latency per pass, route and arm; `query_s` | existing `latency_s`, `timing`, `by_tag` (pass tags) |
| Stage breakdown, including a new `cache_lookup` stage (only when it ran) | trace stages |
| Hit rates: response (exact / semantic / miss / bypass) per pass; retrieval result and embedding hits per search call; cached share of input tokens | new per-turn counters in `state["cache"]`, `cached_tok`, aggregated in `metrics.json` |
| Model usage: model calls and input / output / cached tokens per turn; classifier calls counted separately | trace |
| Cost | tokens × prices supplied to the report script (the price of this model is not hard-coded); tokens only if none are given |
| Answer quality | the existing metrics (`answer_contains`, `answer_f1`, `object_f1`, `tool_sequence_match`, `section_recall`, `citation_precision`) on the same `expected`; hits carry the stored calls, so none are skipped |
| Stale answers | pass S: served-from-cache count and whether the answer matches the post-change truth |
| False hits | pass N: served-from-cache count; with `shadow`, also how many were blocked by the entity guard |
| Threshold curve | `shadow` logs: for each τ in 0.90…0.99, pass P hits and pass N false hits with and without the guard, using the nearest entry's source case as the label (labels used only in the analysis, never as inputs) |
| Retrieval cache correctness | recompute 20 % of result hits uncached and compare ids and order |

### 7.4 Repeated queries vs unique questions

Never pooled into one percentile. The report has three separate tables: **(a) unique questions** (pass U): the overhead of each cache versus B0 (p50 / p95 delta, and the `cache_lookup` stage); **(b) repeats** (passes E and P): hit rate, latency, tokens and cost saved; **(c) a projection** for repeat ratios of 0, 10, 30 and 50 %, E[T] = (1 − ρ)(T_unique + t_lookup) + ρ·T_hit, built from the measured values in (a) and (b), so the reader sees what a given workload would gain instead of one blended number.

### 7.5 Gates

- **Hard:** zero false hits in pass N and zero stale answers served in pass S, at the chosen threshold with the guard on. Either one fails the arm.
- **Quality:** the cache must not change misses: in pass U, per-case guarded metrics stay within one repeat (0.2) of B0; a hit returns the stored answer, whose metrics are those of the original answer.
- **Exact tier:** at least 95 % of eligible pass-E turns hit. **Semantic tier:** kept only if the threshold gives zero false hits and p·T > t_lookup (§4); otherwise exact-only.
- **Overhead on unique questions:** read from the `cache_lookup` stage, which is precise: ≤ 0.05 s for exact-only, ≤ 0.4 s for semantic (one embedding). The pass-U p50 against B0 is only required to stay inside the ±0.5 s run-to-run spread, since a smaller difference cannot be resolved.
- **Retrieval cache:** 100 % identical rankings on the recomputed sample.

### 7.6 Limits

94 turns per arm on one machine with a non-deterministic model: this shows direction and order of magnitude. The hit rates describe this synthetic repeat mix, not production traffic. Paraphrase quality is a property of the frozen set. The prefix result depends on provider behaviour that is best-effort and may change.

## 8. Minimal code changes (for reference)

| File | Change |
|---|---|
| `data_agent/cache.py` (new) | TTL + LRU store with a lock, fingerprints and key builders, `normalize`, entity tokens, namespace registry with `drop(scope_id)`; numpy only, about 150 lines, no new dependency |
| `data_agent/config.py` | `CacheConfig` models and `AgentConfig.cache` |
| `data_agent/agent.py` | split the callback list into build → response lookup → intent; an `after_agent_callback` that stores; an evaluation-only `before_model_callback` for `bust` |
| `data_agent/tools.py`, `retrieval.py` | result and embedding cache in `search_documents`; `retrieval` gets a `hydrate(ids)` factored out of `search` |
| `data_agent/events.py`, `runner.py` | read `state["cache"]` into `TurnResult.cache` and the stored calls; `cached_tok` on `llm` stages; skip the hit answer as an `llm` stage; `drop(session_id)` on close |
| `evals/run.py`, `__main__.py` | aggregate hit rates and cached share into `metrics.json`; one summary line |
| `dev_test/` | derived-dataset and mutated-bundle builders, paraphrase freeze, arm comparison report |

Unit tests cover keys and normalisation, eligibility (including the follow-up bypass), TTL and LRU, session isolation and `drop`, invalidation by fingerprint, the entity guard, and fallback on every injected exception.

## 9. Open points

- Implicit prefix reuse under a **new** question was not observed; if it stays near zero, the prefix cache is "cost on repeats and within a turn" only, and explicit caching is the only route to cross-question reuse.
- The semantic tier may not pay: it needs about 9 % effective hits to break even, and real repeat rates are unknown.
- A cached answer is one sample of a non-deterministic model; users who re-ask to get a different answer will not.
- Multi-instance Cloud Run needs a shared store to keep hit rates, which this design deliberately does not add.

## Sources

- [Gemini API: Context caching](https://ai.google.dev/gemini-api/docs/caching) (implicit and explicit caching, minimum token counts)
- [Gemini 2.5 models now support implicit caching](https://developers.googleblog.com/es/gemini-2-5-models-now-support-implicit-caching/) (launch announcement)
- Installed `google-adk` 2.12.0 (`agents/context_cache_config.py`, `agents/base_agent.py`) and `google-genai` 2.31.0 (`types.py`)
