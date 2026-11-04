import asyncio
import os
from pathlib import Path

from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.local_eval_set_results_manager import LocalEvalSetResultsManager

import data_agent.agent as agent_module
from data_agent.agent import build_agent
from data_agent.config import load_agent_config

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATA_AGENT_CONFIG"] = str(ROOT / "configs" / "eval" / "smoke.yaml")
agent_module.root_agent = build_agent(load_agent_config())  # the eval settings, whatever was imported before

try:
    asyncio.run(
        AgentEvaluator.evaluate(
            "data_agent",
            str(ROOT / "evals" / "adk" / "routing"),
            num_runs=1,
            app_name="data_agent",
            eval_set_results_manager=LocalEvalSetResultsManager(str(ROOT)),
        )
    )
    print("routing: PASS (all cases matched their golden tool trajectory)")
except AssertionError as exc:
    print("routing: FAIL")
    print(str(exc)[:1500])
