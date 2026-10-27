import json
import sys
from pathlib import Path

# Usage: uv run python -m dev_test.phase_7_4 runs/<timestamp>_hybridqa_dev50_ollama
run = Path(sys.argv[1])
ours = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
official = json.loads((run / "official_scores.json").read_text(encoding="utf-8"))

print(f"{'type':8} {'ours EM':>8} {'official':>9} {'ours F1':>8} {'official':>9}")
for tag, key in (("table", "table"), ("passage", "passage"), ("hybridqa", "total")):
    m = ours["by_tag"][tag]["metrics"]
    print(f"{key:8} {100 * m['exact_match']:8.2f} {official[key + ' exact']:9.2f} {100 * m['squad_f1']:8.2f} {official[key + ' f1']:9.2f}")
