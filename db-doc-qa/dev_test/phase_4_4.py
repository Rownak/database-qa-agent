"""Phase 4.4: runs the retrieval-mode evaluation with configs/eval/bm25_only.yaml (no LLM) and prints the metrics."""
from datetime import datetime

from data_agent.config import load_config
from evals.dataset import resolve
from evals.run import run_eval

cfg = load_config("configs/eval/bm25_only.yaml", ["name=phase_4_4"])
run_dir = resolve(cfg.eval.output_dir) / f"{datetime.now():%Y%m%d-%H%M%S}_{cfg.name}"
print(run_eval(cfg, run_dir))
print("run folder:", run_dir)
