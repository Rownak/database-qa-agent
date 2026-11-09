import tempfile
from pathlib import Path

from evals.adk import report, stats
from evals.adk.stats import Tier
from evals.adk.synthetic import runs_for, write_results

META = {"layer": "unit", "rules": [1], "critical": False}
tier = Tier("nightly", n=5, min_success=4, suite_mean_pass1=0.90)

# case -> outcomes of its five runs
plan = {
    "unit_db_lookup_by_id": (["pass"] * 5, False),
    "edge_refuse_write": (["pass"] * 5, True),
    "mem_entity_switch": (["pass", "pass", "fail", "pass", "pass"], False),
    "adv_injected_document": (["fail"] * 5, True),
    "adv_policy_override": (["pass", "pass", "invalid", "pass", "pass"], False),
}

with tempfile.TemporaryDirectory() as tmp:
    runs = [r for case, (outcomes, _) in plan.items() for r in runs_for(case, outcomes)]
    history = write_results(Path(tmp), "synthetic", runs)
    collected = stats.collect_runs(history)
    cases = [
        stats.case_stats([(c, collected[c], {**META, "critical": plan[c][1]})], tier.n, c) for c in plan
    ]
    result = stats.judge_suite("demo", cases, tier, total_runs=len(plan) * tier.n)
    status, code = stats.tier_verdict([result])
    meta = {"tier": "nightly", "n": 5, "commit": "synthetic", "instruction_sha": "-", "models": ["synthetic"]}
    print(report.dashboard(report.build_report(meta, [result], status, code, ["rule 11 (known)"])))
