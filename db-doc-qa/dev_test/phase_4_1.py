"""Phase 4.1: loads configs/eval/smoke.yaml with an override and prints the resolved config as YAML."""
import yaml

from data_agent.config import load_config

cfg = load_config("configs/eval/smoke.yaml", ["agent.retrieval.min_dense_sim=0.55"])
print(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
