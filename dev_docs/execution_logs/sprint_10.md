# Sprint 10 — Execution Log

## Phase 10.1 — Golden Eval Sets and `adk eval` (partially complete)

### Summary
The two eval sets (9 cases), their configs, offline tests and the dev script are done, and `adk eval` loads them. **The live parts are not done: no case has been run against the agent.** Every Gemini call (embedding, intent classifier, agent model) returns `402 RESOURCE_EXHAUSTED: Your prepayment credits are depleted`. Three tasks stay unticked in [sprint10.md](../sprints/sprint10.md) until the credits are topped up: asking a demo question in `adk web`, capturing a case from it, and running both sets.

### What was implemented
- **`evals/adk/routing/`**: `routing.test.json` (`db_location`, `db_count_delayed`, `doc_express_threshold`, `refuse_write`) and `test_config.json` with `tool_trajectory_avg_score: {threshold 1.0, match_type EXACT, ignore_args true}`.
- **`evals/adk/hybrid/`**: `hybrid.test.json` (`hybrid_delayed_express`, `conflict_international`, `no_policy_refrigerated`, `record_not_found`, `threshold_first`) and the same config with `IN_ORDER`.
- **Golden args** are key fragments (for example `{"query": "12345"}`). The built-in score ignores them; 10.3 reads them.
- **`.gitignore`**: `db-doc-qa/data_agent/.adk/` (eval history and dev sessions).
- **Dev dependency**: `google-adk[eval]>=2.12.0` added to the `dev` group in `pyproject.toml` (and `uv.lock`).
- **Tests** `tests/test_adk_evalsets.py` (8 tests, offline).
- **Dev script** `dev_test/phase_10_1.py`.
- **Docs**: the design's run commands now include `PYTHONPATH=.` and the `[eval]` extra.

### Approach and key decisions
- The JSON files were generated once by a throwaway script (kept out of the repo) in the format of the tutorial and the design. The committed files are plain JSON.
- The tests use ADK's own `EvalSet` and `EvalConfig` models, so they reject anything `adk eval` would reject. They also check that every golden tool is a tool of `root_agent`, ids are unique, `refuse_write` is only in the EXACT set (an empty golden list matches anything under IN_ORDER), and each hybrid case expects both tools.
- The extra goes in the `dev` group, not in `dependencies`: deployment installs from `data_agent/requirements.txt` and `requirements-ui.txt`, so the image does not grow. It pulls in about 1,700 lock lines (pandas, scikit-learn, rouge-score, and others).
- The golden arg fragments are provisional. They have not been compared with real model calls, so 10.3 may need to adjust them.

### What was verified
- `adk web . --port 8765` lists `["data_agent"]` (the package structure is eval-ready).
- `adk eval data_agent "evals/adk/routing/routing.test.json:refuse_write" --config_file_path …` loads the set and parses the criteria (`EXACT`, `ignore_args=True`), then fails on the first model call with the 402. The "FAILED" it printed is an API failure, not a routing result. I deleted the `eval_history` file that run wrote, so that it cannot be mistaken for one.

### Problems found
- **Exhausted Gemini credits** (the blocker above). Fix: top up at AI Studio, then do the three open tasks.
- **`adk eval` needs `PYTHONPATH=.`.** Without it, importing the package fails with `No module named 'data_agent'`, because `adk eval` loads the agent by file path (`adk web .` and pytest add the directory themselves).
- **`adk eval` needs the `[eval]` extra.** Without it: `Eval module is not installed`.
- **An `adk web` server may still be running** on port 8765, started during the check. I could not stop it (process termination was denied). Close that terminal or end the process.

### Files
- Created: `evals/adk/routing/routing.test.json`, `evals/adk/routing/test_config.json`, `evals/adk/hybrid/hybrid.test.json`, `evals/adk/hybrid/test_config.json`, `tests/test_adk_evalsets.py`, `dev_test/phase_10_1.py`, this log.
- Modified: `.gitignore`, `db-doc-qa/pyproject.toml`, `db-doc-qa/uv.lock`, `dev_docs/design/adk_tool_evaluation_design.md`, `dev_docs/sprints/sprint10.md` (done tasks ticked).

### Commands and results
From `db-doc-qa/`:

```bash
uv run pytest                          # 494 passed (8 new)
uv run pytest tests/test_adk_evalsets.py -q   # 8 passed
uv run python -m dev_test.phase_10_1   # prints the 9 cases and their golden tool sequences

# Live (needs working Gemini credits):
PYTHONPATH=. DATA_AGENT_CONFIG=configs/eval/smoke.yaml uv run adk eval data_agent \
  evals/adk/routing/routing.test.json \
  --config_file_path evals/adk/routing/test_config.json --print_detailed_results
PYTHONPATH=. DATA_AGENT_CONFIG=configs/eval/smoke.yaml uv run adk eval data_agent \
  evals/adk/hybrid/hybrid.test.json \
  --config_file_path evals/adk/hybrid/test_config.json --print_detailed_results
```

### Still to do in this phase (needs credits)
- [ ] Ask one demo question in `adk web` and look at the trace; save it as an eval case and compare its JSON with the hand-written format.
- [ ] Run both sets and record pass/fail per case here.

---

## Phase 10.2 — Regression Gate in pytest (partially complete)

### Summary
The pytest gate, its offline check, the README section and the dev script are done, and the default suite stays offline (**495 passed, 2 skipped**). **The gate has never run against the model:** the Gemini credits are still exhausted (HTTP 402, re-checked with a one-line call at the start of this phase), so the flakiness record is not done and the gate's live pass/fail is unverified. The one task left open in [sprint10.md](../sprints/sprint10.md) is the flakiness record.

### What was implemented
- **`tests/test_adk_eval.py`**:
  - `test_tool_trajectory[routing|hybrid]` calls `AgentEvaluator.evaluate("data_agent", "evals/adk/<set>", num_runs=3, app_name="data_agent", eval_set_results_manager=LocalEvalSetResultsManager(<db-doc-qa>))`. Every case must match its golden trajectory in all 3 runs, and the results are written to `data_agent/.adk/eval_history/`, as `adk eval` does.
  - `smoke_agent` fixture: sets `DATA_AGENT_CONFIG` to `configs/eval/smoke.yaml`, builds the agent with `build_agent(load_agent_config())` and patches `data_agent.agent.root_agent` for the test.
  - `test_smoke_fixture_applies_eval_settings` (offline, always runs): the patched agent has temperature 0.0 and the response cache is off.
- **README**: new "ADK tool evaluation" subsection (pytest and CLI commands, when to rerun).
- **Dev script** `dev_test/phase_10_2.py`: runs the routing set once through `AgentEvaluator` and prints `routing: PASS` or `routing: FAIL` plus the failure text.

### Key decisions (two differ from the sprint wording)
- **Live tests are opt-in (`RUN_ADK_EVAL=1`), not "skipped without `GOOGLE_API_KEY`".** The key is in `.env`, which `data_agent.agent` loads at import, so a key-only condition would call the billed API on every `uv run pytest` (and, now, fail with the 402). The tests still skip when the key is missing.
- **Sync tests with `asyncio.run`, not async tests.** The repo has no pytest-asyncio and already uses `asyncio.run` in tests.
- **The fixture patches `root_agent` instead of only setting the environment variable.** `AgentEvaluator` reads `data_agent.agent.root_agent` from the already imported module, and earlier tests in the same session import it with default settings, so the variable alone would leave the temperature unset.
- **One gate run is enough for the flakiness record.** `num_runs=3` already runs each case three times; the sprint's "run the gate three times" was redundant (corrected in sprint10.md).

### What was verified
- `uv run pytest tests/test_adk_eval.py -q -rs`: 1 passed, 2 skipped (reason shown).
- `uv run pytest`: 495 passed, 2 skipped.
- `uv run python -m dev_test.phase_10_2` reaches the model for each routing case and prints `routing: FAIL` with "failed without producing any metric results" and the 402 in the log. That wording marks an API failure, not a trajectory mismatch (a mismatch reports a score below the threshold). The run wrote a result file to `eval_history`; I deleted it so it cannot be read as a real result.

### Problems found
- **Exhausted Gemini credits** (same blocker as 10.1). After a top-up, run `RUN_ADK_EVAL=1 uv run pytest tests/test_adk_eval.py -v`, then fill the flakiness section below.
- **The `adk web` server on port 8765 from 10.1 was not confirmed stopped**; close it if it is still open.

### Files
- Created: `tests/test_adk_eval.py`, `dev_test/phase_10_2.py`.
- Modified: `README.md`, `dev_docs/sprints/sprint10.md`, this log.

### Commands and results
From `db-doc-qa/`:

```bash
uv run pytest                                   # 495 passed, 2 skipped (live gate skipped)
uv run pytest tests/test_adk_eval.py -q -rs     # 1 passed, 2 skipped
uv run python -m dev_test.phase_10_2            # live: needs credits; prints routing: PASS / FAIL

# Live gate (opt-in, billed):
RUN_ADK_EVAL=1 uv run pytest tests/test_adk_eval.py -v
```

### Flakiness record (to fill in after a live run)
| Case | Runs below 1.0 | Actual trajectory (from `eval_history`) |
|---|---|---|
| *not run yet* | | |

---

## Phase 10.3 — Argument Correctness and Scorecard (partially complete)

### Summary
The `tool_args_contain` metric, its registration in both configs, the scorecard and the offline tests are done and the full suite passes (**510 passed, 2 skipped**). **Both eval sets are still unrun against the model:** the Gemini credits are exhausted (HTTP 402, re-checked at the start of this phase), so the one open task is "run both sets with the new metric and record the results". That also leaves the 10.1 live tasks and the 10.2 flakiness record open. No routing or argument result exists yet, and the golden argument fragments are unverified against real model output.

### What was implemented
- **`evals/adk/metrics.py::tool_args_contain`** (plus `evals/adk/__init__.py` so the dotted path imports). The golden calls are taken in order; each is matched with the first later actual call of the same name whose arguments contain all its fragments (case-insensitive substrings). The score is the share of golden calls matched; an empty golden list scores 1.0.
- **Registration**: both `test_config.json` files now have `"tool_args_contain": 1.0` in `criteria` and the `custom_metrics.tool_args_contain.code_config.name = "evals.adk.metrics.tool_args_contain"` entry.
- **Scorecard** `dev_test/phase_10_3.py`: reads the newest `*.evalset_result.json` per eval set in `data_agent/.adk/eval_history/` and prints one row per case: runs, worst trajectory score, worst args score, PASS/FAIL, the tools the agent actually called (from the first failed run), and a note. The note says "tools right, arguments wrong" (the case the sprint asks to flag) or "no scores (inference failed?)".
- **Tests**: `tests/test_adk_metrics.py` (11 tests, with hand-built `Invocation` objects and a run through ADK's own `_CustomMetricEvaluator` built from the real config files), `tests/test_adk_scorecard.py` (4 tests, on hand-built result files), and `tests/test_adk_evalsets.py` updated for the new criteria (the config test now also checks the registration).
- **Docs**: design doc §3, §5, §7, §8 updated; one README line for the scorecard.

### Key decisions
- **Matching skips a same-name call with other arguments.** A corrected SQL retry therefore still counts, as long as a later call carries the fragment. A wrong first attempt is not penalised by this metric; under the EXACT set the trajectory metric still fails the extra call.
- **An empty golden list scores 1.0** instead of 0: `refuse_write` has nothing to check here, and a forbidden call is the trajectory metric's job.
- **Threshold is read from `eval_metric.criterion`.** ADK clears `eval_metric.threshold` before calling a custom metric (found in `custom_metric_evaluator.py`), so reading `.threshold` would always give `None`. A test runs the metric through ADK's evaluator to cover this.
- **The scorecard shows the worst run per case.** With `num_runs=3` a case appears several times in one file; the minimum shows a flaky case instead of averaging it away.
- **The scorecard shows only the two gating metrics.** ADK always appends four informational metrics (tool count, inference count, token usage, duration) that never pass or fail.

### What was verified
- `uv run pytest`: 510 passed, 2 skipped (the live gate).
- `adk eval … --config_file_path evals/adk/routing/test_config.json` loads the new config and prints `custom_metrics={'tool_args_contain': CustomMetricConfig(code_config=…evals.adk.metrics.tool_args_contain…)}`, so the dotted path is accepted by the real CLI. The case itself failed on the 402.
- `uv run python -m dev_test.phase_10_3` on the real result file that run wrote parsed it and printed `FAIL … no scores (inference failed?)`, so the scorecard reads ADK's real file format and does not report an API failure as a routing result. I deleted that result file afterwards.

### Problems found
- **Exhausted Gemini credits** (same blocker as 10.1 and 10.2).
- **The golden fragments are guesses.** Examples that may need loosening after the first live run: `Delayed` for `db_count_delayed` (the model may write `status = 'Delayed'` or count differently), and `express` in a search query when the model puts the type in `section` instead. The scorecard's "tools right, arguments wrong" note exists to find these. The design doc says to loosen the fragment, not tune the prompt.
- **The `adk web` server on port 8765 from 10.1 may still be running**; close it.

### Files
- Created: `evals/adk/__init__.py`, `evals/adk/metrics.py`, `dev_test/phase_10_3.py`, `tests/test_adk_metrics.py`, `tests/test_adk_scorecard.py`.
- Modified: `evals/adk/routing/test_config.json`, `evals/adk/hybrid/test_config.json`, `tests/test_adk_evalsets.py`, `README.md`, `dev_docs/design/adk_tool_evaluation_design.md`, `dev_docs/sprints/sprint10.md`, this log.

### Commands and results
From `db-doc-qa/`:

```bash
uv run pytest                                   # 510 passed, 2 skipped
uv run pytest tests/test_adk_metrics.py tests/test_adk_scorecard.py tests/test_adk_evalsets.py -q   # 23 passed
uv run python -m dev_test.phase_10_3            # scorecard over data_agent/.adk/eval_history

# Live, once credits are available (billed). Runs both sets with both metrics, 3 runs each:
RUN_ADK_EVAL=1 uv run pytest tests/test_adk_eval.py -v
uv run python -m dev_test.phase_10_3
```

### Sprint 10 — what remains (all needs working Gemini credits)
- 10.1: ask a question in `adk web` and capture a case; run both sets with `adk eval`.
- 10.2: the flakiness table (above) from one gate run.
- 10.3: the live run with the argument metric; fix any provisional golden fragment it shows to be wrong.
