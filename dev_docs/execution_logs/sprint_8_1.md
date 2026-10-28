# Sprint 8 — Phase 8.1 Execution Log: Opt-in Latency Tracing

## Summary
With `agent.latency_trace: true`, every turn records the stages that actually ran, and the evaluation reports per-stage and per-route latency. With the flag off (the default) nothing is recorded or read, and `results.jsonl` / `metrics.json` keep their old keys. A live one-case run showed the stages plus the residual `other` adding up exactly to `latency_s`.

## What changed
- **Flag** (`data_agent/config.py`): `AgentConfig.latency_trace: bool = False`, documented as a diagnostic flag that does not change an answer.
- **Setup stages** (`data_agent/agent.py`): `_build_session_data`, `_build_session_docs` and `_classify_turn` now return the seconds they took (`None` when nothing ran). With the flag on, `_ensure_data` writes `state["turn_timing"] = [{"stage", "s"}]` each turn (replaced, never appended) for `data_build`, `docs_build` and `intent`, only for the steps that ran.
- **Turn trace** (`data_agent/events.py`, `data_agent/runner.py`):
  - `TurnResult.timing: dict | None = None`, the last field, so positional constructions are unchanged.
  - `turn_timing(events, arrivals, setup, latency_s)` (pure, no ADK import) turns event arrival times into `llm` and `tool` stages. `summarize_stages(stages, latency_s)` closes any list of stages: the residual `other_s`, `query_s` (latency without `data_build` / `docs_build`) and `ok`.
  - `AgentSession.ask_async` records arrivals only when the flag is on, and reads `turn_timing` from the events' `stateDelta`, falling back to the session state. With the flag off, the code path (including the `get_session` condition) is unchanged.
- **Retrieval mode** (`evals/run.py`): `_retrieve` records `query_embedding` (absent with `bm25`) and `search`.
- **Aggregation** (`evals/run.py`, `evals/__main__.py`): `_row` adds `timing` only when present; `_aggregate` adds `latency_stages` (per stage: `turns`, `calls_per_turn`, `p50_s`, `p95_s`, `max_s`, plus `query_p50_s/p95_s` and token means) only when usable traces exist; `_by_tag` adds `latency_p50_s/p95_s` only then; `format_summary` prints one stage line and the per-tag latency.
- **Design doc**: `latency_evaluation_design.md` §3–§4 now describe the flag, "skipped stages are absent, not 0", the list form of `stages`, and the residual / `ok` rule.

## Key decisions
- **No double-counting:** only top-level, non-overlapping stages are recorded; `other` is the residual, so the parts always sum to `latency_s`. A negative residual beyond 10 ms sets `ok: false`, and that row is left out of the stage percentiles. Nested detail (for example the query embedding inside `search_documents`) is not recorded.
- **Events are skipped when they carry no content** (state deltas) or are partial chunks; their time falls into the next stage instead of being lost.
- **Setup is read from the events' `stateDelta`** first, so it also works for the API path; the session-state read is only a fallback.
- Stage percentiles are taken over the turns that had the stage, so a rare stage is not diluted by zeros.

## Files
- Modified: `data_agent/config.py`, `data_agent/agent.py`, `data_agent/events.py`, `data_agent/runner.py`, `evals/run.py`, `evals/__main__.py`, `dev_docs/design/latency_evaluation_design.md`, `dev_docs/sprints/sprint8.md` (Phase 8.1 ticked).
- Created: `db-doc-qa/tests/test_latency_trace.py` (19 tests), `db-doc-qa/dev_test/phase_8_1.py`, this log.

## Tests
- Command (from `db-doc-qa/`): `uv run pytest` → **403 passed** (384 existing, unchanged, plus 19 new).
- New tests cover: sequential / parallel / no-tool / prebuilt / error / overlap / partial-chunk turns and the sum check; the setup steps that ran (flag off, on, second turn, intent off, no documents); `AgentSession` with the flag off and on, the session-state fallback and an error mid-turn; rows, `latency_stages`, per-tag latency and the summary line; untraced rows keeping their old keys; overlapping traces excluded; retrieval mode with `bm25` and `hybrid`.
- Compatibility (live): `uv run python -m evals configs/eval/smoke.yaml dataset.limit=1` has the same keys as before. With `agent.latency_trace=true` it adds only `timing` (row) and `latency_stages` (metrics). Example summary line: `stage p50  data_build 0.06s  docs_build 0.70s  intent 0.97s  llm 2.28s  tool 0.00s  other 0.00s  (query p50 3.2s p95 3.2s)`.

## Run it
```bash
uv run pytest
uv run python -m dev_test.phase_8_1                                    # one live hybrid question, prints the stages
uv run python -m evals configs/eval/smoke.yaml dataset.limit=1 agent.latency_trace=true
```

## Notes and limitations
- **Test file:** the plan listed tests in `test_events.py`, `test_agent.py` and `test_eval_run.py`; they are in one new file, `test_latency_trace.py`, so the existing test files are untouched.
- The first `llm` stage also contains the small cost of `_sweep()` and ADK start-up, since neither is timed separately. In the live runs it was well under 10 ms (`other` rounded to 0.0).
- Tools called together by the model form one `tool` entry (ADK merges their responses); per-tool time inside a batch is not separated.
- Non-streaming runs are assumed. The intent call's token counts are not recorded.
- The two `chk_off` / `chk_on` check runs are in `db-doc-qa/runs/` (git-ignored).
