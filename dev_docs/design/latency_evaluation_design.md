# Latency Evaluation Design — Database & Document Q&A Agent

## 1. Goal and scope

Find out where the time of one agent turn goes, which stage is the bottleneck for database,
document and hybrid questions, and which change is worth making first — without lowering
answer quality or SQL safety.

- **In scope:** in-process query latency through the existing `evals/` runner, broken down
  by stage; per-turn token counts; a ranked list of improvement experiments.
- **Out of scope for the first phase:** browser / UI→API transport and Cloud Run cold
  starts (§8 describes how to measure them later), streaming / time-to-first-token (the
  runner does not stream), and load or concurrency testing.
- **Principle:** reuse `RunConfig`, `run_eval`, `results.jsonl` and `metrics.json`. No new
  storage format, no tracing platform, and no new dependency in the first phase.

## 2. What exists today (verified in the code)

| Fact | Where | Consequence for latency |
|---|---|---|
| `latency_s` is `time.monotonic()` around `runner.run_async(...)` | `runner.py` `AgentSession.ask_async` | One number per turn; no breakdown |
| The `before_agent_callback` `_ensure_data` runs **inside** that window: it builds `data.db`, builds `docs.db` (parse, chunk, **embed every chunk**), calls the intent classifier (one LLM call), then `_sweep()` | `agent.py` | Turn latency includes ingestion and classification |
| `_ensure_data` already measures the data and document builds with `time.monotonic()`, but only **logs** them to stderr | `agent.py` `_build_session_data`, `_build_session_docs` | Half the instrumentation exists; it just does not reach the results |
| The agent eval creates a **new `AgentSession` per case and repeat** | `evals/run.py` `_ask` | With `files_state` (e.g. `shipments_v1`), every turn rebuilds and re-embeds `docs.db`: the numbers are ingestion-inclusive |
| A prebuilt bundle (`state_file`, used by LakeQuest and HybridQA) skips both builds | `evals/run.py` `_load_prebuilt`, `agent.py` `_docs_ready` | The README numbers (LakeQuest p50 2.8 s; HybridQA p50 4.6 s / p95 24.5 s) are query-only and **cannot be compared** with a `shipments_v1` run |
| ADK events carry `usageMetadata` (tokens) and `timestamp`; the runner dumps every event to a dict, then keeps only the parsed answer, calls and intent | `runner.py`, `events.py` | Per-LLM-call tokens and the event order are available but thrown away |
| `intent.classify` makes a separate `generate_content` call and creates a new `genai.Client()` on every call | `intent.py` | One extra serial LLM round trip per turn, plus possible connection setup |
| `search_documents` embeds the query (an API call) unless `retrieval.mode=bm25` | `tools.py`, `retrieval.py` | A network call inside the tool |
| `eval.mode=retrieval` already times embedding plus search with no LLM | `evals/run.py` `_retrieve` | Retrieval latency can be isolated with no code change |
| `_aggregate` reports p50/p95 over rows without errors and counts the errors; `by_tag` has metrics but no latency | `evals/run.py` | No latency per route |
| `eval.repeats` is limited to 1–5; cases run one at a time | `config.py`, `evals/run.py` | Already sequential; no concurrency noise |
| ADK 2.12 emits OpenTelemetry spans (agent invocation, LLM call, tool call) and `adk api_server --otel_to_cloud` exports them to Cloud Trace | `google/adk/telemetry`, `cli/api_server.py` | The deployed path can be traced without app code (§8) |

**Expected call pattern per turn** (with the classifier on): database = intent + 2 agent LLM
calls + `run_sql`; document = intent + 2 LLM calls + query embedding + search; hybrid =
intent + 3 or more LLM calls + both tools. Everything in a turn runs **one step after
another**, except tool calls that the model emits together in one response.

## 3. Timing model

Because the stages run one after another, a flat list of stages is enough. A span tree,
parent/child IDs and interval unions are not needed.

**Opt-in, and only what ran.** Tracing is switched on with `agent.latency_trace: true`
(default `false`; set it in the YAML or with `agent.latency_trace=true`). With the flag off,
nothing is recorded or read and the result files keep their old keys. With it on, `timing` is
a **list of the stages that actually ran**. A skipped stage has no entry; it is never written
as `0`. This keeps the trace valid for any dataset and for partial pipelines (a prebuilt
bundle has no build stages, `intent_enabled=false` has no `intent`, a refusal has no `tool`,
BM25 has no `query_embedding`).

| Stage | How it is measured | Present when |
|---|---|---|
| `data_build` | Timer that already exists in `_build_session_data` | `data.db` was built this turn (not with a prebuilt bundle or on a later turn) |
| `docs_build` | Timer that already exists in `_build_session_docs` | `docs.db` was built this turn; includes chunk embedding |
| `intent` | New timer around `intent.classify` | `intent_enabled` is true |
| `llm` | Gap between event arrivals (below); carries `in_tok` / `out_tok` | Once per agent model call |
| `tool` | From the previous event to the `functionResponse` event; carries `names` | Once per tool batch (calls emitted together are one entry) |
| `other` | `latency_s` minus the stages above (the residual) | Always; sweep, ADK overhead, state reads. It should stay small |

In `eval.mode: retrieval` the stages are `query_embedding` (absent with `bm25`) and `search`.

**Derivation from event arrival times.** In `ask_async`, record `time.perf_counter() - started`
as each event is yielded. (`perf_counter`, not `monotonic`: on Windows `monotonic` ticks only
every ~15.6 ms, which rounds a SQL call or a search to 0. All timers that feed the trace and
`latency_s` use `perf_counter`; the SQL timeout in `tools.py` is a deadline and keeps
`monotonic`.) The runner does not stream, so a model event arrives when its LLM
call has finished:

```text
start ─ setup + intent + LLM#1 ─▶ [model: functionCall run_sql]
      ─ tool ───────────────────▶ [functionResponse]
      ─ LLM#2 ──────────────────▶ [model: functionCall search_documents]
      ─ tool ───────────────────▶ [functionResponse]
      ─ LLM#3 ──────────────────▶ [model: final text] ─ other ─▶ end
```

`LLM#1 = first model event arrival − (the setup stages that ran)`. Events without content
(state deltas) and partial streaming chunks are skipped, so their time falls into the next
stage. The arrival time we record is used rather than ADK's `Event.timestamp`, because ADK
sets that field inside its own flow and its exact meaning is not part of a stable contract.

**No double-counting.** The top-level stages do not overlap, so `sum(stages) + other = latency_s`
by construction. Nested detail (for example the query embedding inside `search_documents`) is
not recorded. If the residual is negative beyond a 10 ms tolerance, the stages overlapped
(for example, an ADK version change emits events differently); the row is marked `ok: false`
and is left out of the stage percentiles rather than reporting misleading numbers.

**Cold and warm turns.** There are three populations. Each is reported separately and never
mixed into one percentile:

| Label | Meaning | How it is obtained |
|---|---|---|
| `ingest_inclusive` | Today's `latency_s` for a `files_state` dataset | As is |
| `query` | Turn latency without ingestion: `latency_s − data_build − docs_build` (`query_s`) | Derived; no new run mode |
| `first_turn` | The first row of a run (imports, first HTTPS connection, provider warm-up) | Row index 0, reported on its own |

Subtracting setup gives the warm-query number without changing how the runner builds
sessions. A real "reuse one prepared session" mode is only worth adding if the subtracted
number turns out to differ from prebuilt-bundle runs.

## 4. Minimal code changes

There are a few small edits, and every one is backward compatible (Sprint 8, Phase 8.1):

| # | File | Change |
|---|---|---|
| 0 | `data_agent/config.py` | `AgentConfig.latency_trace: bool = False`. It is a diagnostic flag and does not change an answer |
| 1 | `data_agent/agent.py` | With the flag on, `_ensure_data` writes `state["turn_timing"] = [{"stage", "s"}]` each turn, for the steps that ran. The two existing timers are returned by `_build_session_data` / `_build_session_docs` (`None` if nothing was built), and one timer is added around `intent.classify` |
| 2 | `data_agent/events.py` + `runner.py` | `TurnResult.timing: dict | None = None` is the **last** field, so the positional constructions keep working. Pure functions `turn_timing(events, arrivals, setup, latency_s)` and `summarize_stages(stages, latency_s)` in `events.py` (no ADK import, so the UI image is unaffected). With the flag on, `ask_async` records arrivals and reads `turn_timing` from the events' `stateDelta`, falling back to the session state; with it off, it does neither |
| 3 | `evals/run.py` | `_row` adds `"timing"` only when present. `_aggregate` adds `latency_stages` only when usable traces exist. `_by_tag` adds latency p50/p95 per tag only then. Retrieval mode records `query_embedding` / `search` |
| 4 | `evals/__main__.py` | `format_summary` prints one stage line and the per-tag latency when present |

**Not changed:** the YAML schema beyond the one flag, the run folder layout,
`report.write_run`, metrics, tools, retrieval and the app. No `traces.jsonl` file is added:
one turn is already one `results.jsonl` row, and its `timing` object holds the breakdown.

**`results.jsonl` row (new key only, flag on):**

```json
"timing": {
  "stages": [{"stage": "data_build", "s": 0.016}, {"stage": "docs_build", "s": 0.719},
             {"stage": "intent", "s": 0.843},
             {"stage": "llm", "s": 1.313, "in_tok": 2024, "out_tok": 35},
             {"stage": "tool", "s": 0.0, "names": ["run_sql"]},
             {"stage": "llm", "s": 0.781, "in_tok": 2223, "out_tok": 22},
             {"stage": "tool", "s": 0.36, "names": ["search_documents"]},
             {"stage": "llm", "s": 1.797, "in_tok": 3064, "out_tok": 556}],
  "other_s": 0.0, "query_s": 5.094, "ok": true
}
```

`metrics.json` gets `latency_stages`: `n`, `query_p50_s`, `query_p95_s`, `in_tok_mean`,
`out_tok_mean` and, per stage name (including `other`), `turns`, `calls_per_turn`, `p50_s`,
`p95_s`, `max_s`. A stage that ran several times in one turn is summed for that turn, and the
percentiles are taken over the turns that had the stage.

Token fields come from `usageMetadata` (`promptTokenCount`, `candidatesTokenCount`). The
intent call's tokens are omitted in the first phase, because `classify` returns no usage.
Adding them is a one-line change if intent turns out to matter.

**Privacy:** the `timing` object holds only durations, counts, tool names and token counts.
It never contains SQL, passages or file contents. This matches the rule in `logs.py`.

## 5. Workload and protocol

Reuse the existing datasets. Add one config file, `configs/eval/latency.yaml`:

```yaml
name: latency_shipments
agent:
  model: {agent: gemini-3.5-flash-lite, temperature: 0.0}
  intent_enabled: true
  retrieval: {mode: hybrid}
dataset:
  path: evals/datasets/shipments_v1.yaml   # 6 cases: database, document, hybrid, conflict, gap, refusal
eval:
  mode: agent
  repeats: 5
```

| Run | Command (from `db-doc-qa/`) | Purpose |
|---|---|---|
| Baseline, ingestion-inclusive | `uv run python -m evals configs/eval/latency.yaml` | Stage breakdown for every route |
| Prebuilt (query-only reference) | `uv run python -m evals configs/eval/lakequest_bank.yaml eval.repeats=3` | Checks that the derived `query_s` matches a run with no ingestion |
| Retrieval only (no LLM) | `uv run python -m evals configs/eval/bm25_only.yaml` and the same with `agent.retrieval.mode=hybrid` | Embedding cost versus search cost |

Protocol:

- 6 cases × 5 repeats = 30 turns, about 2–4 minutes and well within free-tier limits.
- Record the commit hash in the run name or notes. `config.yaml` already pins every agent
  setting.
- Compare a baseline and a candidate **on the same day, run back to back**, by case ID.
  With 30 turns, p95 is a description of this sample, not an SLA.
- Use the Gemini embedding provider for latency runs (it is what the app uses), unless an
  embedding rate limit makes the run fail. Then use `ollama`, and label the results as not
  comparable.

## 6. Analysis and prioritization

**Step 1 — Where does the time go?** For each route tag, show the share of `latency_s` p50
per stage:

| Route | setup | intent | LLM (n calls) | tools | other | total p50 |
|---|---|---|---|---|---|---|
| database | … | … | … (2) | … | … | … |
| document | … | … | … (2) | … | … | … |
| hybrid | … | … | … (3+) | … | … | … |

**Step 2 — Check these hypotheses, which come from reading the code.** They are suspicions
to confirm or reject with data, not findings.

| # | Hypothesis | Evidence that confirms it | Candidate fix | Cost |
|---|---|---|---|---|
| H1 | Document embedding at session setup dominates the first turn (UI) and every eval turn | `docs_s` ≫ the other stages | Eval: use prebuilt bundles or build once per run. App: build documents at upload time, not on the first question | Low (eval) / medium (app) |
| H2 | The intent call adds a serial round trip with little routing benefit | `intent_s` ≈ one `llm_s`; with `agent.intent_enabled=false`, `tool_sequence_match` is unchanged | Turn the classifier off, use a smaller `model.intent`, or run it in parallel with the setup | Config only |
| H3 | The LLM call count, not per-call speed, drives hybrid latency | Hybrid has 3–4 `llm` entries while per-call `s` stays flat | Instruction tweaks so that independent tool calls are emitted together | Prompt change; quality risk |
| H4 | Input tokens grow per call (schema, docs summary, passages) | `in_tok` rises across a turn's calls and correlates with `llm_s` | Lower `retrieval.max_top_k` or `max_passage_chars` | Config only |
| H5 | The query embedding inside `search_documents` is a noticeable part of the tool time | `bm25` vs `hybrid` retrieval-mode latency; the tool's `s` | Keep hybrid (quality) or cache query embeddings | Low |
| H6 | A new `genai.Client()` per classification adds connection setup | `intent_s` on the first turn ≫ later turns | Reuse one module-level client | Small code change |
| H7 | Retries or errors inflate the tail | Rows with `run_sql` called twice (rule 11), and the error count | Better SQL guidance; this is not a latency fix in itself | — |

**Step 3 — Rank by expected gain × ease ÷ quality risk.** Change one lever per run and use
the existing dotted overrides, for example:

```bash
uv run python -m evals configs/eval/latency.yaml agent.intent_enabled=false name=lat_no_intent
uv run python -m evals configs/eval/latency.yaml agent.retrieval.max_top_k=4 name=lat_topk4
```

Default order, before any data: config-only levers (H2, H4) → eval-only fixes (H1 for eval)
→ small code fixes (H6) → prompt changes (H3) → app architecture (H1 for the app).

**Step 4 — Quality guardrail.** Accept a candidate only if its `query_s` p50 improves and
`tool_sequence_match`, `section_recall`, `answer_contains` and `citation_precision` stay
within one case (1/6 ≈ 0.17) of the baseline, judged per case and not only on the means.
Also check that the `refuse_write` case still refuses. A faster agent that skips a tool or
cites the wrong policy has not improved.

## 7. Tracking framework recommendation

| Option | Fit for this project | Verdict |
|---|---|---|
| **Run folders + JSON (current)** | Already the source of truth; diffable; no service; works offline | **Keep.** It is enough for phase 1 |
| **MLflow** (local `mlflow ui`, open source) | `design.md` §8 already reserves the hook: an `eval.tracker: files \| mlflow` sink next to `report.write_run`, mapping `config.yaml` to params and `metrics.json` (including the new `latency_stages`) to metrics. Its run-comparison UI is the missing piece once there are more than about 5 latency runs. MLflow 3 also accepts OpenTelemetry traces | **Recommended next step** for comparing runs. Add it after phase 1 so the latency fields exist first |
| **Arize Phoenix / Langfuse** (open source, self-hostable) | Both have Google ADK instrumentation through OpenTelemetry / OpenInference and draw a waterfall per trace | **Optional**, for visual debugging of single slow turns if the `timing` JSON is not enough. It adds a dependency and a local server |
| **Google Cloud Trace** | Built into ADK: `adk api_server --otel_to_cloud` on Cloud Run | **Use for deployed latency** (§8); no app code needed |
| **LangSmith** | Hosted SaaS built around LangChain (this project does not use LangChain); sends prompts and answers to a third party (HybridQA data is internal-use only); seat pricing after the free tier | **Not recommended** here |

In short: files now, MLflow for comparing runs, and Cloud Trace for production.
OpenTelemetry is the common format, so a later move to Phoenix or Langfuse needs no rework
of the measurement.

## 8. Later: deployed (user-perceived) latency

Measure this separately, and never mix it with the in-process numbers:

1. Deploy with `--otel_to_cloud` to get ADK's LLM and tool spans in Cloud Trace per request.
2. Time `ui_client` calls to `POST /run` from a script to get network plus server time. The
   `turn_timing` state delta also arrives in the `/run` events, so `parse_events` can read
   it there too.
3. Report Cloud Run instance cold start (about 30 s, per the README) as a separate number
   from session-data setup.

## 9. Implementation plan (one sprint, two phases)

| Phase | Work | Tests (`db-doc-qa/tests/`) | Dev script |
|---|---|---|---|
| 1 — Instrument | Edits 1–4 in §4 and `configs/eval/latency.yaml` | `test_events.py`: `turn_timing` on canned events (sequential, parallel tool batch, no tools, an error mid-turn) and the sum check. `test_runner.py`: `timing` is present and `turn_timing` is read from state. `test_eval_run.py`: a fake session with timing gives `latency_stages` and per-tag latency, and rows without timing still aggregate | `dev_test/phase_8_1.py`: one live question, print `timing` |
| 2 — Measure and rank | Run §5, fill in the §6 tables, run the top 2–3 experiments, and write the findings into a `latency_report.md` | None (analysis only) | `dev_test/phase_8_2.py`: print the stage table from a run folder |

MLflow (§7) is a separate, later phase.

## 10. Assumptions and limitations

- The event-arrival derivation assumes non-streaming runs, which is how `AgentSession`
  calls `run_async` today. If streaming is enabled later, the derivation must use the last
  partial event of each response.
- Tool calls run together by ADK are timed as one batch; per-tool times inside a batch are
  not separated.
- Subtracting setup gives the warm-query latency of a new session. It does not remove
  per-process effects, which `first_turn` covers, or provider-side caching.
- Gemini latency varies by time of day and by quota. Small samples support "which stage
  dominates" and "did this change help clearly", not small percentage claims.
- The hypotheses in §6 are suspicions drawn from reading the code, not measurements.

### Reassessment of the previous draft

The previous version was written from `README.md` and `design.md` only. After reading the
code, it was simplified:

- **Dropped:** span trees, parent/child IDs, interval-union arithmetic, a `request_id`, and a
  separate `traces.jsonl`. The turn is sequential, so a flat list of stages with a sum check
  is exact enough.
- **Dropped:** a 12-case new dataset. The existing 6 `shipments_v1` cases already cover
  every route, plus the conflict, gap and refusal cases.
- **Dropped:** proposed config fields (`measurement mode`, `cold/warm label`). The cold/warm
  split is derived from the setup timings instead.
- **Added:** the verified finding that `latency_s` includes ingestion and intent
  classification, and that README latencies from prebuilt bundles cannot be compared with
  `files_state` runs.
- **Added:** reuse of the setup timers that already exist, the token data already in the
  events, the existing retrieval mode and dotted overrides, and the tracker hook already
  reserved in `design.md`.
