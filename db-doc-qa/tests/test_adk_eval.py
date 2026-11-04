"""Regression gate for the agent's tool use: ADK ``AgentEvaluator`` over ``evals/adk/`` (Sprint 10, Phase 10.2).

Each eval set is run ``NUM_RUNS`` times against the real model; a case must match its golden tool
trajectory in every run. These tests call the Gemini API (billed), so they are **opt-in**:
set ``RUN_ADK_EVAL=1`` (and have ``GOOGLE_API_KEY`` available). The key normally sits in ``.env``, which
``data_agent.agent`` loads at import, so "skip without a key" alone would make every ``uv run pytest``
call the API. The offline test below always runs and checks the setup the gate depends on.
"""

import asyncio
import os
from pathlib import Path

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.local_eval_set_results_manager import LocalEvalSetResultsManager

import data_agent.agent as agent_module  # imported first: it loads .env, which the skip condition below reads
from data_agent.agent import build_agent
from data_agent.config import load_agent_config

ROOT = Path(__file__).resolve().parent.parent  # db-doc-qa/
ADK_DIR = ROOT / "evals" / "adk"
SMOKE_CONFIG = ROOT / "configs" / "eval" / "smoke.yaml"
NUM_RUNS = 3

live = pytest.mark.skipif(
    not (os.environ.get("RUN_ADK_EVAL") == "1" and os.environ.get("GOOGLE_API_KEY")),
    reason="live model calls: set RUN_ADK_EVAL=1 and GOOGLE_API_KEY to run",
)


@pytest.fixture
def smoke_agent(monkeypatch):
    """Makes ``data_agent.agent.root_agent`` the agent of ``configs/eval/smoke.yaml`` for one test.

    ``AgentEvaluator`` reads ``root_agent`` from the already imported module, which may have been built
    with the default settings by an earlier test, so setting the environment variable alone is not enough.
    """
    monkeypatch.setenv("DATA_AGENT_CONFIG", str(SMOKE_CONFIG))
    agent = build_agent(load_agent_config())
    monkeypatch.setattr(agent_module, "root_agent", agent)
    return agent


def test_smoke_fixture_applies_eval_settings(smoke_agent):
    """The gate runs the deterministic eval settings: temperature 0, response cache off."""
    assert agent_module.root_agent is smoke_agent
    assert smoke_agent.generate_content_config.temperature == 0.0
    assert smoke_agent.name == "qa_orchestrator"
    assert load_agent_config().cache.response.enabled is False


@live
@pytest.mark.parametrize("folder", ["routing", "hybrid"])
def test_tool_trajectory(folder, smoke_agent):
    """Every case of the eval set matches its golden trajectory in all ``NUM_RUNS`` runs."""
    history = LocalEvalSetResultsManager(str(ROOT))  # writes data_agent/.adk/eval_history, as `adk eval` does
    asyncio.run(
        AgentEvaluator.evaluate(
            "data_agent",
            str(ADK_DIR / folder),
            num_runs=NUM_RUNS,
            app_name="data_agent",
            eval_set_results_manager=history,
        )
    )
