import json
import tempfile
from pathlib import Path

import yaml

from data_agent.config import load_config
from data_agent.events import TurnResult
from evals.dataset import EvalCase, Expected
from evals.metrics import METRICS
from evals.run import run_eval

root = Path(tempfile.mkdtemp())
# one case with its own tiny bundle, one without (it falls back to the sample data)
bundle = root / "tables" / "a"
bundle.mkdir(parents=True)
(bundle / "data.db").write_bytes(b"")
(bundle / "docs.db").write_bytes(b"")
(bundle / "state.json").write_text(json.dumps({"db_path": "data.db", "docs_db_path": "docs.db", "column_map": {}, "schema_summary": "tiny", "docs_summary": "tiny"}))
(root / "dataset.yaml").write_text(yaml.safe_dump({"name": "demo", "version": 1, "cases": [
    {"id": "own_bundle", "question": "q1", "state_file": "tables/a/state.json", "expected": {"reference_answers": ["Paris"]}},
    {"id": "samples", "question": "q2", "expected": {"reference_answers": ["Paris"]}},
]}))
(root / "run.yaml").write_text(yaml.safe_dump({
    "name": "demo", "dataset": {"path": str(root / "dataset.yaml")},
    "eval": {"metrics": ["exact_match", "squad_f1"], "predictions": True, "prediction_answer": "first_line"},
}))


class Canned:
    def __init__(self, cfg, state):
        print("state keys:", sorted(state))

    def ask(self, question):
        return TurnResult("The Paris.\nSee [France.md].", [], None, 0.1)

    def close(self, delete_files=True):
        pass


m = run_eval(load_config(root / "run.yaml"), root / "run", session_factory=Canned)
print("metrics:", m["metrics"])
print("prediction:", (root / "run" / "predictions.jsonl").read_text().splitlines()[0])

for answer in ("The Beatles.", "New York", "Paris\nSources: x"):
    c = EvalCase(id="c", question="q", expected=Expected(reference_answers=["beatles", "New York City", "Paris"]))
    r = TurnResult(answer, [], None, 0.0)
    print(repr(answer), METRICS["exact_match"](c, r), round(METRICS["squad_f1"](c, r), 3))
