# Latency Report — LakeQuest bank sample (Sprint 8, Phase 8.4)

Run `20261028-082919_lat_lakequest` (`db-doc-qa/runs/`, git-ignored): `configs/eval/lakequest_bank.yaml` with `agent.latency_trace=true eval.repeats=3`. It has 40 cases × 3 = **120 turns** (60 hybrid, 30 table, 30 document), 0 errors, `timing.ok` on all 120. Gemini 3.5 Flash-Lite, temperature 0, hybrid retrieval, Gemini embeddings, the intent classifier **on** with the bank prompt (Phase 8.3), prebuilt bundle (no data or document build). Method: [latency_evaluation_design.md](latency_evaluation_design.md); the shipments result is in [latency_report.md](latency_report.md).

**Units and samples.** Route latency and stage times are **seconds** (`latency_s` and the trace, `n` = turns). Search and SQL times are **milliseconds**, measured offline on the bundle with `time.perf_counter()` (median of 3, each including opening a read-only connection); `calls` = recorded tool calls. The offline replay used 120 of the 130 recorded `search_documents` calls (10 had no query, see bullet 1) and all 104 `run_sql` calls. Reproduce with `uv run python -m dev_test.phase_8_4`.

## 1. Route summary (seconds)

| Route | n | p50 | max | p50 w/o intent | avg tool calls | `llm` p50 | `tool` p50 | `intent` p50 |
|---|---|---|---|---|---|---|---|---|
| hybrid | 60 | 5.02 | 13.52 | 3.73 | 2.88 | 3.37 | 0.36 | 1.11 |
| table | 30 | 2.81 | 4.86 | 1.78 | 1.00 | 1.78 | 0.00 | 1.02 |
| document | 30 | 3.36 | 4.88 | 2.23 | 1.03 | 1.87 | 0.34 | 1.11 |
| **all** | **120** | **3.63** | **13.52** | **2.53** | **1.95** | 2.22 | 0.34 | 1.09 |

p95 (from `metrics.json`): hybrid 8.9, table 3.6, document 4.3, all 7.3. "w/o intent" is `latency_s` minus the `intent` stage and is comparable with the earlier intent-off LakeQuest figures (README: p50 2.8 s, p95 6.3 s). The `tool` p50 of 0.00 for table is a timer tick (see Measurement notes), since `run_sql` takes well under a millisecond.

## 2. Routing (classifier vs. case tag)

| Case tag (rows) → predicted | database | document | hybrid | n | correct |
|---|---|---|---|---|---|
| table | 30 | 0 | 0 | 30 | 100 % |
| document | 0 | 30 | 0 | 30 | 100 % |
| hybrid | 0 | 2 | 58 | 60 | 97 % |
| **overall** | | | | **120** | **98.3 % (118/120)** |

- Two classifier outputs were invalid and fell back to `hybrid`. Table and document turns have no `hybrid` predictions, so both fell on hybrid cases, where the fallback counts as correct.
- `tool_sequence_match` (the agent's real tool routing): **0.992** (119/120) with the classifier on, vs **0.975** (39/40) in the earlier intent-off run (`20261026-133728_lakequest_bank_sample`). That is one turn either way.
- The case tags are used only here, to score. They never reach the classifier or the agent.

## 3. Dense retrieval (milliseconds)

| Chunks scanned | calls | search median | search max | dense part median | traced `tool` median | search share of `tool` |
|---|---|---|---|---|---|---|
| ≤ 50 (filtered by `source` / `section`) | 80 | 12.5 | 16.0 | 4.5 | 343 | 3.6 % |
| > 500 (all 2,481, unfiltered) | 40 | 24.3 | 31.6 | 15.4 | 344 | 7.0 % |

- **Not every call scans all chunks.** `retrieval._dense` reads only the rows matching the filter, so 40 of 120 calls scanned all 2,481 chunks (equal to `prepare.json`) and 80 were narrowed by a `source` or `section` filter (one matched no chunk). No call fell in the 51–500 band.
- "Dense part" is the hybrid time minus a BM25-only run of the same call. "Search share" is the median replayed search divided by the median traced `tool` stage of single-call batches, so about 320 ms of every search call is the query embedding (a Gemini API call) and overhead.

## 4. SQL execution (milliseconds)

| Largest table (rows × cols) | class | calls | SQL median | SQL max | traced `tool` median |
|---|---|---|---|---|---|
| client (2,196 × 14) | lookup | 62 | 0.14 | 0.27 | 0 |
| bank_account (1,508 × 8) | aggregate | 18 | 0.14 | 0.19 | 0 |
| bank_account (1,508 × 8) | lookup | 16 | 0.15 | 0.23 | 0 |
| transaction (125,742 × 18) | aggregate | 4 | 0.15 | 0.17 | 0 |
| merged groups with < 3 calls | mixed | 4 | 0.35 | 0.52 | 15 |

Groups with fewer than 3 calls are merged (mostly `client` ⋈ `bank_account` joins). A query with an aggregate is tagged `aggregate` even if it also joins. The traced `tool` column is quantised to the timer tick, so 0 means "under one tick".

## 5. Main bottlenecks

- **Hybrid turns, and model calls in them, dominate.** Hybrid turns have p50 5.0 s (p95 8.9 s, max 13.5 s) and average 2.88 tool calls and 3.4 agent model calls (up to 8; table and document turns make 2), plus the classifier call; the `llm` stage is about two thirds of the turn (3.37 of 5.02 s). The three slowest turns (9.2, 12.4, 13.5 s) are hybrid. 10 of 130 `search_documents` calls (7.7 %; 9 hybrid, 1 document) had no `query` and were rejected by the tool, each costing an extra model round trip. `LAKE_INSTRUCTION` rule 4 describes the call with `source` and `section` but no query, which is a likely cause (not verified).
- **The classifier costs about a third of a turn and did not change routing.** The `intent` stage has p50 1.09 s (max 2.88 s), 30 % of a turn at the median and 36 % for table questions (1.02 of 2.81 s). Routing accuracy is 98.3 %, but `tool_sequence_match` moved by one turn (0.992 vs 0.975), because the agent already chose the right tools without the label.
- **The dense scan is not a bottleneck at 2,481 chunks.** A search takes 12.5 ms filtered and 24.3 ms unfiltered, 3.6–7 % of a ~344 ms tool call; the query embedding is the cost. The dense part grows from 4.5 to 15.4 ms with the chunks scanned, so if it stays linear it would reach roughly 150 ms at 25,000 chunks (an extrapolation, not measured).
- **SQL is negligible.** Every recorded query ran in about 0.5 ms or less (max 0.52 ms), including aggregates joined to the 125,742-row `transaction` table (the bundle has indexes on the id and join columns).

**Fix candidates (not implemented):** leave the classifier off for LakeQuest, or give it a smaller `model.intent` (saves up to ~1.1 s per turn; untested); reword `LAKE_INSTRUCTION` rule 4 to always include a query; look at fewer model calls in hybrid turns; if the corpus grows by an order of magnitude, filter chunks before the scan or use an index.

## Measurement notes

- This run's trace was recorded with `time.monotonic()`, which ticks about every 15.6 ms on Windows, so its stages under ~16 ms read 0 or 0.016 s (hence the traced `tool` column above). The offline search and SQL times use `time.perf_counter()`. The tracing code was switched to `perf_counter()` after this run, so new traces resolve short stages (a single-call `run_sql` stage now reads about 3 ms, including dispatch overhead); the stage conclusions here are unaffected, since they rest on stages of 100 ms and more.
- The offline search replays each recorded call with a fixed random unit vector instead of the real query embedding (the scan cost does not depend on the vector). Similarity thresholds therefore do not matter here, but the result list is meaningless.
- Tool batches are matched to calls by order; only batches holding exactly one call are compared with the offline timings.

*Caveat: an internal 40-case sample with 3 repeats on one machine; the numbers are descriptive, not a production latency estimate.*
