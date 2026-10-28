# Latency Report — shipments_v1 (Sprint 8, Phase 8.2)

Measured with `agent.latency_trace=true` on `evals/datasets/shipments_v1.yaml` (6 cases × 5 repeats = 30 turns per run), Gemini 3.5 Flash-Lite, temperature 0, Gemini embeddings, hybrid retrieval, in-process, one query at a time, 2026-10-28. Method and definitions: [latency_evaluation_design.md](latency_evaluation_design.md). Run folders are in `db-doc-qa/runs/` (git-ignored).

## 1. Findings

1. **Model calls are the bottleneck.** They are 47–66 % of the turn for every case except the refusal, and the time scales with the *number* of calls (1 to 6 per turn), not with their size: one call takes about 0.7 s whatever its input.
2. **The intent classifier costs about 0.8 s per turn**, a serial call that is 11–36 % of the turn. Turning it off cut the median query time by **22 %** (3.66 s → 2.84 s), beyond the run-to-run spread, at the cost of one routing deviation in 30 turns.
3. **The tail comes from one case.** `no_policy_refrigerated` (a record with no matching policy) makes 6 model calls and 5 tool calls, uses about 19.5k input tokens and takes 7.0 s, against 2.6–4.2 s for the other cases.
4. **A `search_documents` call costs 0.31 s, and that is almost all the query embedding** (312 ms); the search itself takes under 1 ms. SQL is under 10 ms.
5. **`max_top_k=4` and BM25-only are not worth it** for latency (§5).
6. Differences below about **0.5 s (13 %) in the median** are within the run-to-run spread of this sample.

## 2. Runs

| Run | Folder (under `db-doc-qa/runs/`) | Change from baseline |
|---|---|---|
| Baseline A | `20261028-003527_lat_base_a` | — |
| Baseline B | `20261028-003745_lat_base_b` | — (back to back with A) |
| No intent | `20261028-004032_lat_no_intent` | `agent.intent_enabled=false` |
| Top-k 4 | `20261028-004226_lat_topk4` | `agent.retrieval.max_top_k=4` |
| Retrieval, BM25 | `20261028-004450_lat_ret_bm25` | `bm25_only.yaml`, `agent.latency_trace=true`, `eval.repeats=5` |
| Retrieval, hybrid | `20261028-004452_lat_ret_hybrid` | same, with `agent.retrieval.mode=hybrid` |

All agent runs: 0 errors, `fail_under` thresholds met. "Baseline" below pools A and B (60 turns) unless a run is named.

## 3. Baseline stage breakdown (design §6, step 1)

`shipments_v1` tags only `conflict`, `gap` and `safety`, so the three untagged cases stand for the routes. Values are the median over 10 turns (2 runs × 5), in seconds. Stage medians do not add up exactly to the median total.

| Case (route) | Total | `query_s` | docs build | intent | model | tool | model calls / tool calls | Input tokens | Share of total: model / tool / intent / docs build |
|---|---|---|---|---|---|---|---|---|---|
| `db_location` (database) | 3.2 | 2.6 | 0.64 | 0.87 | 1.68 | 0.00 | 2 / 1 | 4.2k | 52 % / 0 % / 27 % / 20 % |
| `doc_express_threshold` (document) | 3.3 | 2.7 | 0.64 | 0.80 | 1.56 | 0.31 | 2 / 1 | 4.9k | 47 % / 9 % / 24 % / 19 % |
| `hybrid_delayed_express` (hybrid) | 4.9 | 4.2 | 0.64 | 0.80 | 3.09 | 0.34 | 3 / 2 | 7.3k | 63 % / 7 % / 16 % / 13 % |
| `conflict_international` (conflict) | 4.6 | 3.9 | 0.62 | 0.80 | 2.73 | 0.30 | 3 / 2 | 7.2k | 60 % / 7 % / 17 % / 14 % |
| `no_policy_refrigerated` (gap) | 7.6 | 7.0 | 0.62 | 0.85 | 5.09 | 1.06 | 6 / 5 | 19.5k | 66 % / 14 % / 11 % / 8 % |
| `refuse_write` (safety) | 2.2 | 1.6 | 0.62 | 0.80 | 0.79 | 0.00 | 1 / 0 | 2.0k | 35 % / 0 % / 36 % / 28 % |

- **Ingestion-inclusive vs query:** the "docs build" column (about 0.64 s, plus 0.02 s data build) is paid by every evaluated turn because the harness builds a new session per turn; it is not part of `query_s`. In the app it is paid once, on the first question of a session.
- **Baseline totals:** `query_s` p50 3.66 s / p95 7.42 s; `latency_s` p50 4.30 s.
- **Model call cost is flat:** 172 model calls, median 0.72 s; the lower half by input tokens has a median of 0.72 s and the upper half 0.75 s.
- **First turn of a run** (`db_location`): 3.77 s and 3.59 s, against a 3.1–3.2 s median for the same case later. That is about **+0.5 s, mostly in the model stages** (2.0–2.2 s vs 1.7 s) plus 0.08 s in the docs build; the intent stage is flat (0.85 s vs 0.82 s). Only two observations, so treat it as indicative.

## 4. Run-to-run spread

| | Baseline A | Baseline B | Spread |
|---|---|---|---|
| `query_s` p50 / p95 | 3.31 / 7.54 | 3.77 / 7.00 | 0.46 s on p50 (13 % of the mean) |
| `latency_s` p50 | 4.01 | 4.40 | 0.39 s |
| `section_recall` / `citation_precision` / `answer_contains` | 1.00 / 1.00 / 0.97 | 0.95 / 0.96 / 1.00 | one turn out of 20–30 |

Quality also moves by one turn between identical runs, so the guardrail below allows one repeat of a case. A p95 over 30 turns is a description of this sample, not a tail estimate.

## 5. Experiments (one lever per run)

**Acceptance rule (design §6, step 4):** `query_s` p50 must improve beyond the baseline spread, and per case `tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision` may not drop by more than one repeat of five (0.2). `refuse_write` must still refuse without calling a tool.

| Experiment | `query_s` p50 | `query_s` p95 | Mean model calls / input tokens per turn | Guardrail | Verdict |
|---|---|---|---|---|---|
| Baseline (A, B pooled) | 3.66 (3.31, 3.77) | 7.42 | 2.87 / 7.8k | — | — |
| `intent_enabled=false` | **2.84** (−0.82, −22 %) | 6.50 (−0.92) | 2.90 / 7.8k | `db_location` `tool_sequence_match` 1.00 → 0.80 (1 of 5 turns also called `search_documents`); every other per-case value is equal, higher, or at most 0.03 lower (`no_policy_refrigerated` `citation_precision` 0.96 → 0.93); `refuse_write` refuses, 0 tools | **Accepted, conditionally** |
| `retrieval.max_top_k=4` | 3.34 (−0.32, −9 %) | 8.14 (+0.7) | 2.97 / 8.2k | `no_policy_refrigerated` `answer_contains` 0.90 → 0.80; others unchanged | **Rejected** |
| Retrieval only, BM25 vs hybrid | search 0.0 ms vs query embedding 312 ms + search 0.0 ms (per query, 20 queries each) | — | no model calls | raw-question retrieval: `section_recall` 0.38 (BM25) vs 0.50 (hybrid) | **Rejected** as a latency fix |

- **`intent_enabled=false`: conditionally accepted.** The gain is the largest measured and exceeds the spread, and the guardrail is met on the plan's rule (exactly one repeat of one case). It is not a free change, though: the single deviation is the routing mistake the classifier prevents ("database" hint → do not search documents), n=5 per case is small, and the run-to-run spread of the quality metrics is about the same size. **Do not change the app default until a repeat run (≥3 × 5 turns) shows the deviation rate is still low.**
- **`max_top_k=4`: rejected.** The 9 % median gain sits inside the baseline spread (3.31–3.77), p95 got worse, and input tokens did not fall (the agent searched a little more), consistent with model time being flat in prompt size.
- **BM25 vs hybrid:** BM25 removes the 0.31 s embedding per search, but recall drops. Because the agent writes its own queries, the raw-question recall above is only indicative, so no end-to-end BM25 run was made.

## 6. Hypotheses (design §6, step 2)

| # | Hypothesis | Status | Evidence |
|---|---|---|---|
| H1 | Document embedding at session setup is a large cost | **Confirmed for evaluation turns; not measured for the app** | 0.64 s per turn, 8–28 % of the total, because each evaluated turn builds a new session. In the app it is paid once per session (first question). Query time excludes it |
| H2 | The intent call adds a serial round trip | **Confirmed** | 0.80–0.87 s per turn, 11–36 % of the total; turning it off saved 0.82 s (22 %) in the median |
| H3 | The model-call count, not per-call speed, drives latency | **Confirmed** | Calls per case 1 / 2 / 2 / 3 / 3 / 6 track `query_s` (1.6 / 2.6 / 2.7 / 4.2 / 3.9 / 7.0 s); one call is about 0.7 s whatever the prompt |
| H4 | Input tokens grow per call and slow the model | **Rejected as a latency lever** | Per-call time is flat in input tokens (0.72 s vs 0.75 s for the lower and upper half); `max_top_k=4` gave no gain beyond the spread |
| H5 | The query embedding is a noticeable part of the tool time | **Confirmed** | 312 ms per query embedding vs <1 ms for the search; `search_documents` tool stage 0.30–0.34 s per call; 1.06 s in the 5-tool-call case |
| H6 | A new `genai.Client()` per classification adds connection setup | **Not tested** | Every turn creates a client, so first-turn vs later turns cannot separate it; the first-turn `intent` stage was not slower (0.85 s vs 0.82 s). Needs a one-off micro-benchmark |
| H7 | Retries or errors inflate the tail | **Rejected on this dataset** | 0 errors in 120 agent turns; `run_sql` was never called twice in the 60 baseline turns. The tail is explained by H3 (extra searches in the gap case), not by retries |

## 7. Ranked next fixes (not implemented in this sprint)

Ranked by expected gain × ease ÷ quality risk.

| # | Fix | Expected gain | Cost / risk | Next step |
|---|---|---|---|---|
| 1 | **Drop or shrink the intent call** (`intent_enabled=false`, or a faster `model.intent`) | About 0.8 s (22 %) per turn | Config only. Risk: more over-calling of tools (1 of 30 turns) | Repeat with ≥3 × 5 turns, including a 4th and 5th database/document case; then decide the app default. Also try `agent.model.intent` with a smaller model, which keeps the hint |
| 2 | **Fewer model calls in the gap case** (prompt: when a record has no matching policy, search all documents in one step instead of re-searching) | Up to 2–3 s on the slowest cases, which sets the p95 | Prompt change in `INSTRUCTION` rule 2; high risk to "gap" and conflict detection | Change one rule, rerun, and apply the same guardrail (all four metrics for `gap` and `conflict`) |
| 3 | **Build documents at upload, not on the first question (H1, app)** | About 0.64 s on the first turn of each session; none on later turns | Medium code change in the app/session flow | Measure the first-turn cost in the deployed app first |
| 4 | **Cache query embeddings (H5)** | 0.31 s per repeated query only | Low gain, since the agent rarely repeats a query exactly | Skip unless the real workload shows repeats |
| 5 | **Reuse one `genai.Client` (H6)** | Unknown, probably tens of ms | Small code change in `intent.py` | Micro-benchmark client construction before changing |
| — | Not worth doing for latency | — | `max_top_k` reduction (no gain), BM25-only (lower recall) | — |

## 8. Limitations

- 30 turns per run and 6 cases: the medians are reliable to about ±0.5 s, the p95 is descriptive, and one quality turn is 0.03 overall (0.2 on a case).
- In-process timings only: no browser, UI→API transport or Cloud Run cold start (design §8). The embedding provider here is Gemini, so the 0.31 s depends on network distance and quota.
- Stage times come from event arrivals, so tools called together are one entry and the first model stage includes a few milliseconds of ADK overhead.
- Gemini latency varies with time of day and load; all runs were made within about 10 minutes of each other.
