# Sprint 11 — ADK Evaluation at Scale

**Goal:** turn the Sprint 10 eval sets into a release gate: golden data by behaviour layer with rule coverage, answer and citation checks, multi-turn and adversarial suites, and one headless command that runs every suite *n* times and reports one verdict with pass@1 / pass^k and an exit code. Design: [adk_eval_at_scale_design.md](../design/adk_eval_at_scale_design.md).

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests passing (`uv run pytest`). Default tests are offline; live runs are opt-in
- a short script `db-doc-qa/dev_test/phase_11_N.py`, run with `uv run python -m dev_test.phase_11_N`
- an entry in `dev_docs/execution_logs/sprint_11.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 11.1 | Golden YAML, builder and coverage checks (migrate the Sprint 10 sets) |
| 11.2 | Answer and citation metrics |
| 11.3 | Author the suites: unit, memory, adversarial, regression |
| 11.4 | Registry, headless runner, statistics and report |
| 11.5 | End-to-end validation: live tiers, baseline, calibration |

**Rules for all phases**

- **No agent changes.** A failing case is a finding for the execution log; do not tune the prompt or edit a golden case to make it pass (design §4.3).
- **Native ADK first.** Eval sets, `test_config.json`, `custom_metrics`, `rubrics`, `AgentEvaluator` and `eval_history` are the building blocks; the new code is only the builder, the metrics, the runner and the report.
- **Phases 11.1–11.4 are offline.** They are tested with hand-built invocations and result files, so they can be finished without Gemini credits. Only 11.5 calls the model.
- **Reuse:** `evals/metrics.py` scoring functions, the Sprint 10 `tool_args_contain`, the `smoke_agent` fixture pattern, and the `phase_10_3` scorecard parsing.

---

## Phase 11.1 — Golden YAML, Builder and Coverage

**Goal:** golden cases are written in YAML and generated into committed ADK eval sets, with coverage and instruction coupling checked offline (design §4).

- [x] **Golden schema** (pydantic, strict) for a suite file: `suite`, `instruction_sha`, cases with `id`, `layer`, `rules`, `critical`, `state`, `variants`, `trajectory: any`, and `turns` (`q`, `tools`, `contains`, `contains_sql`, `excludes`, `sections`).
- [x] **State presets** `evals/golden/states/`: `default`, `no_docs`, `injection_docs`, `corrupt_upload` (design §4.2).
- [x] **Builder** `evals/adk/build.py` (`uv run python -m evals.adk.build`), which writes, per suite:
  - `<suite>.test.json`: one invocation per turn, tool fragments in `tool_uses`, answer checks as typed `rubrics`, state from the preset, variants as `<id>__vN`
  - `manifest.json`: layer, rules, critical, group per case
  - `contains_sql` values computed on the bundled `shipments.csv`
- [x] **Builder validation:** reject an empty golden tool list without `trajectory: any`, unknown tools, duplicate ids, and a match type that does not fit the folder; flag redundant cases.
- [x] **Migrate** Sprint 10's `routing` and `hybrid` into `golden/unit_routing.yaml` and `golden/unit_hybrid.yaml` with layer-prefixed ids; generate them into `evals/adk/unit_routing/` and `evals/adk/unit_hybrid/`, and remove the old folders.
- [x] **Coverage matrix:** the builder prints rule × case counts for `INSTRUCTION` rules 1–14, with rule 11 as a known gap (design §4.4).
- [x] **Tests (offline):**
  - generated JSON equals the committed JSON (stale check)
  - every rule has a case, and failure-mode rules have a negative or edge case
  - `instruction_sha` matches the current `INSTRUCTION` and tool docstrings
  - builder rejections; `contains_sql` fills the right value
  - update `test_adk_evalsets.py` to the new folders
- [x] **Dev script** `phase_11_1.py`: build, then print the coverage matrix and the case count per suite.

## Phase 11.2 — Answer and Citation Metrics

**Goal:** the four new gating metrics score a final answer and its passages the same way the harness does (design §6).

- [x] **Adapter** in `evals/adk/metrics.py`: an ADK `Invocation` (final response, tool calls, tool responses) and its rubrics become the inputs of the `evals/metrics.py` functions.
- [x] **Metrics** `answer_contains`, `answer_excludes`, `section_retrieved` and `citation_grounded`. Each returns one result per invocation, takes its threshold from the criterion, and is `NOT_EVALUATED` when the invocation has no matching rubric (or, for `citation_grounded`, no citation).
- [x] **Register** all six gating metrics in every suite's `test_config.json`, written by the builder.
- [x] **Tests (offline):**
  - pass and fail for each metric on hand-built invocations
  - an invocation without the rubric is `NOT_EVALUATED`, and ADK's final status ignores it
  - the same answer gives the same score through the adapter and through the harness function
  - every metric runs through ADK's `_CustomMetricEvaluator` from the real config
- [x] **Dev script** `phase_11_2.py`: score one hand-written answer with all metrics and print the results.

## Phase 11.3 — Author the Suites

**Goal:** the initial cases of design §3.1 exist as golden YAML and build cleanly.

- [ ] **`unit_routing` and `unit_hybrid`:** add the remaining unit and edge cases, with document fragments and sections copied from the policy text and data values from `contains_sql`.
- [ ] **`memory`:** the four multi-turn cases, with checks on each turn.
- [ ] **`adversarial`:** the seven cases, split into `adversarial/exact` and `adversarial/in_order` under one suite; the injection case uses the `injection_docs` preset.
- [ ] **`regression`:** `reg_empty_state_build` in `regression`. Put `reg_followup_not_cached` in `regression_cache`, which needs a new `configs/eval/cache_session.yaml` (`smoke.yaml` with `cache.response.enabled: true`, `scope: session`).
- [ ] **Variants:** 1–2 paraphrases for every critical case and every `unit_*` happy path (design §3.2).
- [ ] **Review** each case against the checklist in design §4.3 and mark critical cases (⚑).
- [ ] **Tests (offline):** all suites build; the coverage test passes with the full set; critical ids match design §3.1.
- [ ] **Dev script** `phase_11_3.py`: print each suite's cases with layer, rules, critical flag and golden tools per turn.

## Phase 11.4 — Registry, Headless Runner and Report

**Goal:** one command runs the selected suites *n* times, computes the statistics and the verdict, writes the report and exits 0, 1 or 2 (design §5, §7, §8).

- [ ] **Registry** `evals/adk/suites.yaml` with the suites, per-suite `agent_config`, and the `pr`, `nightly` and `release` tiers (`n`, `min_success`, `suite_mean_pass1`).
- [ ] **Runner** `evals/adk/suite.py`:
  - options `--tier`, `--suite`, `--n`, `--baseline`, `--capture <id>`
  - per suite: build the agent from its config, run `AgentEvaluator.evaluate(num_runs=n)` with a results manager, catch the assertion and collect the result files of this run
  - suites run in order
- [ ] **Validity:** classify invalid runs (no metric results, or a BM25-fallback note in a search response), rerun them once, and apply the 10 % and `min_success` rules (design §7.1).
- [ ] **Statistics and verdict:**
  - per case: c, n, pass@1, pass^3, pass@k and STABLE / FLAKY / BROKEN; variant groups take their worst member
  - per suite: the criteria of design §7.3, with regressions against `--baseline`
- [ ] **Report:** `runs/adk/<timestamp>_<tier>/` with `report.json`, `report.md` (dashboard as in design §8), copied `eval_history/` and run metadata (commit, instruction hash, models, tier, n).
- [ ] **pytest gate:** `tests/test_adk_eval.py` becomes a wrapper around `suite.py --tier pr` (still `RUN_ADK_EVAL=1`).
- [ ] **Tests (offline, synthetic result files):**
  - pass@1, pass^k and pass@k against hand-computed values, including n=5, c=1
  - class and verdict rules, critical cases, variant groups
  - INVALID on too many invalid runs; exit codes 0, 1 and 2
  - regression detection against a baseline report
  - report files and dashboard content
- [ ] **Dev script** `phase_11_4.py`: run the report step on a folder of synthetic result files and print the dashboard.

## Phase 11.5 — End-to-End Validation

**Goal:** the gate runs live on the real agent, produces the first baseline, and is calibrated by a human review. This phase needs working Gemini credits.

- [ ] **Smoke:** `suite.py --suite unit_routing --n 1`; confirm results, report and exit code.
- [ ] **Capture check:** capture three golden cases (one unit, one memory, one adversarial) in `adk web` or with `--capture`, and compare them with the YAML. Record any differences.
- [ ] **`pr` tier** run; record the verdict and every non-stable case with its actual trajectory.
- [ ] **`nightly` tier** run; keep it as the first baseline, then rerun with `--baseline` to confirm the run-to-run spread.
- [ ] **Invalid handling:** confirm with one forced failure (for example an invalid API key in a single run) that the suite is reported INVALID with exit 2, not FAIL.
- [ ] **Human calibration** on the nightly run: 10 passing runs and all failing ones. Record agreement, too-lenient and too-strict checks, and the resulting fragment fixes.
- [ ] **Release tier, once:** run with the judges, and compare `hallucinations_v1` with the human labels; record whether the judge can be trusted.
- [ ] **Thresholds:** revisit `min_success` and `suite_mean_pass1` from the baseline, and record any change with its reason.
- [ ] **Docs:**
  - README: replace the Sprint 10 section with the suite and tier commands and the first baseline result
  - design doc: update with what was learned (§11 open points)
- [ ] **Tests:** none unless code changes; confirm `uv run pytest` passes.
- [ ] **Dev script** `phase_11_5.py`: print the dashboard of the latest run folder.

---

## Out of Scope

- A CI pipeline file and hosted scheduling: the exit codes are ready for one.
- Agent or prompt changes driven by eval findings: a later sprint.
- New metrics in the harness (`python -m evals`), LakeQuest and HybridQA suites, and a table large enough to cover rule 11 (truncation).
