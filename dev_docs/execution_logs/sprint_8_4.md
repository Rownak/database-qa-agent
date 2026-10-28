# Sprint 8 — Phase 8.4 Execution Log: LakeQuest Latency Evaluation

## Summary
Ran the LakeQuest bank sample with tracing and the Phase 8.3 classifier on (120 turns, 0 errors, `timing.ok` on every row), then explained the route latency, the classifier, dense search and SQL from the bundle's own data. Report: `dev_docs/design/latency_report_lakequest.md`. **No pipeline code changed.**

## Result (full tables in the report)
- **Routes (p50 / max, seconds):** hybrid 5.02 / 13.52, table 2.81 / 4.86, document 3.36 / 4.88; all 3.63, p95 7.3. Without the classifier stage: all 2.53 (earlier intent-off run: 2.8).
- **Classifier:** `intent` stage p50 1.09 s (max 2.88 s), 30 % of a turn at the median. Routing accuracy 98.3 % (118/120; 2 invalid outputs fell back to `hybrid`). `tool_sequence_match` 0.992 vs 0.975 in the intent-off run, one turn either way, so no measurable routing gain.
- **Dense search:** 12.5 ms median when filtered (80 of 120 calls), 24.3 ms when it scans all 2,481 chunks (40 calls): 3.6–7 % of a ~344 ms tool call. The rest is the query embedding.
- **SQL:** every recorded query (104) ran in about 0.5 ms or less, including aggregates on the 125,742-row `transaction` table.
- **Other finding:** 10 of 130 `search_documents` calls had no `query` and were rejected, each costing an extra model round trip (likely cause: `LAKE_INSTRUCTION` rule 4 never mentions a query; not verified).

## How it was done
- **Run:** `uv run python -m evals configs/eval/lakequest_bank.yaml agent.latency_trace=true eval.repeats=3 name=lat_lakequest`, in the background (about 9 minutes). Run folder: `db-doc-qa/runs/20261028-082919_lat_lakequest` (git-ignored).
- **Analysis:** `dev_test/phase_8_4.py` (read-only, no model or network calls) prints the four tables. Searches are replayed on the bundle's `docs.db` with the run's retrieval settings and a fixed random unit vector; SQL is re-run read-only on `bank.db`. Both use `time.perf_counter()`, median of 3.
- **Decisions:** a query with an aggregate is tagged `aggregate` even if it also joins; groups with fewer than 3 calls are merged; calls with no query are skipped and counted; the dense part is the hybrid time minus a BM25-only run of the same call.

## Timer issue found (and what was corrected)
`time.monotonic()` ticks about every 15.6 ms on this Windows machine, so traced stages under ~16 ms read 0 or 0.016 s. That made two statements in the Phase 8.2 material unsupported: "the search itself is under 1 ms" and "SQL under 10 ms" (they came from readings of 0.0 ms, max 15 ms). I measured them with `perf_counter` on the shipments corpus (37 chunks): search about 0.9 ms (hybrid) / 0.7 ms (BM25), SQL point lookup 0.12 ms. The statements were right in substance; I replaced them in `latency_report.md` (three places) and in the README with the measured values and the method. The 312 ms query-embedding figure and the stage conclusions are unaffected, since those stages are far above the tick. 

## Follow-up: tracing clock fixed
- **Change:** every timer that feeds the trace or `latency_s` now uses `time.perf_counter()`: `AgentSession.ask_async`, the three setup timers in `data_agent/agent.py`, `evals/run.py` `_retrieve`, and the live check in `dev_test/phase_8_3.py`. The SQL timeout in `tools.py` (a deadline) and the token TTL in `ui_client.py` keep `monotonic`. A comment at the first use in each file says why.
- **Tests:** three new tests in `tests/test_latency_trace.py` replace the `time` name in each module with a scripted clock and assert the exact stage values (session turn and arrivals, the three setup steps, retrieval mode). With `monotonic` restored in `runner.py` the session test fails, as intended. `uv run pytest` → **426 passed**.
- **Live check** (`lakequest_bank.yaml agent.latency_trace=true dataset.limit=3`): 0 errors, `timing.ok` true; a single-call `run_sql` stage now reads 2.8 ms (dispatch included; it read 0 or 15 ms before) and the residual `other` about 1 ms (it read 0.00). The two ~360 ms `run_sql` stages in that run are parallel batches (`['run_sql', 'search_documents']`), dominated by the search's query embedding.
- **Effect on the reports:** earlier run folders keep the old 15.6 ms resolution. Their conclusions rest on stages of 100 ms and more and are unaffected; the sub-16 ms statements were already replaced by offline measurements.

## Files
- Created: `db-doc-qa/dev_test/phase_8_4.py`, `dev_docs/design/latency_report_lakequest.md`, this log.
- Modified: `dev_docs/design/latency_report.md` and `README.md` (the timer correction above), `dev_docs/sprints/sprint8.md` (Phase 8.4 ticked, follow-up added), `dev_docs/design/latency_evaluation_design.md` (clock note). Clock fix: `db-doc-qa/data_agent/agent.py`, `data_agent/runner.py`, `evals/run.py`, `dev_test/phase_8_3.py`, and three tests added to `db-doc-qa/tests/test_latency_trace.py`.

## Tests
`uv run pytest` (from `db-doc-qa/`) → **426 passed**: 423 before the clock follow-up, plus 3 new tests. The evaluation and report themselves changed no production code; only the follow-up did.

## Run it
```bash
uv run python -m evals configs/eval/lakequest_bank.yaml agent.latency_trace=true eval.repeats=3 name=lat_lakequest
uv run python -m dev_test.phase_8_4 [run_dir]     # no argument: the newest *_lat_lakequest
```

## Limitations
- Internal 40-case sample, 3 repeats, one machine: descriptive only. One turn moves `tool_sequence_match` by 0.008 here.
- The offline search replay uses a random vector, so it measures cost, not result quality. The extrapolation of the dense scan to larger corpora (about 150 ms at 25,000 chunks) is a linear estimate, not a measurement.
- Which two turns fell back to `hybrid` is not recorded in `results.jsonl`, so the fallback cannot be separated from a genuine `hybrid` label in the accuracy figure.



All seven Phase 8.4 tasks are done, and `uv run pytest` still passes with 423 tests. No production code changed. The report is `dev_docs/design/latency_report_lakequest.md`, and the log is `dev_docs/execution_logs/sprint_8_4.md`. I haven't staged or committed anything.

**The run:** 120 turns (40 cases × 3), 0 errors, and `timing.ok` on every row.

| Route | n | p50 (s) | max (s) | p50 without classifier (s) | avg tool calls |
|---|---|---|---|---|---|
| hybrid | 60 | 5.02 | 13.52 | 3.73 | 2.88 |
| table | 30 | 2.81 | 4.86 | 1.78 | 1.00 |
| document | 30 | 3.36 | 4.88 | 2.23 | 1.03 |
| all | 120 | 3.63 | 13.52 | 2.53 | 1.95 |

**Findings:**
- **Hybrid turns are the bottleneck.** The three slowest turns (9.2–13.5 s) are all hybrid, with 3.4 agent model calls per turn on average. Model calls take about two thirds of each hybrid turn.
- **The classifier costs about 1.09 s per turn**, 30 % of a turn at the median, and its routing is 98.3 % accurate (118/120). But the agent's actual tool choice barely moved: `tool_sequence_match` was 0.992, against 0.975 in the earlier run without the classifier. That is one turn either way, so on LakeQuest it adds latency without a measurable routing gain.
- **Dense search is not a bottleneck at 2,481 chunks.** A search takes 12.5 ms when filtered (80 of 120 calls) and 24.3 ms when it scans every chunk (40 calls). That is 3.6–7 % of a ~344 ms tool call, and the rest is the query embedding.
- **SQL is negligible.** All 104 recorded queries ran in about 0.5 ms or less, including aggregates on the 125,742-row transaction table.
- **Wasted calls:** 10 of the 130 searches had no `query` and were rejected, each costing an extra model round trip. Rule 4 of the LakeQuest instruction never mentions a query, which is a likely cause but not verified.
- **Fallbacks:** 2 classifier outputs were invalid and fell back to `hybrid`. Both fell on hybrid cases, so they count as correct by default.

**Correction to my earlier work:** the timer behind the traced stages, `time.monotonic()`, ticks only every ~15.6 ms on this machine. So my earlier claims in the shipments report and the README, "search under 1 ms" and "SQL under 10 ms", came from readings of 0.0 ms and could not have shown that. I re-measured with a high-resolution timer on the shipments corpus: about 0.9 ms for search and 0.12 ms for a SQL lookup. The claims were right in substance, and I replaced them with the measured values in `latency_report.md` and the README. The 312 ms embedding figure is unaffected.

**Decision for you:** the proper fix is to switch `time.monotonic()` to `time.perf_counter()` in the tracing code. That is a small code change, so I left it out of this no-code-change phase. It would make sub-16 ms stages readable in the trace itself. Should I do that?