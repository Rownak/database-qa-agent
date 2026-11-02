# Caching Report — Sprint 9

Measurements for the three caches of [caching_design.md](caching_design.md), one section per cache. Each section is added by the evaluation phase of that cache (9.2 response, 9.4 prefix, 9.6 retrieval); 9.7 adds the combined results and the final recommendation.

All numbers are from one machine, a non-deterministic model (`gemini-3.5-flash-lite`, temperature 0) and **one run per arm**, so they show direction and order of magnitude. Differences under the measured ±0.5 s run-to-run spread are noise. Passes are never pooled.

## Response cache (Phase 9.2)

### Setup

- **Workload** (94 turns, run in this order, every case a new session, `scope: dataset`, built by `dev_test/phase_9_2_build.py` from the 40 LakeQuest bank-sample cases):

  | Pass | Turns | Content |
  |---|---|---|
  | U unique | 40 | the originals (always cold: nothing is cached yet) |
  | E exact | 20 | verbatim copies of half A (10 hybrid, 5 table, 5 document) |
  | P paraphrase | 20 | frozen paraphrases of half B; ids, names, numbers, quoted text and policy ids checked equal to the original, and read once by hand |
  | N near miss | 10 | an original with another client (name and id) or another policy |
  | S stale | 4 | originals asked on mutated bundles: 10 transactions and 1 account removed (the counts and the precomputed `client.n_transactions` follow), two policies removed from `docs.db` |

- **Arms** (fresh process each; `configs/eval/lakequest_cache.yaml` plus dotted overrides):

  | Arm | Overrides |
  |---|---|
  | base | none (no app cache; the provider's own caching is untouched) |
  | exact | `response.enabled=true` |
  | shadow | `response.enabled=true semantic=true shadow=true` (never serves a semantic hit) |
  | sem95 | `response.enabled=true semantic=true threshold=0.95` (added after the sweep, to check the chosen threshold when serving) |

- **Correction during the phase.** The first stale probe on `client 62` was wrong: the agent read the precomputed `client.n_transactions` instead of counting rows, so it answered 359 on the mutated copy in every arm. The mutation now updates that column too (the dataset file is unchanged) and the four stale cases were re-run, with the 4 originals they depend on, for base, exact and sem95 (`*_stalefix_*` runs). The S results below are from those runs; the S rows inside the full runs are superseded.

### Cold overhead (pass U, all misses by construction)

| Arm | p50 / p95 / max (s) | `cache_lookup` stage | Verdict |
|---|---|---|---|
| base | 3.10 / 6.56 / 8.33 | - | |
| exact | 3.07 / 5.16 / 6.36 | median 0.3 ms, max 36 ms (first call hashes the 31 MB `bank.db`) | within the ≤ 0.05 s gate; p50 inside the noise band |
| sem95 | 3.79 / 6.28 / 7.13 | median 305 ms, max 386 ms (one query embedding) | within the ≤ 0.4 s gate; p50 +0.69 s is **outside** ±0.5 s |

The stage is the precise number. The sem95 p50 difference is larger than the 0.3 s the lookup costs, and the other passes of the same run moved by as much (N p50 2.93 → 4.59 s), while the exact arm, whose lookup costs 0.3 ms, shows the same kind of shift on pass N (2.93 → 4.19 s). With n = 10 to 40 per pass that is run-to-run drift, not a cost of the cache beyond the lookup stage; it is not resolved by this single run.

### Repeat (warm) benefit

| Pass | Arm | Hits | p50 / p95 / max (s) | Agent calls | Input tokens | Classifier calls |
|---|---|---|---|---|---|---|
| E exact | base | - | 3.02 / 4.69 / 5.22 | 44 | 336 k | 20 |
| | exact | 17/20 | 0.01 / 4.77 / 6.71 | 9 | 87 k | 3 |
| | sem95 | 18/20 | 0.00 / 3.02 / 5.68 | 6 | 47 k | 2 |
| P paraphrase | base | - | 3.66 / 6.56 / 8.69 | 60 | 495 k | 20 |
| | exact | 0/20 | 3.55 / 7.49 / 7.96 | 64 | 535 k | 20 |
| | sem95 | 13/20 | 0.30 / 5.86 / 8.73 | 25 | 206 k | 7 |

- An exact hit costs about 5 ms (p50 5 ms, max 9 ms). A semantic hit costs the query embedding, about 0.3 s.
- **Not hit:** E has 2 bypasses and 1 miss in the exact arm. P has 4 bypasses and 3 misses in sem95. All 15 bypasses of a run are `time_relative` (see Findings). The one E miss is a case whose original turn had a tool error (a search call without `query`), which the cache correctly refuses to store; the same case was stored and hit in sem95.
- Whole-run totals (94 turns): latency 352 s → 288 s (exact) → 264 s (sem95); agent model calls 252 → 217 → 178; classifier calls 94 → 77 → 63; input tokens 2.04 M → 1.75 M → 1.47 M (-28 %); output tokens 16.1 k → 14.1 k → 11.0 k. Cost is not computed (no prices were supplied); the token counts are the saving.

### Quality

- **Pass U** cannot be changed by the cache (every turn is a miss with the same prompts), yet 2/40 (exact) and 4/40 (sem95) cases differ from the baseline by more than 0.2 on some metric (largest drop 1.00 on one metric of one case). That is the model's own run-to-run variation and is the floor for the per-case gate; a drop of that size elsewhere cannot be blamed on the cache.
- **Hits return the stored answer.** Exact copies and their intent are identical to the originals (checked for all 17). Paraphrase hits scored mean `answer_f1` 0.35 against 0.32 for the baseline's fresh answers on the same cases; no paraphrase case dropped by more than 0.18 on any metric.
- Overall `answer_f1`, `object_f1` and `tool_sequence_match` of the exact arm are 0.33, 0.71 and 1.00 against the baseline's 0.33, 0.70 and 0.98, so within 0.02.

### Invalidation

| Check | exact | sem95 |
|---|---|---|
| Pass S served from cache | **0 / 4** | **0 / 4** |
| Pass S answers match the post-change truth | counts 349 and 2; both policies reported as not found | same |
| Pass N served from cache | **0 / 10** (7 looked up, 3 bypassed) | **0 / 10** (7 looked up, 3 bypassed) |

Both hard gates hold. In the stale runs all four cases were looked up (`miss`), so the changed data and document fingerprints did the work, not the bypass rules.

### Threshold sweep (shadow run, no extra model calls)

Counts for 20 paraphrases (P: served from the right case, 16 had a neighbour, 4 were bypassed), 10 near misses (N: served at all; 7 had a neighbour) and the 40 unique questions (U: served from another question). "Guard" is the shipped entity guard; "+ids" also compares hyphenated words such as policy ids (recomputed offline from the question texts).

| τ | P right: no guard | P right: guard | P right: guard + ids | N false: no guard | N false: guard | N false: guard + ids | U false |
|---|---|---|---|---|---|---|---|
| 0.90 | 16 | 14 | 14 | 1 | 0 | 0 | 0 |
| 0.92 | 16 | 14 | 14 | 1 | 0 | 0 | 0 |
| 0.93 | 15 | 13 | 13 | 0 | 0 | 0 | 0 |
| 0.95 | 15 | **13** | 13 | 0 | 0 | 0 | 0 |
| 0.96 | 13 | 11 | 11 | 0 | 0 | 0 | 0 |
| 0.97 (default) | 9 | 8 | 8 | 0 | 0 | 0 | 0 |
| 0.98 | 5 | 4 | 4 | 0 | 0 | 0 | 0 |
| 0.99 | 1 | 1 | 1 | 0 | 0 | 0 | 0 |

- Paraphrase similarities: min 0.928, median 0.972, max 0.990. Near-miss similarities: 0.714 to 0.928. The nearest near miss (0.928) is a client swap that only the guard blocks below τ = 0.93.
- The guard blocks 2 of the 16 paraphrases that had a neighbour (a rewritten identifier token), the price of a guard that never lets a wrong id through.
- **Policy swaps pass the guard** (`guard: True` for all three): hyphenated policy ids are not in its token set. They are stopped by similarity alone (0.71 to 0.85).
- **Chosen threshold: 0.95.** It keeps a 0.02 margin above the highest near miss and serves 13 of the 20 paraphrases against 8 at the 0.97 default. Serving it live (sem95) gave exactly those 13 hits, 0 false hits and 0 stale answers. The config default is still 0.97; changing it is a decision for 9.7.

### Does the semantic tier pay for itself?

Break-even hit rate = lookup cost / mean turn time = 0.30 s / 3.10 s = **9.6 to 9.8 %** (0.297 s in the shadow run, 0.305 s in sem95; the design estimated 9 %). The semantic tier added 13 hits to the exact tier on 94 turns = **13.8 %**, so it passes on this workload: net, the 13 hits save about 13 × 3.3 s and the 61 semantic lookups cost about 61 × 0.3 s, a gain of about 25 s over the run (measured: 288 s → 264 s). The result depends on the paraphrase share of the workload (20 % here); below about 10 % paraphrase hits the exact tier alone is the better choice.

### Gates (design §7.5)

| Gate | Result |
|---|---|
| Pass N served = 0, pass S served = 0 | met (0, 0) in both arms |
| Quality: misses unchanged | met as far as measurable: pass U differences equal the model's own variation (2/40 and 4/40 cases beyond 0.2 although no turn could be affected) |
| Exact tier ≥ 95 % of eligible E turns | **17/18 = 94.4 %** in the exact arm (just under; the one miss is a correctly unstored turn, 17/17 of stored originals hit); 18/18 in sem95 |
| Semantic tier: zero false hits and p·T > t_lookup | met (0 false hits; 13.8 % > 9.8 %) |
| Overhead on unique questions: ≤ 0.05 s exact, ≤ 0.4 s semantic | met (max 36 ms; max 386 ms) |
| Pass-U p50 inside ±0.5 s of base | exact met (-0.03 s); sem95 **not** (+0.69 s, see the note under Cold overhead) |

### Findings and open points

1. **The time-relative rule bypasses 15 of 94 turns (16 %) and 6 of the 40 originals**, mostly hybrid questions that say "recent activity" or "current". In this static dataset those words are relative to the data, not to the clock, so the rule costs hits (hybrid E hit rate is 7/10 and 8/10, tables and documents 5/5). It is the safe default; whether a dataset can opt out is for 9.7.
2. **The entity guard has a hole for policy ids.** Hyphenated policy names are not identifier-like to it. On this sample similarity alone kept them out (0.71 to 0.85), but that is a margin, not a guarantee. Adding hyphenated words to the token set does not change any count above ("+ids"); it would cost hits on paraphrases that rewrite a hyphenated phrase ("first-line"). Decide in 9.7.
3. **A turn that had a tool error is never stored**, including a recoverable one (the model corrects the call and answers). This is the intended conservative rule; it explains the one E miss.
4. Single run per arm; the hit rates describe this synthetic repeat mix, not production traffic; paraphrase quality is a property of the frozen set; seven of ten near misses were looked up.

### Reproduce

```bash
uv run python -m dev_test.phase_9_2_build                                   # once; frozen paraphrases are reused
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_base
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_exact agent.cache.response.enabled=true
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_shadow agent.cache.response.enabled=true \
    agent.cache.response.semantic=true agent.cache.response.shadow=true
uv run python -m evals configs/eval/lakequest_cache.yaml name=cache_sem95 agent.cache.response.enabled=true \
    agent.cache.response.semantic=true agent.cache.response.threshold=0.95
uv run python -m dev_test.phase_9_2 runs/<base> runs/<arm>                  # per-pass comparison
uv run python -m dev_test.phase_9_2_sweep runs/<shadow> runs/<base>         # threshold sweep and break-even
```

Run folders (git-ignored, in `db-doc-qa/runs/`): `20261102-012520_cache_base`, `20261102-013114_cache_exact`, `20261102-013605_cache_shadow`, `20261102-014212_cache_sem95`, and the corrected stale probes `20261102-014648_cache_stalefix_base`, `20261102-014713_cache_stalefix_exact`, `20261102-014738_cache_stalefix_sem95`.
