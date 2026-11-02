# Sprint 9 — Phase 9.2 Execution Log: LakeQuest Evaluation of the Response Cache

## Summary
The response cache was measured on a 94-turn LakeQuest workload (unique, exact repeat, paraphrase, near miss, stale), with the threshold set from data. Both hard gates hold: **0 of 10 near misses and 0 of 4 stale probes were served from cache**, in the exact arm and in the semantic arm. Exact repeats answer in about 5 ms (17 of 20 turns hit; 15 of 94 turns are bypassed as time-relative). The semantic tier at **τ = 0.95** served 13 of 20 paraphrases and passes its break-even (13.8 % vs about 9.7 %). Full numbers and findings are in [caching_report.md](../design/caching_report.md) (§Response).

## What was implemented
- **Workload builder** (`dev_test/phase_9_2_build.py`): writes `benchmark/lakequest_prepared/bank_sample_cache/` (git-ignored with `benchmark/`).
  - U: the 40 originals. E: 20 verbatim copies of half A (every other case per route: 10 hybrid, 5 table, 5 document). P: 20 paraphrases of half B, generated once with Gemini, frozen in `paraphrases.json`, accepted only when numbers, ids, quoted text, file names, client names and policy ids equal the original, and read once by hand. N: 10 near misses (4 hybrid and 3 table with another client name and id, 3 document with another policy). S: 4 stale probes with `expected` set to the post-change truth.
  - Stale bundles are mutated copies (the benchmark bundle is untouched): `stale_data/bank.db` (10 transactions of client 62 and one account of client 552 removed, `client.n_transactions` updated) and `stale_docs/docs.db` (two policies removed from documents, chunks and the search index), each with its own `state.json`, used through per-case `state_file`. The dataset-level `state.json` points at the original bundle, and `prepare.json` is copied next to it for the settings guard.
  - Each case carries its pass tag (`pass_unique`, `pass_exact`, `pass_paraphrase`, `pass_near_miss`, `pass_stale`) next to its route tag.
- **Config** `configs/eval/lakequest_cache.yaml`: `lakequest_bank.yaml` on the derived dataset, `latency_trace: true`, every cache off, `response.scope: dataset`.
- **Hit-rate aggregation** (`evals/run.py`, `evals/__main__.py`): a row gets `cache` only when the turn reported one; `metrics["cache"] = {"all", "by_tag"}` counts outcomes per layer (`n`, `hits`, `hit_rate`, `outcomes`, and bypass `reasons`), generically for any string-valued key of `TurnResult.cache` except the detail keys `reason`, `entry`, `error`, `intent`. `format_summary` prints one line per layer. With no cache the keys of `results.jsonl` and `metrics.json` are unchanged.
- **Comparison script** `dev_test/phase_9_2.py`: two run folders, per pass and per route: hits, p50 / p95 / max, agent and classifier calls, tokens, mean guarded metrics, per-case quality delta, `cache_lookup` stage. Importable functions (`load_run`, `pass_summary`, `quality_delta`, `table`), reused by 9.4, 9.6 and 9.7.
- **Threshold sweep** `dev_test/phase_9_2_sweep.py`: from a shadow run, hits for τ = 0.90 to 0.99 with no guard, the shipped guard and a guard that also compares hyphenated ids (recomputed offline from the question texts); the nearest entry is labelled with its case in the analysis only. Prints the break-even.
- **Report**: new `dev_docs/design/caching_report.md` with §Response.
- **Runs** (each a fresh process): base, exact, shadow (`semantic=true shadow=true`), and a fourth arm `sem95` (`semantic=true threshold=0.95`) added to check the chosen threshold while serving. Run folders are listed in the report.

## Key decisions
- **Fourth arm.** The plan listed three runs; the shadow run predicted 13 paraphrase hits and 0 false hits at 0.95, and `sem95` confirmed those exact numbers while serving.
- **Shadow does not switch off the exact tier**, so in the shadow arm pass E still hits.
- **Chosen threshold 0.95** (0.02 above the highest near miss, 13 vs 8 paraphrase hits compared with 0.97). I did **not** change the config default (0.97): that is left for the 9.7 decision.
- **The entity guard was not changed**, although it does not cover hyphenated policy ids (reported as a finding with the offline "+ids" variant).

## Problems found and fixed during the phase
- **Aggregation counted detail strings as layers.** The first runs printed `cache entry: ...` and `cache reason: ...` as layers with a hit rate. The aggregation was fixed (detail keys excluded, bypass reasons counted) before the `sem95` run, and the `metrics.json` of the three earlier runs was re-written from their `results.jsonl` with the fixed function. Tests were added.
- **A wrong stale probe.** The agent answered "How many transactions does client 62 have?" from the precomputed `client.n_transactions` column, so the mutated bundle still gave 359. The mutation now updates that column; the dataset file did not change. The four stale cases (with the four originals they depend on) were re-run for base, exact and sem95 as `*_stalefix_*` runs, and the report's S results come from those. The S rows in the three full runs are superseded (their served-from-cache count was 0 as well).

## Files
- Created: `dev_test/phase_9_2_build.py`, `dev_test/phase_9_2.py`, `dev_test/phase_9_2_sweep.py`, `configs/eval/lakequest_cache.yaml`, `tests/test_eval_cache.py` (5 tests), `tests/test_cache_report.py` (6 tests), `dev_docs/design/caching_report.md`, this log.
- Modified: `evals/run.py`, `evals/__main__.py`, `dev_docs/sprints/sprint9.md` (Phase 9.2 ticked).
- Generated, not committed: `benchmark/lakequest_prepared/bank_sample_cache/` and the run folders under `db-doc-qa/runs/`.

## Tests
- Command (from `db-doc-qa/`): `uv run pytest` → **486 passed** (475 before this phase, plus 11 new).
- New tests: outcome counting overall and per tag with a fake session, generic layers, detail keys and bypass reasons, unchanged keys without a cache, the summary line; the run comparison (hits and usage per pass, errored rows, quality delta, one table row per pass) and the threshold sweep (right and wrong hits, the guard blocking 252 → 253, the threshold, case-id mapping).

## Results at a glance
| | base | exact | sem95 |
|---|---|---|---|
| Pass U p50 (cold) | 3.10 s | 3.07 s | 3.79 s |
| `cache_lookup` median / max | - | 0.3 ms / 36 ms | 305 ms / 386 ms |
| Pass E hits, p50 | - / 3.02 s | 17/20, 0.01 s | 18/20, 0.00 s |
| Pass P hits, p50 | - / 3.66 s | 0/20, 3.55 s | 13/20, 0.30 s |
| Pass N / S served from cache | - | 0 / 0 | 0 / 0 |
| Agent calls, input tokens (94 turns) | 252, 2.04 M | 217, 1.75 M | 178, 1.47 M |
| Total latency (94 turns) | 352 s | 288 s | 264 s |

## Run it
```bash
uv run pytest
uv run python -m dev_test.phase_9_2_build            # once (needs a Gemini key the first time, for the paraphrases)
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_base
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_exact agent.cache.response.enabled=true
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_shadow agent.cache.response.enabled=true agent.cache.response.semantic=true agent.cache.response.shadow=true
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_sem95 agent.cache.response.enabled=true agent.cache.response.semantic=true agent.cache.response.threshold=0.95
uv run python -m dev_test.phase_9_2 runs/<base> runs/<arm>
uv run python -m dev_test.phase_9_2_sweep runs/<shadow> runs/<base>
```

## Notes and limitations
- One run per arm: the pass-U p50 of `sem95` (+0.69 s) is outside the ±0.5 s noise band although the lookup costs 0.3 s; the exact arm shows similar unexplained drift on pass N, so it is reported as unresolved, not as a cache cost.
- The exact-tier gate (≥ 95 % of eligible E turns) is missed by one case in the exact arm (17/18): its original turn had a tool error and was correctly not stored. It is 18/18 in `sem95`.
- 3 of 10 near misses and 4 of 20 paraphrases were bypassed as time-relative, so 7 near misses and 16 paraphrases were actually looked up.
- The per-case quality gate cannot be checked more tightly than the model's own variation: pass U, which the cache cannot change, shows 2/40 and 4/40 cases beyond 0.2.
- The hit rates describe this synthetic mix (20 % exact, 20 % paraphrase) and the frozen paraphrase set, not production traffic.
- Two follow-ups for 9.7: the time-relative rule (16 % of turns bypassed on a static dataset) and the policy-id gap in the entity guard.
