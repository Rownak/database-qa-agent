# Sprint 8 — Latency Evaluation (shipments_v1)

**Goal:** measure where the time of one agent turn goes on `shipments_v1`, as described in [latency_evaluation_design.md](../design/latency_evaluation_design.md):

- **minimal, opt-in latency tracing** that is switched on by one config flag. It records **only the stages that actually ran**, so it works for any dataset and for partial pipelines (prebuilt bundles, intent off, BM25 only, retrieval mode, turns with no tool calls)
- a **baseline run and a few config-only experiments** on `shipments_v1`, ranked with the design's quality guardrail

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use canned events and fake sessions, with no network
- a short script `db-doc-qa/dev_test/phase_8_N.py`, run with `uv run python -m dev_test.phase_8_N`
- an entry in `dev_docs/execution_logs/sprint_8_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 8.1 | Opt-in latency tracing in the shared runner and the evaluation |
| 8.2 | `shipments_v1` baseline, experiments and report |

**Rules for both phases**

- **Off by default, identical when off.** The flag is `agent.latency_trace: bool = False`. With the flag off:
  - the app, the deployed agent, `smoke.yaml`, `bm25_only.yaml`, `lakequest_bank.yaml` and `hybridqa_*.yaml` behave exactly as before
  - `results.jsonl` and `metrics.json` keep their current keys
  - no extra session read happens
  - the existing tests pass unchanged
- **Record only stages that ran.** A stage that is skipped gets no entry, never a `0`. For example: no `data_build`/`docs_build` with a prebuilt bundle, no `intent` when `intent_enabled=false`, no `tool` on a refusal, and no query embedding with `bm25`.
- **No double-counting.** The turn's top-level stages do not overlap, so they add up to `latency_s`. Two rules keep it that way:
  - `other` is the residual: `latency_s` minus the sum of the stages. A negative `other` (beyond a 10 ms tolerance) means overlap; the trace is then marked `ok: false` and left out of the stage percentiles.
  - Nested detail (for example the query embedding inside `search_documents`) is not recorded in this sprint.
- **Dataset-agnostic.** No dataset name appears in `data_agent/` or `evals/{run,report,metrics}.py`.
- **Reuse, don't restructure:**
  - the existing `time.monotonic()` timers in `_ensure_data`
  - the events already dumped in `AgentSession.ask_async`, and its existing `get_session` read
  - `TurnResult`, `_row`, `_aggregate`, `_by_tag`, `report.write_run`, `format_summary` and the dotted overrides
  - no new output file, no new dependency, and no change to `session_factory` or other signatures
- **Privacy:** a trace holds only stage names, durations, tool names and token counts. It never holds SQL, passages, questions or file contents.

**Decision:** the flag lives in `AgentConfig`, not `EvalConfig`. The `_ensure_data` callback and `AgentSession` already receive `AgentConfig`, so no signature changes, and the app can use the flag through `DATA_AGENT_CONFIG`. The `_load_prebuilt` guard compares only chunking and embedding, so bundles stay valid.

---

## Phase 8.1 — Opt-in Latency Tracing

**Goal:** with `agent.latency_trace=true`, every `results.jsonl` row carries a `timing` object holding the stages that ran in that turn, and `metrics.json` carries per-stage and per-tag latency percentiles. With the flag off, nothing changes.

- [ ] **Config flag** (`data_agent/config.py`):
  - Add `AgentConfig.latency_trace: bool = False`.
  - Update the class docstring: it is a diagnostic flag and does not change the answer.
- [ ] **Setup stages** (`data_agent/agent.py`):
  - When the flag is on, `_ensure_data` writes `state["turn_timing"]` each turn: a list of `{"stage", "s"}` entries.
  - Include only the steps that ran: `data_build` and `docs_build` (from the existing timers) and `intent` (a new timer around `intent.classify`).
  - The value is overwritten each turn, never appended to.
- [ ] **Turn trace** (`data_agent/events.py`, `data_agent/runner.py`):
  - **Result field:** add `timing: dict | None = None` as the **last** field of `TurnResult`, so the positional constructions keep working.
  - **Derivation:** add a pure function `turn_timing(events, arrivals, setup, latency_s) -> dict`, with no ADK import. It returns `{"stages": [...], "other_s", "query_s", "ok"}`:
    - `llm` entries come from gaps between event arrivals; the first one has the setup stages subtracted. Each carries `in_tok`/`out_tok` from `usageMetadata` when present.
    - `tool` entries run from a `functionCall` event to its last `functionResponse`. Calls emitted together in one model event form one entry with `names: [...]`.
    - `query_s` is `latency_s` minus `data_build` and `docs_build`.
    - Events up to an error are kept; the rest of the time goes to `other`.
  - **Runner:** when the flag is on, `ask_async` records the arrival offset of each event and reads `turn_timing` with the existing `get_session` call. It then fills `timing`; with the flag off it does none of this.
- [ ] **Retrieval mode** (`evals/run.py` `_retrieve`): when the flag is on, record `query_embedding` (only when `mode != "bm25"`) and `search` as stages. This is the no-LLM partial pipeline.
- [ ] **Aggregation** (`evals/run.py`, `evals/__main__.py`):
  - `_row` adds `"timing"` only when `result.timing` is not `None`.
  - When any row has a trace (and `ok` is true), `_aggregate` adds `latency_stages`. For each stage name it holds:
    - `turns`: how many turns had the stage
    - `calls_per_turn`
    - `p50_s`/`p95_s`/`max_s`, computed over each turn's per-stage sum
    - plus `query_p50_s`/`query_p95_s` and the mean `in_tok`/`out_tok`
  - `_by_tag` adds `latency_p50_s`/`latency_p95_s` per tag only when traces exist.
  - `format_summary` prints one stage line and the per-tag latency when `latency_stages` is present.
- [ ] **Design doc:** update `latency_evaluation_design.md` §3–§4:
  - the flag
  - "skipped stages are absent, not 0"
  - the list form of `stages`
  - the residual and `ok` rule
- [ ] **Tests:**
  - `tests/test_events.py` (`turn_timing` on canned events with arrival offsets):
    - sequential `run_sql` then `search_documents`
    - two calls in one model event (one tool entry)
    - no tool calls
    - prebuilt state (no setup stages)
    - an error mid-turn
    - overlap that gives a negative `other`, which yields `ok: false`
    - every stage plus `other` adds up to `latency_s`
  - `tests/test_agent.py`:
    - with the flag on, `turn_timing` lists only the steps that ran (intent off means no `intent` entry; prebuilt means no build entries)
    - with the flag off, no `turn_timing` key
  - `tests/test_eval_run.py` (fake session):
    - rows with `timing` produce `latency_stages` and per-tag latency
    - rows without it produce today's keys exactly
    - retrieval mode with `bm25` has no `query_embedding` stage
  - `test_config`, `test_runner`, `test_app*` and the other suites pass unchanged.
- [ ] **Compatibility check:**
  - `uv run python -m evals configs/eval/smoke.yaml dataset.limit=1` gives the same file keys as before.
  - The same command with `agent.latency_trace=true` adds `timing` and `latency_stages`, and nothing else changes.
- [ ] **Dev script** `phase_8_1.py`: run one live question (`hybrid_delayed_express`) through `AgentSession` with the flag on, and print the stage list, `other_s` and `ok`.

## Phase 8.2 — shipments_v1 Baseline, Experiments and Report

**Goal:** a measured stage breakdown per route on `shipments_v1`, the top bottleneck identified, and 2–3 config-only experiments accepted or rejected with the quality guardrail (design §6).

- [ ] **Config** `configs/eval/latency_shipments.yaml`:
  - Start from a copy of `smoke.yaml` and set `name: latency_shipments`, `agent.latency_trace: true` and `eval.repeats: 5`.
  - Keep `smoke.yaml`'s `fail_under` thresholds, so a quality drop also fails the run.
- [ ] **Baseline:** run `uv run python -m evals configs/eval/latency_shipments.yaml` twice, back to back. Record:
  - the per-stage share of p50 for each route tag (`database`, `document`, `hybrid`, plus `conflict`/`gap`/`safety` from the tags), split into ingestion-inclusive, `query_s` and the first turn
  - the run-to-run spread
- [ ] **Experiments** (one lever per run, overrides only, picked from the baseline's top stages):
  - `agent.intent_enabled=false` (H2)
  - `agent.retrieval.max_top_k=4` (H4)
  - the retrieval-only comparison: `bm25_only.yaml` vs. `agent.retrieval.mode=hybrid`, both with `agent.latency_trace=true` (H5)
  - Accept a change only if `query_s` p50 improves and, per case, `tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision` stay within one case of the baseline. `refuse_write` must still refuse.
- [ ] **Report:** write `dev_docs/design/latency_report.md`:
  - the §6 stage table filled in
  - each hypothesis (H1–H7) marked confirmed, rejected or not tested
  - the accepted and rejected experiments, with deltas
  - a ranked list of next fixes, including code-change candidates such as H1 (app document build at upload) and H6 (reuse one `genai.Client`), which are not implemented in this sprint
- [ ] **Tests:** `tests/test_config.py`: `latency_shipments.yaml` loads and validates, with `latency_trace` true and `repeats` 5. There are no other code changes; confirm `uv run pytest` passes.
- [ ] **Dev script** `phase_8_2.py`: read two run folders (the baseline and one experiment) and print `latency_stages` p50 and the guarded metrics side by side.

---

## Out of Scope

- **Implementing the fixes found in 8.2** (for example document build at upload, client reuse, prompt changes for fewer LLM calls). They are ranked in the report for a later sprint.
- **LakeQuest and HybridQA latency runs.** The tracing works on them unchanged; their analysis is a later sprint.
- **Nested sub-stage timing** inside tools (query embedding vs. BM25 vs. dense vs. RRF), the intent call's token counts, streaming / TTFT, and concurrency or load tests.
- **Deployed latency** (Cloud Trace via `--otel_to_cloud`, UI→API round trip) and **MLflow / Phoenix** tracking. The `report.write_run` hook is unchanged.
