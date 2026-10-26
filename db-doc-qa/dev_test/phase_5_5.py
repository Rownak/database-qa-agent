"""Phase 5.5: reads a run folder and prints our metrics next to the official scorer's scores.json.

Usage: uv run python -m dev_test.phase_5_5 [run_folder]   (default: the newest lakequest run in runs/)
Run the official scorer first (see README, "LakeQuest evaluation").
"""
import json
import sys
from pathlib import Path

runs = sorted(Path("runs").glob("*_lakequest_bank_sample"))
run = Path(sys.argv[1]) if len(sys.argv) > 1 else runs[-1]
ours = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
official = json.loads((run / "official" / "scores.json").read_text(encoding="utf-8"))

print(f"run: {run}  cases: {ours['n']}  errors: {ours['errors']}")
print(f"{'metric':<12}{'ours':>8}{'official':>10}")
for name in ("answer_f1", "object_f1", "coverage"):
    print(f"{name:<12}{ours['metrics'][name]:>8.4f}{official[name]:>10.4f}")
print("answer_f1 equal:", abs(ours["metrics"]["answer_f1"] - official["answer_f1"]) < 1e-9)
