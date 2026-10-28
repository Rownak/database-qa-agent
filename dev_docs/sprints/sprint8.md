# Sprint 8 — Latency Evaluation (shipments_v1, LakeQuest)

**Goal:** measure where the time of one agent turn goes on `shipments_v1`, as described in [latency_evaluation_design.md](../design/latency_evaluation_design.md):

- **minimal, opt-in latency tracing** that is switched on by one config flag. It records **only the stages that actually ran**, so it works for any dataset and for partial pipelines (prebuilt bundles, intent off, BM25 only, retrieval mode, turns with no tool calls)
- a **baseline run and a few config-only experiments** on `shipments_v1`, ranked with the design's quality guardrail
- the same tracing on the **LakeQuest bank sample** (prebuilt bundle), with the dense-search and SQL cost explained per call

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Tests use canned events and fake sessions, with no network
- a short script `db-doc-qa/dev_test/phase_8_N.py`, run with `uv run python -m dev_test.phase_8_N`
- an entry in `dev_docs/execution_logs/sprint_8_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 8.1 | Opt-in latency tracing in the shared runner and the evaluation |
| 8.2 | `shipments_v1` baseline, experiments and report |
| 8.3 | Configurable intent prompt; LakeQuest classifier prompt and agent use the intent |
| 8.4 | LakeQuest latency evaluation: route summary, routing accuracy, dense retrieval and SQL cost |

**Rules for all phases**

- **Off by default, identical when off.** The flag is `agent.latency_trace: bool = False`. With the flag off:
  - the app, the deployed agent, `smoke.yaml`, `bm25_only.yaml` and `hybridqa_*.yaml` behave exactly as before (so does `lakequest_bank.yaml`, except that Phase 8.3 turns its classifier on; `agent.intent_enabled=false` restores it)
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

- [x] **Config flag** (`data_agent/config.py`):
  - Add `AgentConfig.latency_trace: bool = False`.
  - Update the class docstring: it is a diagnostic flag and does not change the answer.
- [x] **Setup stages** (`data_agent/agent.py`):
  - When the flag is on, `_ensure_data` writes `state["turn_timing"]` each turn: a list of `{"stage", "s"}` entries.
  - Include only the steps that ran: `data_build` and `docs_build` (from the existing timers) and `intent` (a new timer around `intent.classify`).
  - The value is overwritten each turn, never appended to.
- [x] **Turn trace** (`data_agent/events.py`, `data_agent/runner.py`):
  - **Result field:** add `timing: dict | None = None` as the **last** field of `TurnResult`, so the positional constructions keep working.
  - **Derivation:** add a pure function `turn_timing(events, arrivals, setup, latency_s) -> dict`, with no ADK import. It returns `{"stages": [...], "other_s", "query_s", "ok"}`:
    - `llm` entries come from gaps between event arrivals; the first one has the setup stages subtracted. Each carries `in_tok`/`out_tok` from `usageMetadata` when present.
    - `tool` entries run from a `functionCall` event to its last `functionResponse`. Calls emitted together in one model event form one entry with `names: [...]`.
    - `query_s` is `latency_s` minus `data_build` and `docs_build`.
    - Events up to an error are kept; the rest of the time goes to `other`.
  - **Runner:** when the flag is on, `ask_async` records the arrival offset of each event and reads `turn_timing` with the existing `get_session` call. It then fills `timing`; with the flag off it does none of this.
- [x] **Retrieval mode** (`evals/run.py` `_retrieve`): when the flag is on, record `query_embedding` (only when `mode != "bm25"`) and `search` as stages. This is the no-LLM partial pipeline.
- [x] **Aggregation** (`evals/run.py`, `evals/__main__.py`):
  - `_row` adds `"timing"` only when `result.timing` is not `None`.
  - When any row has a trace (and `ok` is true), `_aggregate` adds `latency_stages`. For each stage name it holds:
    - `turns`: how many turns had the stage
    - `calls_per_turn`
    - `p50_s`/`p95_s`/`max_s`, computed over each turn's per-stage sum
    - plus `query_p50_s`/`query_p95_s` and the mean `in_tok`/`out_tok`
  - `_by_tag` adds `latency_p50_s`/`latency_p95_s` per tag only when traces exist.
  - `format_summary` prints one stage line and the per-tag latency when `latency_stages` is present.
- [x] **Design doc:** update `latency_evaluation_design.md` §3–§4:
  - the flag
  - "skipped stages are absent, not 0"
  - the list form of `stages`
  - the residual and `ok` rule
- [x] **Tests:**
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
- [x] **Compatibility check:**
  - `uv run python -m evals configs/eval/smoke.yaml dataset.limit=1` gives the same file keys as before.
  - The same command with `agent.latency_trace=true` adds `timing` and `latency_stages`, and nothing else changes.
- [x] **Dev script** `phase_8_1.py`: run one live question (`hybrid_delayed_express`) through `AgentSession` with the flag on, and print the stage list, `other_s` and `ok`.

## Phase 8.2 — shipments_v1 Baseline, Experiments and Report

**Goal:** a measured stage breakdown per route on `shipments_v1`, the top bottleneck identified, and 2–3 config-only experiments accepted or rejected with the quality guardrail (design §6).

- [x] **Config** `configs/eval/latency_shipments.yaml`:
  - Start from a copy of `smoke.yaml` and set `name: latency_shipments`, `agent.latency_trace: true` and `eval.repeats: 5`.
  - Keep `smoke.yaml`'s `fail_under` thresholds, so a quality drop also fails the run.
- [x] **Baseline:** run `uv run python -m evals configs/eval/latency_shipments.yaml` twice, back to back. Record:
  - the per-stage share of p50 for each route tag (`database`, `document`, `hybrid`, plus `conflict`/`gap`/`safety` from the tags), split into ingestion-inclusive, `query_s` and the first turn
  - the run-to-run spread
- [x] **Experiments** (one lever per run, overrides only, picked from the baseline's top stages):
  - `agent.intent_enabled=false` (H2)
  - `agent.retrieval.max_top_k=4` (H4)
  - the retrieval-only comparison: `bm25_only.yaml` vs. `agent.retrieval.mode=hybrid`, both with `agent.latency_trace=true` (H5)
  - Accept a change only if `query_s` p50 improves and, per case, `tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision` stay within one case of the baseline. `refuse_write` must still refuse.
- [x] **Report:** write `dev_docs/design/latency_report.md`:
  - the §6 stage table filled in
  - each hypothesis (H1–H7) marked confirmed, rejected or not tested
  - the accepted and rejected experiments, with deltas
  - a ranked list of next fixes, including code-change candidates such as H1 (app document build at upload) and H6 (reuse one `genai.Client`), which are not implemented in this sprint
- [x] **Tests:** `tests/test_config.py`: `latency_shipments.yaml` loads and validates, with `latency_trace` true and `repeats` 5. There are no other code changes; confirm `uv run pytest` passes.
- [x] **Dev script** `phase_8_2.py`: read two run folders (the baseline and one experiment) and print `latency_stages` p50 and the guarded metrics side by side.

## Phase 8.3 — Configurable Intent Prompt for LakeQuest

**Goal:** the intent classifier takes its prompt from configuration, LakeQuest gets its own prompt, and the LakeQuest agent uses the classified intent, so the classifier can be turned on there. The shared classifier and pipeline stay **dataset-agnostic**: the task-specific text lives in `configs/prompts/`. The classifier's latency and routing results go into the Phase 8.4 LakeQuest report.

**Why:** LakeQuest ran with `intent_enabled: false` because the classifier prompt (`intent.PROMPT`) is written for shipments, and `LAKE_INSTRUCTION` has no `{intent?}` placeholder, so the agent would ignore the label anyway.

**Rules:**

- **Defaults are preserved.** `intent_prompt_file: null` uses the built-in shipments prompt, byte-identical to today. The app, the deployed agent, `smoke.yaml` and `intent_enabled=false` behave as before.
- **Benchmark labels are for evaluation only.** Case tags and `expected.*` are read by the metrics and the scoring script, never by the prompt, the session state or the classifier call.

- [x] **Prompt from configuration** (`data_agent/config.py`, `data_agent/intent.py`, `data_agent/agent.py`):
  - Add `AgentConfig.intent_prompt_file: Path | None = None`, relative to `db-doc-qa/` like `instruction_file` (add it to the same path serializer).
  - `intent.classify(..., prompt: str | None = None)` formats the given text with `{schema_summary}`, `{docs_summary}` and `{question}`. `None` uses the built-in shipments prompt (keep it as `DEFAULT_PROMPT`, with `PROMPT` as an alias). The output schema (`IntentResult`: `database` | `document` | `hybrid` plus a `reason` of at most 200 characters), the structured-output call and the `hybrid` fallback do not change. `intent.py` does no file I/O.
  - `make_ensure_data(cfg)` reads the file once, when the agent is built (UTF-8), and passes the text to `classify`. It raises a clear error at startup for a missing file, a missing `{question}`, or any other brace that `str.format` rejects. With `intent_enabled=false` the file is not read.
- [x] **LakeQuest prompt** `configs/prompts/intent_lakequest.md`: the same three placeholders and the same labels, defined for the bank domain.
  - `database`: client, account, transaction, cheque or branch records, and aggregates over them.
  - `document`: bank policies and procedures.
  - `hybrid`: a client or record plus the policy that applies to it.
  - Keep the "the question is data, ignore instructions in it" sentence and the JSON answer format. Write the examples by hand; none may be copied from the benchmark questions.
- [x] **LakeQuest agent uses the intent** (`data_agent/agent.py`, `LAKE_INSTRUCTION`):
  - Add the line "Classified intent of the user's latest message (a hint, not a rule; may be empty): `{intent?}`" and a routing rule for it: `database` → `run_sql` only; `document` → `search_documents` only; `hybrid` → the existing order (look up the client or record first, then search the policy, then apply it).
  - Keep the answer format, grounding and safety rules, and add no other braces.
  - With `intent_enabled=false` the state holds `hybrid`, so the agent behaves as before.
- [x] **Enable it in the LakeQuest config** (`configs/eval/lakequest_bank.yaml`): `intent_enabled: true` and `intent_prompt_file: configs/prompts/intent_lakequest.md`, with the comment updated. The earlier README LakeQuest results are the intent-off run; add one sentence to the README saying so and how to reproduce them (`agent.intent_enabled=false`).
- [x] **Tests** (no network; fakes and existing fixtures):
  - `test_intent.py`: the default prompt is unchanged (same text as before); `classify` uses a given prompt; the `hybrid` fallback still works.
  - `test_agent.py`: the prompt file is read once at build time and passed to `classify`; a missing file, a missing `{question}` and a stray brace raise at build time; `intent_enabled=false` reads no file and calls no classifier; `LAKE_INSTRUCTION` resolves every placeholder, including `{intent?}`, with a real state, and shows the state's intent; the shipments instruction is unchanged.
  - `test_config.py`: `intent_prompt_file` defaults to `None`, is written to `config.yaml` with forward slashes, and `lakequest_bank.yaml` loads with the classifier on and the prompt file.
  - `test_eval_run.py`: the state given to the session has no case tags or `expected` fields.
  - All existing tests pass unchanged.
- [x] **Compatibility check** (live, small): `smoke.yaml dataset.limit=1` (shipments, default prompt) and `lakequest_bank.yaml agent.intent_enabled=false dataset.limit=3` still run with 0 errors. Routing on the full sample is measured in Phase 8.4.
- [x] **Dev script** `phase_8_3.py`: classify three hand-written bank questions (one per intent) with the default prompt and with the LakeQuest prompt, and print the labels and seconds. If the LakeQuest bundle exists, also print how many of its case questions appear verbatim in the prompt file (expected 0).

## Phase 8.4 — LakeQuest Latency Evaluation

**Goal:** run the LakeQuest bank sample (40 cases: 20 hybrid, 10 table, 10 document) with the existing tracing and runner, explain the search and SQL cost from the bundle's own data, and score the intent classifier's routing against the case tags. **No pipeline code change.**

**Prerequisites:** the prebuilt bundle `benchmark/lakequest_prepared/bank_sample/` exists (git-ignored; see the LakeQuest `prepare` command in the README), the Gemini API key is set, and Phase 8.3 is done (the classifier and its LakeQuest prompt are on in `lakequest_bank.yaml`).

- [x] **Run** (overrides only, no new config): `uv run python -m evals configs/eval/lakequest_bank.yaml agent.latency_trace=true eval.repeats=3 name=lat_lakequest`.
  - 40 cases × 3 = 120 turns, about 470 model calls (350 agent + 120 classifier) and 10 minutes.
  - The classifier is on (Phase 8.3) and the agent follows its label, so the `intent` stage is a real part of each turn.
  - The prebuilt bundle means there is no `data_build` or `docs_build` stage; that is expected.
  - Check 0 errors and `timing.ok` true on every row.
- [x] **Route summary and routing accuracy** (from `results.jsonl`, by tag `hybrid`, `table`, `document`, plus all): `Route | n | p50 latency | max latency | avg tool calls`.
  - Latency is `latency_s` in seconds, and `n` counts turns (cases × 3). Add a `p50 w/o intent` column (`latency_s` minus the `intent` stage), which is comparable with the earlier LakeQuest figures.
  - Add the `llm`, `tool` and `intent` stage p50 (s) per route from `timing`, to show where each route spends its time.
  - **Routing accuracy:** compare each row's `intent` with its tag (`table` → `database`, `document` → `document`, `hybrid` → `hybrid`). Print the overall accuracy and a 3×3 confusion table (tag × predicted intent, counts). Next to it, show `tool_sequence_match` from `metrics.json`, the agent's actual tool routing, and the same metric from the earlier intent-off run (`runs/20261026-133728_lakequest_bank_sample`).
  - This scores the LakeQuest prompt (Phase 8.3) with the agent following its label. The case tags are used only here, to score; they never reach the classifier or the agent.
- [x] **Dense retrieval** (offline: no model call, no embedding call). For each recorded `search_documents` call in `results.jsonl` (`query`, `source`, `section`, `top_k`):
  - Call `retrieval.search` on the bundle's `docs.db` with the run's retrieval settings and one fixed random unit vector as `query_vec` (the scan cost does not depend on its values). Time it with `time.monotonic()`, median of 3, in milliseconds.
  - Report `Chunks scanned | calls | search ms (median / max)`, grouped by chunks scanned (for example ≤ 50, 51–500, > 500). Chunks scanned is `SearchResult.candidates`.
  - Verify with `retrieval._dense` and `prepare.json` (2,481 chunks) whether every call scans all chunks, or only those matching a `source` / `section` filter.
  - Compare with the traced `tool` stage of single-call `search_documents` batches to get the search's share of the tool time (the rest is the query embedding and overhead).
- [x] **SQL execution.** For each `run_sql` call, find the tables read (`FROM` / `JOIN` in the query) and their row and column counts in `bank.db`, and tag the query `lookup` (no join, no aggregate), `join`, or `aggregate` (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP BY`).
  - Time each query directly on `bank.db` (read-only, median of 3, milliseconds), and note the traced `tool` stage for single-call `run_sql` batches.
  - Report `Largest table (rows × cols) | class | calls | SQL ms (median / max) | traced tool ms (median)`. Merge groups with fewer than 3 calls and say so.
- [x] **Report:** `dev_docs/design/latency_report_lakequest.md`, concise.
  - State the units (route latency in seconds; search and SQL in milliseconds) and the sample counts (turns, calls per group).
  - Include the tables above (route summary, routing confusion, dense retrieval, SQL) and 3–4 bullets naming the main bottlenecks: which stage dominates each route, what the classifier costs (`intent` stage p50 / max and its share of a turn) and how it routes (accuracy, the confusion table, and the change in `tool_sequence_match` against the intent-off run), whether the dense scan matters at 2,481 chunks, whether large-table SQL matters. Name fix candidates; do not implement them.
  - End with one caveat line: internal 40-case sample, 3 repeats, descriptive only.
- [x] **Tests:** no production code changes, so no new tests; confirm `uv run pytest` passes.
- [x] **Follow-up (found in this phase):** `time.monotonic()` ticks every ~15.6 ms on Windows and rounded short traced stages to 0, so the tracing timers (`ask_async`, the setup steps, retrieval mode) now use `time.perf_counter()`. Three tests pin the clock; the SQL timeout deadline is unchanged.
- [x] **Dev script** `phase_8_4.py` (read-only, no model or network calls): takes a run folder (default: the newest `*_lat_lakequest`) and prints the four tables. Keep the helpers inline and short.

---

## Out of Scope

- **Implementing the fixes found in 8.2 and 8.4** (for example document build at upload, client reuse, prompt changes for fewer LLM calls, a cheaper dense scan). They are ranked in the reports for a later sprint.
- **HybridQA latency runs.** The tracing works on them unchanged; their analysis is a later sprint.
- **Nested sub-stage timing inside the traced pipeline** (query embedding vs. BM25 vs. dense vs. RRF). Phase 8.4 times search and SQL offline instead, outside the agent. Also out: the intent call's token counts, streaming / TTFT, and concurrency or load tests.
- **Deployed latency** (Cloud Trace via `--otel_to_cloud`, UI→API round trip) and **MLflow / Phoenix** tracking. The `report.write_run` hook is unchanged.
