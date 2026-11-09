"""Offline checks of the generated ADK eval sets under ``evals/adk/`` (Sprint 10, Phase 10.1; Sprint 11, 11.3).

The eval sets and their ``test_config.json`` files are read by ``adk eval`` and ``AgentEvaluator``.
These tests only validate the files with ADK's own models and against the agent's tool list,
so a typo is caught without a live model call. Every folder holding a ``test_config.json`` is checked, so a
new suite is covered without editing this file.
"""

from pathlib import Path

import pytest
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_metrics import ToolTrajectoryCriterion
from google.adk.evaluation.eval_set import EvalSet

from data_agent.agent import root_agent

GATING = ("tool_args_contain", "answer_contains", "answer_excludes", "section_retrieved", "citation_grounded")
ADK_DIR = Path(__file__).resolve().parent.parent / "evals" / "adk"
FOLDERS = sorted(p.parent for p in ADK_DIR.rglob("test_config.json"))  # one eval set and one config per folder


def _id(folder: Path) -> str:
    return folder.relative_to(ADK_DIR).as_posix()


def _load_set(folder: Path) -> EvalSet:
    (path,) = folder.glob("*.test.json")
    return EvalSet.model_validate_json(path.read_text(encoding="utf-8"))


def _load_config(folder: Path) -> EvalConfig:
    return EvalConfig.model_validate_json((folder / "test_config.json").read_text(encoding="utf-8"))


def _match(folder: Path) -> str:
    return _load_config(folder).criteria["tool_trajectory_avg_score"].match_type


def test_all_suites_have_a_folder():
    assert {_id(f).split("/")[0] for f in FOLDERS} == {
        "unit_routing", "unit_hybrid", "memory", "adversarial", "regression", "regression_cache"}


@pytest.mark.parametrize("folder", FOLDERS, ids=_id)
def test_eval_set_and_config_validate(folder):
    eval_set = _load_set(folder)
    assert eval_set.eval_cases
    for case in eval_set.eval_cases:
        assert case.conversation, case.eval_id
        assert case.session_input.app_name == "data_agent"
        assert isinstance(case.session_input.state, dict)


def test_eval_ids_are_unique_across_sets():
    ids = [c.eval_id for folder in FOLDERS for c in _load_set(folder).eval_cases]
    assert len(ids) == len(set(ids))


def test_golden_tools_belong_to_root_agent():
    agent_tools = {getattr(t, "__name__", None) or getattr(t, "name", None) for t in root_agent.tools}
    assert agent_tools == {"run_sql", "search_documents"}
    for folder in FOLDERS:
        for case in _load_set(folder).eval_cases:
            for inv in case.conversation:
                assert {t.name for t in inv.intermediate_data.tool_uses} <= agent_tools, case.eval_id


@pytest.mark.parametrize("folder", FOLDERS, ids=_id)
def test_config_registers_the_gating_metrics(folder):
    config = _load_config(folder)
    criteria = config.criteria
    assert set(criteria) == {"tool_trajectory_avg_score", *GATING}, "no ROUGE or other wording-based scoring"
    crit = criteria["tool_trajectory_avg_score"]
    assert crit.threshold == 1.0
    assert crit.ignore_args is True
    assert crit.match_type in ("EXACT", "IN_ORDER")
    assert ToolTrajectoryCriterion.MatchType[crit.match_type]
    for name in GATING:
        assert criteria[name] == 1.0
        assert config.custom_metrics[name].code_config.name == f"evals.adk.metrics.{name}"


def test_empty_golden_only_where_it_is_deliberate():
    """Under IN_ORDER an empty golden list matches any trajectory; the builder allows it only with ``trajectory: any``.

    Under EXACT an empty list means "no tool call" (a refusal). So the IN_ORDER folders may hold empty turns
    only in cases the golden YAML marks ``trajectory: any``, which the builder enforces; here the generated
    sets are checked against the YAML.
    """
    from evals.adk.golden import load_all

    any_ids = {c.id for s in load_all() for c in s.cases if c.trajectory == "any"}
    for folder in FOLDERS:
        if _match(folder) != "IN_ORDER":
            continue
        for case in _load_set(folder).eval_cases:
            base = case.eval_id.split("__v")[0]
            if any(not inv.intermediate_data.tool_uses for inv in case.conversation):
                assert base in any_ids, case.eval_id


def test_hybrid_cases_expect_the_documented_tools():
    for case in _load_set(ADK_DIR / "unit_hybrid").eval_cases:
        names = {t.name for inv in case.conversation for t in inv.intermediate_data.tool_uses}
        assert names <= {"run_sql", "search_documents"}, case.eval_id
