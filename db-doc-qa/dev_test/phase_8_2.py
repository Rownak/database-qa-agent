import json
import sys
from pathlib import Path

GUARDED = ["tool_sequence_match", "section_recall", "answer_contains", "citation_precision"]


def cell(value, width):
    return f"{value:>{width}.2f}" if value is not None and value is not False else f"{'-':>{width}}"


def newest(pattern):
    return sorted(Path("runs").glob(pattern))[-1]


# usage: uv run python -m dev_test.phase_8_2 [baseline_run_dir candidate_run_dir]
if len(sys.argv) == 3:
    baseline, candidate = Path(sys.argv[1]), Path(sys.argv[2])
else:
    baseline, candidate = newest("*_lat_base_*"), newest("*_lat_no_intent")
a, b = (json.loads((r / "metrics.json").read_text()) for r in (baseline, candidate))
print(f"baseline:  {baseline.name}\ncandidate: {candidate.name}\n")

sa, sb = a["latency_stages"], b["latency_stages"]
print(f"{'stage p50 (s)':<18}{'baseline':>10}{'candidate':>11}")
for name in dict.fromkeys([*sa["stages"], *sb["stages"]]):  # a stage that did not run shows as "-"
    pa, pb = sa["stages"].get(name), sb["stages"].get(name)
    print(f"{name:<18}{cell(pa and pa['p50_s'], 10)}{cell(pb and pb['p50_s'], 11)}")
print(f"{'query p50':<18}{sa['query_p50_s']:>10.2f}{sb['query_p50_s']:>11.2f}")
print(f"{'query p95':<18}{sa['query_p95_s']:>10.2f}{sb['query_p95_s']:>11.2f}")

print(f"\n{'guarded metric':<22}{'baseline':>10}{'candidate':>11}")
for m in GUARDED:
    va, vb = a["metrics"].get(m), b["metrics"].get(m)
    print(f"{m:<22}{cell(va, 10)}{cell(vb, 11)}")
