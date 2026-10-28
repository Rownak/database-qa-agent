from pathlib import Path

from data_agent.config import AgentConfig
from data_agent.runner import AgentSession, files_state
from evals.dataset import load_dataset

case = next(c for c in load_dataset(Path("evals/datasets/shipments_v1.yaml")).cases if c.id == "hybrid_delayed_express")
session = AgentSession(AgentConfig(latency_trace=True), files_state(None, None))
result = session.ask(case.question)
session.close()

print("error:", result.error, "| latency_s:", round(result.latency_s, 3))
for stage in result.timing["stages"]:
    extra = {k: v for k, v in stage.items() if k not in ("stage", "s")}
    print(f"  {stage['stage']:<10}{stage['s']:>7.3f}s  {extra or ''}")
print(f"  {'other':<10}{result.timing['other_s']:>7.3f}s")
print("query_s:", result.timing["query_s"], "| ok:", result.timing["ok"])
