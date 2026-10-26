"""Phase 5.4: runs lakequest_bank.yaml on the first 3 bank_sample cases with the live agent and prints each answer, answer_f1 and object_ids."""
import json
from datetime import datetime

from data_agent.config import load_config
from evals.dataset import resolve
from evals.run import run_eval

cfg = load_config("configs/eval/lakequest_bank.yaml", ["name=phase_5_4", "dataset.limit=3"])
run_dir = resolve(cfg.eval.output_dir) / f"{datetime.now():%Y%m%d-%H%M%S}_{cfg.name}"
metrics = run_eval(cfg, run_dir)
for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    print(f"\n{row['id']}: {row['question'][:90]}")
    print("answer:", row["answer"][:300])
    print("answer_f1:", row["metrics"]["answer_f1"], "| object_ids:", row["object_ids"], "| error:", row["error"])
print("\nmeans:", metrics["metrics"])
print("run folder:", run_dir)
