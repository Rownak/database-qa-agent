import json
from pathlib import Path

import yaml

from evals.hybridqa import prepare

OUT = Path("../benchmark/hybridqa_prepared/dev_s50")
info = prepare(Path("../benchmark/HybridQA"), Path("configs/eval/hybridqa_dev50.yaml"), OUT,
               per_type=25, seed=20260101, cache=OUT.parent / "raw")
print("cases per type:", info["sample"]["cases_per_type"], "unlabeled excluded:", info["sample"]["unlabeled_excluded"])
print("skipped:", [(s["qid"], s["reason"][:60]) for s in info["sample"]["skipped"]])
print("tables / passages / chunks:", info["counts"]["tables"], info["counts"]["passages"], info["counts"]["chunks"])

cases = yaml.safe_load((OUT / "dataset.yaml").read_text(encoding="utf-8"))["cases"]
for tag in ("table", "passage"):
    case = next(c for c in cases if tag in c["tags"])
    print(tag, "->", json.dumps(case, ensure_ascii=False)[:400])

state = json.loads((OUT / cases[0]["state_file"]).read_text(encoding="utf-8"))
print("schema_summary:", state["schema_summary"].splitlines()[:3])
print("docs_summary:", state["docs_summary"].splitlines()[:3])
