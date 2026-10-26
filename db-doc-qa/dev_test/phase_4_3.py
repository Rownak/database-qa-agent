"""Phase 4.3: runs the first 2 evaluation cases on the live agent (needs GOOGLE_API_KEY) and prints the results."""
from datetime import datetime

from data_agent.config import load_config
from evals.dataset import resolve
from evals.run import run_eval

cfg = load_config("configs/eval/smoke.yaml", ["dataset.limit=2", "name=phase_4_3"])
run_dir = resolve(cfg.eval.output_dir) / f"{datetime.now():%Y%m%d-%H%M%S}_{cfg.name}"
run_eval(cfg, run_dir)
print((run_dir / "metrics.json").read_text())
print("run folder:", run_dir)
