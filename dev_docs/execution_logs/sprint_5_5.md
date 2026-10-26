# Sprint 5 — Phase 5.5 Execution Log: Run and Official Check

## Summary
All 40 `bank_sample` cases were run with the live agent, twice (before and after one adjustment of `LAKE_INSTRUCTION`). On the second run our `answer_f1`, `object_f1` and `coverage` **equal the official scorer's** (0.3371, 0.7113, 1.0000). The first comparison found a gap that was fixed in the case generator (below). `uv run pytest`: **306 passed**. The scores are internal only; some of these questions may be in the hidden test set.

## Official check and the gap it found
First run (`runs/20261026-133346_lakequest_bank_sample`): ours `answer_f1` 0.321 against the official 0.285. `object_f1` (0.683) and `coverage` already matched.
- **Cause:** per case, the 35 cases that both scorers score were identical. The 5 "which section covers …" cases had only `answer_contains` and no `reference_answers`, so `answer_f1` was `None` for us (and left out of the mean), while `references.jsonl` gives the number as `answer_short`, so the official scorer scored them (about 0.03 each). The means had different denominators (35 against 40).
- **Fix:** the generator now also sets `reference_answers: [<number>]` for those cases (`evals/lakequest.py`), and a test checks it. The bundle was rebuilt (same 2,481 chunks, same cases otherwise).

## Result (second run, `runs/20261026-133728_lakequest_bank_sample`)
Command: `uv run python -m evals configs/eval/lakequest_bank.yaml`. 40 cases, **0 errors**, 2 min 17 s.

| | `tool_sequence_match` | `section_recall` | `answer_contains` | `citation_precision` | `answer_f1` | `object_f1` | `coverage` |
|---|---|---|---|---|---|---|---|
| **overall** | 0.97 | 0.79 | 1.00 | 0.90 | **0.34** | **0.71** | 1.00 |
| hybrid (20) | 0.95 | 0.58 | – | 0.84 | 0.33 | 0.59 | 1.00 |
| table (10) | 1.00 | – | 1.00 | – | 0.31 | 0.66 | 1.00 |
| document (10) | 1.00 | 1.00 | 1.00 | 1.00 | 0.38 | 1.00 | 1.00 |

- Latency: p50 2.8 s, p95 6.3 s (slowest case 7.5 s).
- Model calls: 73 tool calls in 40 cases, so about **113 model calls** (one per tool call plus one final answer per case), plus one query embedding per search. Intent classification is off.
- Official scorer on the same run (`official/scores.json`): `answer_f1` 0.3371, `object_f1` 0.7113, `coverage` 1.0, `n_predictions` 40. `dev_test/phase_5_5.py` prints both side by side; `answer_f1 equal: True`.

## Reading the results
Lowest-scoring row per type (second run) and its class:

| Type | Case | `answer_f1` | Class |
|---|---|---|---|
| hybrid | qa-1139840cb0 | 0.17 | **Wrong policy.** It looked up the client, but cited `customer-profile-updates` instead of `high-risk-authentication-access-restore`, and so reached the opposite decision (not eligible). The question does not name the policy |
| table | tbl-2 | 0.22 | **Correct but differently worded.** "Dennis Sherman (client_id: 623) has 278 transactions." is right; the reference is the single token "278", so any sentence scores low |
| document | doc-1 | 0.03 | **Correct but too long.** It names section 6 and describes it; the reference is just "6" |

`answer_f1` is therefore a relative signal: for the table and section cases the reference is one token, so the maximum score of a sentence is low even when the answer is right. `answer_contains` (1.00 for table and document) and `tool_sequence_match` show correctness better there. The weak spot is the **hybrid** type: wrong policy choice, `section_recall` 0.58 and `object_f1` 0.59.

**One instruction adjustment** (the first run showed a format cause) in `LAKE_INSTRUCTION`: look up the client with `run_sql` first and never search before it; a count or value question gets one short sentence with that value; without a Policy ID, search with the key terms of the situation and cite the policy whose title fits best.

| Change from run 1 to run 2 | Before | After |
|---|---|---|
| `tool_sequence_match` overall / hybrid | 0.82 / 0.65 | 0.97 / 0.95 |
| `answer_f1` table | 0.11 | 0.31 |
| `object_f1` overall | 0.68 | 0.71 |
| `citation_precision` | 0.89 | 0.90 |
| latency p50 / p95 | 2.9 s / 7.0 s | 2.8 s / 6.3 s |

`answer_f1` overall went from 0.32 to 0.34 in our metric (0.285 to 0.337 in the official one, partly the section-case fix). Model output varies between runs, and each version ran once, so a change of a few hundredths is within noise; the tool order and the table answers are clear improvements. I did not change the instruction again, as the sprint allows one adjustment.

## What was done
- **Runs, official check and reading:** as above.
- **Generator fix** (`evals/lakequest.py`) and its test (`tests/test_lakequest_prepare.py`).
- **`LAKE_INSTRUCTION`** (`data_agent/agent.py`): the three edits above. The shipments instruction is untouched.
- **Docs:** a "LakeQuest evaluation" section in `README.md` with the `prepare`, run and official-scorer commands.
- **Dev script** `dev_test/phase_5_5.py`: reads a run folder and prints our metrics next to `official/scores.json` (default: the newest `*_lakequest_bank_sample` run).

## Limits
- Hybrid cases are judgments ("Eligible – approve with controls…"), so token F1 against `answer_direct` and `answer_reasoning` is noisy; 10 of the 20 have no `sections`, so `section_recall` covers only the others.
- 40 cases and one run per version: no confidence interval. Use `eval.repeats` for finer comparisons.
- The remaining hybrid failures are about choosing the right policy when the question does not name it. A larger or different retrieval setting (`max_top_k`, hybrid weights) was not tried.

## Files
- Modified: `data_agent/agent.py`, `evals/lakequest.py`, `tests/test_lakequest_prepare.py`, `README.md`, `dev_docs/sprints/sprint5.md` (ticked 5.5)
- Created: `dev_test/phase_5_5.py`, `dev_docs/execution_logs/sprint_5_5.md`
- Outputs (git-ignored): `runs/20261026-133346_lakequest_bank_sample`, `runs/20261026-133728_lakequest_bank_sample` (with `official/`), the rebuilt `benchmark/lakequest_prepared/bank_sample/`

## Commands
```
cd db-doc-qa
uv run pytest                                  # 306 passed
uv run python -m evals configs/eval/lakequest_bank.yaml
RUN=runs/20261026-133728_lakequest_bank_sample
LAKEQUEST_REFERENCE_DIR=../benchmark/lakequest_prepared/bank_sample/reference LAKEQUEST_RESULT_DIR=$RUN \
  LAKEQUEST_SCORE_OUTPUT_DIR=$RUN/official uv run python ../benchmark/LakeQuest/scoring_program/score.py
uv run python -m dev_test.phase_5_5 $RUN
```
