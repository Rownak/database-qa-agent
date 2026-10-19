"""Offline tests for data_agent.agent (Phase 1.4). The live Gemini call is in dev_test/phase_1_4.py."""

from data_agent.agent import AGENT_MODEL, INSTRUCTION, root_agent


def test_agent_definition():
    assert root_agent.name == "data_analyst"
    assert root_agent.model == AGENT_MODEL == "gemini-3.5-flash-lite"
    assert [t.__name__ for t in root_agent.tools] == [
        "get_table_info", "get_sample_rows", "get_column_stats", "run_sql",
    ]


def test_instruction_has_schema_placeholder_and_no_other_braces():
    assert "{schema_summary}" in INSTRUCTION
    assert "{" not in INSTRUCTION.replace("{schema_summary}", "")


def test_instruction_covers_required_rules():
    for text in ("double quotes", "Never estimate", "once more", "Source:", "read-only"):
        assert text in INSTRUCTION


def test_seed_state_fills_empty_state_only():
    from types import SimpleNamespace

    from data_agent.agent import seed_state_if_missing

    empty = SimpleNamespace(state={})
    assert seed_state_if_missing(empty) is None
    assert empty.state["schema_summary"].startswith("order_id")
    assert empty.state["db_path"].endswith("data.db")

    preset = {"db_path": "x.db", "column_map": {}, "schema_summary": "keep me"}
    seed_state_if_missing(SimpleNamespace(state=preset))
    assert preset == {"db_path": "x.db", "column_map": {}, "schema_summary": "keep me"}
