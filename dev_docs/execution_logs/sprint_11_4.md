# Sprint 11 — Phase 11.4: Registry, Headless Runner and Report

## Summary
`uv run python -m evals.adk.suite` now runs the selected suites *n* times, computes pass@1 / pass^3 / pass@k and the verdict, writes the report and exits 0, 1 or 2. The live path (`adk_evaluator`) has **not** been run (no Gemini call in this phase); everything else is tested offline with a fake evaluator that writes synthetic ADK result files. `uv run pytest`: **626 passed, 1 skipped** (the live `test_pr_gate`).

## What was implemented
- **Registry** `evals/adk/suites.yaml`: six suites with per-suite `agent_config`, tiers `pr` (n=3, min 2), `nightly` (n=5, min 4, mean pass@1 0.90) and `release` (n=10, min 9, 0.95, judges, calibration, `fail_on_regression`). Validated by pydantic (unknown tier, duplicate name).
- **Runner** `evals/adk/suite.py`: options `--tier`, `--suite` (repeatable), `--n`, `--baseline`, `--capture <id>`. Per suite it builds the agent from its config (`agent_from_config`, the Sprint 10 fixture pattern, restored afterwards), runs `AgentEvaluator.evaluate(num_runs=n)` with a `LocalEvalSetResultsManager` rooted in the run folder, catches the `AssertionError`, and reads that suite's own result files. Suites run one after another, in registry order.
- **Validity (§7.1)** in `evals/adk/stats.py`: a case-run is invalid when no gating metric produced a score (ADK's record of an API error or timeout), no invocation was recorded, or a search response has the "Semantic search was unavailable" note. Cases short of valid runs are rerun once on a filtered copy of the eval sets, and valid runs beyond *n* are ignored. A suite is INVALID when over 10 % of runs stay invalid, or any case has fewer valid runs than `min_success`. An evaluator exception is recorded as a reason and ends as INVALID, not a crash.
- **Statistics and verdict (§7.2-7.3):** per case c, n, pass@1, pass^3 = C(c,3)/C(n,3), pass@k (k = n), STABLE / FLAKY / BROKEN, failing metrics, and the actual tools of the first failed run. A variant group takes its worst member. Suite PASS needs critical cases STABLE, others c >= `min_success`, mean pass@1 >= the tier threshold, and (tiers with `fail_on_regression`, i.e. release) no regression. The tier verdict is FAIL (1) if any suite failed, else INVALID (2) if any is invalid, else PASS (0).
- **Report** `evals/adk/report.py`: `runs/adk/<timestamp>_<tier>/report.json`, `report.md` (the §8 dashboard), `eval_history/` (copies of the raw result files) and metadata (commit, instruction hash, golden hashes, models, tier, n, date, baseline). Regressions and fixed cases against `--baseline` are always listed; the coverage gaps come from the builder.
- **`--capture <id>`:** runs one golden case once on a filtered copy and prints per turn the question, the tool calls with arguments and the answer, for comparison with the YAML in 11.5.
- **pytest gate:** `tests/test_adk_eval.py::test_pr_gate` is now `suite.main(["--tier", "pr"]) == 0` (still `RUN_ADK_EVAL=1`); an offline test checks that it wraps the four pr suites.
- **Helper** `evals/adk/synthetic.py` writes ADK-format result files by hand (used by the tests and the dev script only).

## Decisions and assumptions
- **Failures beat INVALID in the tier exit code:** an observed regression in one suite is not masked by missing evidence in another.
- **Regressions gate only where the tier says so** (`fail_on_regression`, release). Design §7.3 makes it release-only but the nightly dashboard shows them, so they are reported everywhere.
- **`min_success` is capped at `--n`**, so `--n 2` on the release tier is satisfiable.
- **Variants count as eval cases** for the run total and the invalid share (unit_routing: 21 cases x 3 = 63 runs) but as one group in the "Cases" column.
- Latency and tokens come from ADK's informational metrics (`invocation_duration_v1`, `token_usage_v1`) when present; the real score format of those metrics is unverified and shows `-` / 0 otherwise.
- The release tier's judge metrics are carried as `judges: true` in the metadata only; wiring them is 11.5.
- `_work/` (raw per-suite output) is deleted after the files are copied to `eval_history/`.

## Files
- Created: `evals/adk/{suites.yaml,stats.py,report.py,suite.py,synthetic.py}`, `tests/test_adk_stats.py` (25 tests), `tests/test_adk_suite.py` (22 tests), `dev_test/phase_11_4.py`.
- Modified: `tests/test_adk_eval.py` (wrapper), `README.md` (suite commands), `dev_docs/sprints/sprint11.md`.

## Limitations
- `adk_evaluator`, the invalid-run detection on real ADK errors, the token/latency fields and the rerun behaviour with real `AgentEvaluator` are untested live (11.5).
- The rerun is one extra `evaluate` call with `max(short)` runs for every short case, so some cases get more runs than needed; the extras are ignored.
- A suite that cannot be built (missing manifest) raises instead of reporting INVALID.

## Commands
From `db-doc-qa/`:
```bash
uv run pytest tests/test_adk_stats.py tests/test_adk_suite.py -q   # 47 passed
uv run pytest                                                       # 626 passed, 1 skipped
uv run python -m dev_test.phase_11_4                                # dashboard of synthetic results
uv run python -m evals.adk.suite --help
# live (billed, needs GOOGLE_API_KEY): uv run python -m evals.adk.suite --tier pr
```
