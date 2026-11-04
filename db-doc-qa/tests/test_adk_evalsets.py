"""Offline checks of the ADK tool-trajectory eval sets under ``evals/adk/`` (Sprint 10, Phase 10.1).

The eval sets and their ``test_config.json`` files are read by ``adk eval`` and ``AgentEvaluator``.
These tests only validate the files with ADK's own models and against the agent's tool list,
so a typo is caught without a live model call.
"""

from pathlib import Path

import pytest
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_metrics import ToolTrajectoryCriterion
from google.adk.evaluation.eval_set import EvalSet

from data_agent.agent import root_agent

ADK_DIR = Path(__file__).resolve().parent.parent / "evals" / "adk"
SETS = {"routing": "EXACT", "hybrid": "IN_ORDER"}  # folder -> match type of its test_config.json


def _load_set(folder: str) -> EvalSet:
    return EvalSet.model_validate_json((ADK_DIR / folder / f"{folder}.test.json").read_text(encoding="utf-8"))


def _load_config(folder: str) -> EvalConfig:
    return EvalConfig.model_validate_json((ADK_DIR / folder / "test_config.json").read_text(encoding="utf-8"))


def _golden(case) -> list:
    return list(case.conversation[0].intermediate_data.tool_uses)


@pytest.mark.parametrize("folder", SETS)
def test_eval_set_and_config_validate(folder):
    eval_set = _load_set(folder)
    assert eval_set.eval_cases
    for case in eval_set.eval_cases:
        assert len(case.conversation) == 1, case.eval_id
        assert case.session_input.app_name == "data_agent"
        assert case.session_input.state == {}, "bundled demo data only"
    _load_config(folder)


def test_eval_ids_are_unique_across_sets():
    ids = [c.eval_id for folder in SETS for c in _load_set(folder).eval_cases]
    assert len(ids) == len(set(ids))


def test_golden_tools_belong_to_root_agent():
    agent_tools = {getattr(t, "__name__", None) or getattr(t, "name", None) for t in root_agent.tools}
    assert agent_tools == {"run_sql", "search_documents"}
    for folder in SETS:
        for case in _load_set(folder).eval_cases:
            assert {t.name for t in _golden(case)} <= agent_tools, case.eval_id


@pytest.mark.parametrize("folder,match_type", SETS.items())
def test_config_scores_tools_and_args_only(folder, match_type):
    config = _load_config(folder)
    criteria = config.criteria
    assert set(criteria) == {"tool_trajectory_avg_score", "tool_args_contain"}, "no text-based response scoring"
    crit = criteria["tool_trajectory_avg_score"]
    assert crit.threshold == 1.0
    assert crit.ignore_args is True
    assert ToolTrajectoryCriterion.MatchType[crit.match_type] is ToolTrajectoryCriterion.MatchType[match_type]
    assert criteria["tool_args_contain"] == 1.0
    assert config.custom_metrics["tool_args_contain"].code_config.name == "evals.adk.metrics.tool_args_contain"


def test_empty_golden_only_in_exact_set():
    """Under IN_ORDER an empty golden list matches any trajectory, so such a case must be EXACT."""
    routing = {c.eval_id: _golden(c) for c in _load_set("routing").eval_cases}
    assert routing["refuse_write"] == []
    assert all(_golden(c) for c in _load_set("hybrid").eval_cases)


def test_hybrid_cases_expect_both_tools():
    for case in _load_set("hybrid").eval_cases:
        assert {t.name for t in _golden(case)} == {"run_sql", "search_documents"}, case.eval_id
