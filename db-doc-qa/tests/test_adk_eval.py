"""Pytest wrapper of the pr-tier gate (Sprint 10, Phase 10.2; since Sprint 11, Phase 11.4 a wrapper of ``evals.adk.suite``).

The live test runs ``suite.main(["--tier", "pr"])``: every pr suite, ``n`` runs per case, one verdict. It calls
the Gemini API (billed), so it is **opt-in**:
set ``RUN_ADK_EVAL=1`` (and have ``GOOGLE_API_KEY`` available). The key normally sits in ``.env``, which
``data_agent.agent`` loads at import, so "skip without a key" alone would make every ``uv run pytest``
call the API. The offline test below always runs and checks the setup the gate depends on.
"""

import os
from pathlib import Path

import pytest

import data_agent.agent as agent_module  # imported first: it loads .env, which the skip condition below reads
from data_agent.agent import build_agent
from data_agent.config import load_agent_config
from evals.adk import suite

ROOT = Path(__file__).resolve().parent.parent  # db-doc-qa/
SMOKE_CONFIG = ROOT / "configs" / "eval" / "smoke.yaml"

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
def test_pr_gate():
    """The ``pr`` tier of the suite registry passes: the same gate as ``uv run python -m evals.adk.suite --tier pr``.

    The report is written to ``runs/adk/`` (git-ignored); the exit code is the verdict (0 PASS, 1 FAIL, 2 INVALID).
    """
    assert suite.main(["--tier", "pr"]) == 0


def test_gate_wraps_the_pr_tier():
    """Offline: the wrapper runs the registry's ``pr`` suites, none of which needs a different agent config."""
    registry = suite.load_registry()
    names = [s.name for s in suite.select(registry, "pr")]
    assert names == ["unit_routing", "unit_hybrid", "adversarial", "regression"]
    assert {s.agent_config for s in suite.select(registry, "pr")} == {"configs/eval/smoke.yaml"}
