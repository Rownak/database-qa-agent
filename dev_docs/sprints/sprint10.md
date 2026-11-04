# Sprint 10 — ADK Tool-Trajectory Evaluation

**Goal:** add an automated, repeatable regression test of the agent's tool use, built from native ADK evaluation pieces: eval sets (`*.test.json`), `test_config.json` criteria, `adk eval`, `AgentEvaluator` in pytest, and `.adk/eval_history`. Design: [adk_tool_evaluation_design.md](../design/adk_tool_evaluation_design.md). It runs alongside `python -m evals` and does not replace it.

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Default tests are offline; live tests are skipped without `GOOGLE_API_KEY`
- a short script `db-doc-qa/dev_test/phase_10_N.py`, run with `uv run python -m dev_test.phase_10_N`
- an entry in `dev_docs/execution_logs/sprint_10_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 10.1 | Golden eval sets and `adk eval` from the CLI |
| 10.2 | Regression gate: `AgentEvaluator` in pytest, repeated runs |
| 10.3 | Argument correctness (custom metric) and eval-history scorecard |

**Rules for all phases**

- **No agent changes.** `data_agent/agent.py`, `tools.py`, the instruction and the tool docstrings stay as they are. If a case fails, record it in the execution log as a finding; do not tune the prompt to make it pass.
- **Native ADK first.** Use the built-in `tool_trajectory_avg_score` (`match_type`, `ignore_args`) and ADK's `custom_metrics` hook. No custom runner, no custom eval-set format.
- **Bundled demo data only.** Every case has `session_input.state: {}`, so `ensure_data` loads `shipments.csv` and the three sample documents.
- **Deterministic settings.** Live runs use `DATA_AGENT_CONFIG=configs/eval/smoke.yaml`: temperature 0.0, response cache off.
- **One concern per case.** Each eval case tests one instruction rule (design §4).

---

## Phase 10.1 — Golden Eval Sets and `adk eval`

**Goal:** the two eval sets from design §4 run from the ADK CLI and pass, with full results in `.adk/eval_history`.

- [ ] **Eval-ready check:** confirm `uv run adk web .` lists `data_agent` (`__init__.py` → `agent.py` → `root_agent`). Ask one demo question and look at its tool calls in the trace viewer.
- [ ] **Capture one case in `adk web`:** answer "Where is shipment 12345?", save it as an eval case from the Eval tab, and compare the generated JSON with the format in the design. Use it as the template for the hand-written cases.
- [ ] **`evals/adk/routing/`:**
  - `routing.test.json` with `db_location`, `db_count_delayed`, `doc_express_threshold`, and `refuse_write` (empty `tool_uses`: the baseline "no tool" behaviour)
  - `test_config.json`: `tool_trajectory_avg_score` with `{threshold: 1.0, match_type: EXACT, ignore_args: true}`, and no `response_match_score`
- [ ] **`evals/adk/hybrid/`:**
  - `hybrid.test.json` with `hybrid_delayed_express`, `conflict_international`, `no_policy_refrigerated`, `record_not_found`, and `threshold_first`
  - `test_config.json` as in `routing`, but with `match_type: IN_ORDER`
- [ ] **Golden args:** fill each golden call's `args` with key fragments (for example `{"query": "12345"}`). Built-in scoring ignores them; 10.3 reads them.
- [ ] **`.gitignore`:** add `data_agent/.adk/`.
- [ ] **Run both sets** with `adk eval … --config_file_path … --print_detailed_results`. Record pass/fail per case in the execution log.
- [ ] **Tests (offline)** `tests/test_adk_evalsets.py`:
  - every `*.test.json` validates as ADK `EvalSet`, and every `test_config.json` as ADK `EvalConfig`
  - eval ids are unique, and every golden tool name is a tool of `root_agent`
  - `refuse_write` is in the EXACT set (an empty golden list matches anything under IN_ORDER)
- [ ] **Dev script** `phase_10_1.py`: print each case's id, question and golden tool sequence.

## Phase 10.2 — Regression Gate in pytest

**Goal:** one pytest command runs both eval sets several times, so tool routing can be checked after any change to the instruction, tools, intent prompt or model.

- [ ] **`tests/test_adk_eval.py`:** one async test per folder that calls `AgentEvaluator.evaluate("data_agent", "evals/adk/<set>", num_runs=3)`. It sets `DATA_AGENT_CONFIG` to `smoke.yaml` before the agent module is imported, and is skipped without `GOOGLE_API_KEY`.
- [ ] **Confirm the default suite stays offline:** `uv run pytest` passes and reports the live tests as skipped.
- [ ] **Flakiness record:** run the gate three times. In the execution log, list any case that does not score 1.0 every time, with its actual trajectory from `eval_history`.
- [ ] **README:** a short "ADK tool evaluation" subsection with the CLI and pytest commands, and when to rerun them.
- [ ] **Dev script** `phase_10_2.py`: run the routing set once through `AgentEvaluator` and print pass/fail.

## Phase 10.3 — Argument Correctness and Scorecard

**Goal:** check that the right values reach the tools without matching the model's exact SQL, and summarise the eval history.

- [ ] **Custom metric** `evals/adk/metrics.py::tool_args_contain`: it uses the ADK custom-metric signature and returns `EvaluationResult`.
  - It pairs each golden call with an actual call of the same name, in order.
  - A call scores 1.0 when every golden arg value is a case-insensitive substring of the actual arg.
- [ ] **Register the metric** in both `test_config.json` files under `custom_metrics` (`code_config.name: evals.adk.metrics.tool_args_contain`), with threshold 1.0.
- [ ] **Tests (offline)** `tests/test_adk_metrics.py`:
  - build `Invocation` objects by hand; a matching fragment passes, and a missing fragment or a missing call fails
  - check matching on extra calls and on an empty golden list
- [ ] **Run both sets** with the new metric and record the results. Note any case where the tool order passes but the arguments fail.
- [ ] **Dev script** `phase_10_3.py` (scorecard): read the latest files in `data_agent/.adk/eval_history/` and print a case × metric table (trajectory score, args score, pass/fail).
- [ ] **Design doc:** update `adk_tool_evaluation_design.md` with anything learned (format details, flaky cases).

---

## Out of Scope

- Scoring answer text (`response_match_score`, an LLM judge): `python -m evals` already covers answers and citations.
- Multi-turn cases, the LakeQuest profile and uploaded data.
- A CI pipeline file; the pytest gate is the command a pipeline would run.
- Self-correction cases (a SQL retry after an error) as golden trajectories.
