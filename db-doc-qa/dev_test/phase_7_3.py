import tempfile
from pathlib import Path

from data_agent.config import load_config
from evals.dataset import load_dataset
from evals.metrics import METRICS, short_answer
from evals.run import _ask, _load_prebuilt
from data_agent.runner import AgentSession

# Live: needs GOOGLE_API_KEY (agent LLM), a running Ollama server and the dev_s50_ollama bundle.
cfg = load_config("configs/eval/hybridqa_dev50_ollama.yaml")
ds_path = Path(cfg.dataset.path)
ds = load_dataset(ds_path)
cases = [next(c for c in ds.cases if tag in c.tags) for tag in ("table", "passage")]

for case in cases:
    state, _ = _load_prebuilt(ds_path.parent / case.state_file, cfg, fallback_dir=ds_path.parent)
    result = _ask(AgentSession, cfg, state, case.question, keep_files=True)
    print("---", case.tags[1], case.id)
    print("question:  ", case.question)
    print("answer:    ", repr(result.answer))
    print("first line:", repr(short_answer(result)))
    print("gold:      ", case.expected.reference_answers)
    print("tool calls:", [(c["name"], c["args"].get("source") or c["args"].get("query", "")[:50]) for c in result.calls])
    print("EM / F1:   ", METRICS["exact_match"](case, result), round(METRICS["squad_f1"](case, result), 3))
    if result.error:
        print("error:", result.error)
