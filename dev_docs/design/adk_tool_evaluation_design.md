# ADK Tool-Trajectory Evaluation: Design

Status: proposed · Source: `dev_docs/tutorial/adk_eval_tool_trajectory.md`

## 1. Goal

Add an automated, repeatable `adk eval` regression test that checks **which tools `qa_orchestrator` calls, and in what order**, for the bundled shipment demo. It sits alongside the existing harness (`python -m evals`); it does not replace it. It uses ADK's own eval-set format, so the same files run from the ADK CLI, from `adk web`, and from pytest/CI.

Out of scope: answer-text scoring (the existing harness does this with `answer_contains` and citation checks), and changes to the agent, tools or instruction.

## 2. Why the tutorial's setup has to change

| Tutorial (procurement agent) | This agent | Consequence |
|---|---|---|
| Tool args are short enum/ID values (`item_id`, `department`) | `run_sql(query)` and `search_documents(query, …)` take text the model writes | Exact arg matching would fail at random → compare **tool names only** (`ignore_args: true`) |
| One fixed SOP path | Instruction rule 1 says a single-intent question calls one tool only; rule 2 allows an extra unfiltered search on hybrid questions | Two eval sets: **EXACT** for single-intent cases, **IN_ORDER** for hybrid cases |
| Mock catalog JSON | `shipments.csv` + 3 policy docs, loaded by `ensure_data` when the session state is empty | `session_input.state: {}` is enough; no fixtures |

Argument correctness (the tutorial's second concern) is covered by a small custom metric (§5) that checks for key fragments, not full strings.

## 3. Proposed files (minimal; no changes to agent code)

```
db-doc-qa/evals/adk/
  routing/
    routing.test.json      # single-tool and no-tool cases
    test_config.json       # EXACT, names only
  hybrid/
    hybrid.test.json       # cases that need SQL and documents
    test_config.json       # IN_ORDER, names only
  __init__.py, metrics.py  # tool_args_contain custom metric (§5)
db-doc-qa/tests/test_adk_eval.py   # live pytest wrapper, opt-in with RUN_ADK_EVAL=1
db-doc-qa/dev_test/phase_10_3.py   # scorecard over data_agent/.adk/eval_history (§7)
.gitignore                         # + data_agent/.adk/
```

Each folder holds its own `test_config.json`, because `AgentEvaluator` reads the config that sits next to the test file and ADK allows one match type per config.

## 4. Eval cases (bundled data only)

Questions come from `evals/datasets/shipments_v1.yaml`, plus two cases that cover instruction rules 3 and 5. Each case tests one routing rule.

**routing**: `match_type: EXACT` (an extra "just in case" tool call fails the case)

| eval_id | Question | Golden tools | Rule |
|---|---|---|---|
| `db_location` | Where is shipment 12345? | `run_sql` | 1 (database) |
| `db_count_delayed` | How many shipments are delayed? | `run_sql` | 1 (aggregate) |
| `doc_express_threshold` | What is the escalation threshold for express shipments? | `search_documents` | 1 (document) |
| `refuse_write` | Delete all delivered shipments. | *(none)* | 12 (read-only) |

**hybrid**: `match_type: IN_ORDER` (the golden calls must appear in this order; extra calls are allowed)

| eval_id | Question | Golden tools | Rule |
|---|---|---|---|
| `hybrid_delayed_express` | Shipment 12345 is delayed. What should I do? | `run_sql` → `search_documents` | 2 |
| `conflict_international` | When must shipment 12347 be escalated? | `run_sql` → `search_documents` | 2, 7 |
| `no_policy_refrigerated` | Shipment 12349 is delayed. What should I do? | `run_sql` → `search_documents` | 2, 6 |
| `record_not_found` | Shipment 99999 is delayed. What should I do? | `run_sql` → `search_documents` | 5 |
| `threshold_first` | Which express shipments are past their escalation threshold? | `search_documents` → `run_sql` | 3 |

Example case (the format from the tutorial; `args` holds key fragments, which the built-in metric ignores and §5 reads):

```json
{
  "eval_id": "hybrid_delayed_express",
  "conversation": [{
    "invocation_id": "inv_001",
    "user_content": {"role": "user", "parts": [{"text": "Shipment 12345 is delayed. What should I do?"}]},
    "final_response": {"role": "model", "parts": [{"text": ""}]},
    "intermediate_data": {
      "intermediate_responses": [],
      "tool_uses": [
        {"name": "run_sql", "args": {"query": "12345"}},
        {"name": "search_documents", "args": {"query": "express"}}
      ]
    }
  }],
  "session_input": {"app_name": "data_agent", "user_id": "evaluator", "state": {}}
}
```

`refuse_write` uses `"tool_uses": []`. It has to live in the EXACT set: under IN_ORDER an empty golden list matches any trajectory.

## 5. Scoring config

`routing/test_config.json` (in `hybrid/`, the only change is `"match_type": "IN_ORDER"`):

```json
{
  "criteria": {
    "tool_trajectory_avg_score": {"threshold": 1.0, "match_type": "EXACT", "ignore_args": true},
    "tool_args_contain": 1.0
  },
  "custom_metrics": {
    "tool_args_contain": {"code_config": {"name": "evals.adk.metrics.tool_args_contain"}}
  }
}
```

- `response_match_score` is left out, so no text comparison runs (the tutorial sets it to 0.0 for the same reason).
- Threshold 1.0: every case must match its golden trajectory.

**Argument check.** `evals/adk/metrics.py::tool_args_contain(eval_metric, actual_invocations, expected_invocations, conversation_scenario) -> EvaluationResult`. The golden calls are taken in order; each is matched with the first later actual call of the same name whose arguments contain all its fragments (case-insensitive substrings, for example the SQL contains `12345`). A call of that name with other arguments, such as a first SQL attempt that was then corrected, is skipped. The score is the share of golden calls matched; an empty golden list scores 1.0 (calls that should not happen are the trajectory metric's job). This keeps argument checks deterministic without depending on the exact SQL the model writes.

How ADK treats a custom metric (found while building it):
- ADK clears the metric's own `threshold` before calling the function, so the function sets pass/fail itself and reads the threshold from `eval_metric.criterion` (1.0 if absent).
- It must return exactly one per-invocation result per actual invocation; any other count raises a hard error. An invocation without a golden counterpart is `NOT_EVALUATED`.
- ADK always appends four informational metrics (tool call count, inference call count, token usage, invocation duration) to every result. They never pass or fail, and the scorecard leaves them out.
- The metric is loaded by dotted path, so `adk eval` needs `PYTHONPATH=.` (see §7).

## 6. Determinism

- Run with `DATA_AGENT_CONFIG=configs/eval/smoke.yaml` (temperature 0.0, response cache off by default), so `root_agent` loads the eval settings.
- An empty `state` makes `ensure_data` load `shipments.csv` and the three sample docs. The first turn embeds the docs, which needs `GOOGLE_API_KEY`.
- The intent classifier is part of the system under test, because routing depends on it.
- The pytest wrapper passes `num_runs=3` to `AgentEvaluator.evaluate` to expose flaky routing; any run below 1.0 fails. The `adk eval` CLI (ADK 2.12) has no repeat flag and runs each case once.

## 7. How to run

From `db-doc-qa/`, with `GOOGLE_API_KEY` in `.env` and the dev dependencies installed (`google-adk[eval]` is in the `dev` group; `uv sync` installs it). Bash syntax; in PowerShell, set `$env:PYTHONPATH="."` and `$env:DATA_AGENT_CONFIG="configs/eval/smoke.yaml"` first. `PYTHONPATH=.` is needed because `adk eval` imports the package by file path and does not add `db-doc-qa/` to the import path (`adk web .` and pytest do).

```bash
# CLI: one command per eval set (each uses its own config)
PYTHONPATH=. DATA_AGENT_CONFIG=configs/eval/smoke.yaml uv run adk eval data_agent \
  evals/adk/routing/routing.test.json \
  --config_file_path evals/adk/routing/test_config.json --print_detailed_results

PYTHONPATH=. DATA_AGENT_CONFIG=configs/eval/smoke.yaml uv run adk eval data_agent \
  evals/adk/hybrid/hybrid.test.json \
  --config_file_path evals/adk/hybrid/test_config.json --print_detailed_results

# pytest / CI: AgentEvaluator.evaluate("data_agent", "evals/adk/<set>", num_runs=3) per folder (opt-in, billed)
RUN_ADK_EVAL=1 uv run pytest tests/test_adk_eval.py -v

# Scorecard: newest result per eval set, one row per case (worst run), trajectory and args scores
uv run python -m dev_test.phase_10_3

# Interactive: open adk web, pick data_agent, load the eval set under the Eval tab
uv run adk web .
```

Results: pass/fail per case on stdout. Full trajectories and scores go to `data_agent/.adk/eval_history/<app>_<eval_set_id>_<timestamp>.evalset_result.json` (an `EvalSetResult`; a case run `num_runs` times has that many entries). The scorecard reads these files. An API failure (for example HTTP 402) is recorded as a failed case with no metric scores, which the scorecard marks "no scores" so it is not mistaken for a routing failure.

Rerun after any change to `INSTRUCTION`, tool docstrings or signatures, the intent prompt, or the model version.

## 8. Limitations

- Names-only scoring cannot tell a correct SQL query from a wrong one; §5 checks fragments only, and answer correctness stays with `python -m evals`.
- Under EXACT, a legitimate SQL retry (rule 11) fails the case. Questions in `routing` are kept simple so that a retry is rare, and a failure there is worth a look anyway.
- Live model calls make this suite slow and billed, so it is opt-in (`RUN_ADK_EVAL=1`) and skipped in the default offline `uv run pytest`. A key-only skip condition is not enough, because the key is normally in `.env`.
- The golden argument fragments are provisional until both sets have run against the real model; a fragment the model words differently (for example `delay` vs `delayed`) shows as "tools right, arguments wrong" in the scorecard and should be loosened, not the prompt tuned.
