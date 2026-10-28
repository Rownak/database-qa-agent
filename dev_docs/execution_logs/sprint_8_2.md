# Sprint 8 — Phase 8.2 Execution Log: shipments_v1 Baseline, Experiments and Report

## Summary
Measured the stage breakdown on `shipments_v1` (two baseline runs plus three experiments), and wrote `dev_docs/design/latency_report.md`. Headline: model calls are 47–66 % of a turn and scale with the **number** of calls (about 0.7 s each, flat in prompt size). The intent call costs about 0.8 s per turn; turning it off cut the median `query_s` by 22 % (3.66 s → 2.84 s) and is conditionally accepted. `max_top_k=4` and BM25-only are rejected. No production code changed in this phase.

## What was done
- **Config:** `configs/eval/latency_shipments.yaml`, a copy of `smoke.yaml` with `name: latency_shipments`, `agent.latency_trace: true` and `eval.repeats: 5`. The `fail_under` thresholds are kept.
- **Baseline:** two back-to-back runs (`lat_base_a`, `lat_base_b`), 30 turns each, 0 errors. Run-to-run spread: `query_s` p50 3.31 s vs 3.77 s (13 %).
- **Experiments (overrides only, one lever each):** `agent.intent_enabled=false` (`lat_no_intent`), `agent.retrieval.max_top_k=4` (`lat_topk4`), and the retrieval-only comparison `bm25_only.yaml` with `agent.retrieval.mode=bm25` vs `hybrid`, both with `agent.latency_trace=true eval.repeats=5` (`lat_ret_bm25`, `lat_ret_hybrid`).
- **Report:** stage table per case, run-to-run spread, H1–H7 marked, experiment deltas and verdicts, a ranked list of next fixes.
- **Verdicts:** `intent_enabled=false` accepted conditionally (one routing deviation in 30 turns, so no default change until a repeat run); `max_top_k=4` rejected; BM25-only rejected as a latency fix.

## Approach and decisions
- The dataset tags only `conflict`, `gap` and `safety`, so the three plain cases (database, document, hybrid) are reported by case id instead of a route tag.
- "Within one case" is read as at most one repeat of five (0.2) per case metric, which matches the observed one-turn drift between the two identical baseline runs.
- The per-case and stage-level breakdown was computed with two throwaway scripts in the scratchpad (not in the repo); `metrics.json` already holds the aggregate `latency_stages`.
- Percentiles in the report use linear interpolation, the same method as the runner.

## Files
- Created: `db-doc-qa/configs/eval/latency_shipments.yaml`, `db-doc-qa/dev_test/phase_8_2.py`, `dev_docs/design/latency_report.md`, this log.
- Modified: `db-doc-qa/tests/test_config.py` (one test), `dev_docs/sprints/sprint8.md` (Phase 8.2 ticked).
- Run folders (git-ignored), under `db-doc-qa/runs/`: `20261028-003527_lat_base_a`, `…003745_lat_base_b`, `…004032_lat_no_intent`, `…004226_lat_topk4`, `…004450_lat_ret_bm25`, `…004452_lat_ret_hybrid`, plus the two Phase 8.1 check runs `chk_off` / `chk_on`.

## Tests
- Command (from `db-doc-qa/`): `uv run pytest` → **404 passed** (403 from Phase 8.1, plus `test_latency_shipments_config_loads_with_tracing_on_and_the_smoke_thresholds`, which also checks that tracing is the only difference from `smoke.yaml`'s agent settings).
- No other code changes.

## Run it
```bash
uv run python -m evals configs/eval/latency_shipments.yaml name=lat_base_a
uv run python -m evals configs/eval/latency_shipments.yaml agent.intent_enabled=false name=lat_no_intent
uv run python -m evals configs/eval/bm25_only.yaml agent.latency_trace=true eval.repeats=5 name=lat_ret_bm25
uv run python -m dev_test.phase_8_2 [baseline_run_dir candidate_run_dir]   # no arguments: newest lat_base_* vs lat_no_intent
```

## Limitations
- 30 turns per run: medians reliable to about ±0.5 s; p95 is descriptive. One quality turn moves a case metric by 0.2.
- Gemini latency varies over time; all runs were made within about 10 minutes of each other.
- H6 (`genai.Client` reuse) was not tested: every turn creates a client, so it cannot be separated from the API call without a micro-benchmark.
- In-process only: no UI→API transport or Cloud Run cold start. The 0.31 s query embedding depends on network distance to the Gemini API.
