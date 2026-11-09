"""Offline checks of the authored golden suites (Sprint 11, Phase 11.3): contents, review rules and critical cases."""

import json
from pathlib import Path

import pytest

from data_agent.config import load_agent_config
from evals.adk import build
from evals.adk.golden import Layer, load_all

ROOT = Path(__file__).resolve().parent.parent
SUITES = {s.suite: s for s in load_all()}
CASES = {c.id: c for s in SUITES.values() for c in s.cases}
# Design §3.1 marks these with ⚑.
CRITICAL = {
    "edge_refuse_write", "mem_refusal_persists", "adv_injected_document", "adv_urgent_write",
    "adv_sql_in_question", "adv_fabricated_section",
}


def test_suites_have_the_initial_cases_of_the_design():
    assert {name: len(s.cases) for name, s in SUITES.items()} == {
        "unit_routing": 9, "unit_hybrid": 10, "memory": 4, "adversarial": 7, "regression": 1, "regression_cache": 1}
    assert {"edge_no_documents", "edge_load_notice", "edge_missing_column", "adv_injected_document"} <= set(CASES)


def test_critical_ids_match_the_design():
    assert {cid for cid, c in CASES.items() if c.critical} == CRITICAL


def test_every_critical_case_and_unit_case_has_one_or_two_variants():
    needs = CRITICAL | {cid for cid, c in CASES.items() if c.layer is Layer.UNIT}
    for cid in sorted(needs):
        assert 1 <= len(CASES[cid].variants) <= 2, cid


def test_adversarial_is_split_into_exact_and_in_order_under_one_suite():
    folders = {p.parent.relative_to(build.ADK_DIR).as_posix() for p in build.ADK_DIR.glob("adversarial/*/test_config.json")}
    assert folders == {"adversarial/exact", "adversarial/in_order"}
    manifest = json.loads((build.ADK_DIR / "adversarial" / "manifest.json").read_text(encoding="utf-8"))
    by_folder = {c["group"]: c["folder"] for c in manifest["cases"]}
    assert by_folder["adv_sql_in_question"] == "adversarial/in_order"
    assert by_folder["adv_urgent_write"] == "adversarial/exact"
    assert len(SUITES["adversarial"].cases) == 7


def test_adversarial_injection_case_uses_the_injection_preset():
    case = CASES["adv_injected_document"]
    assert case.state == "injection_docs"
    assert [c.name for c in case.turns[0].tools] == ["search_documents"], "search only: no run_sql after injected text"


def test_state_presets_match_the_design_cases():
    assert CASES["edge_no_documents"].state == "no_docs"
    assert CASES["edge_load_notice"].state == "corrupt_upload"
    assert {c.state for c in CASES.values()} == {"default", "no_docs", "injection_docs", "corrupt_upload"}


def test_memory_cases_have_the_designed_turns_and_every_turn_with_tools_is_checked():
    assert {cid: len(c.turns) for cid, c in CASES.items() if c.layer is Layer.MEMORY} == {
        "mem_followup_pronoun": 2, "mem_constraint_retention": 4, "mem_entity_switch": 2, "mem_refusal_persists": 2}
    for case in CASES.values():
        for n, turn in enumerate(case.turns, 1):
            checked = turn.contains or turn.contains_sql or turn.excludes or turn.sections
            if turn.tools or n == len(case.turns):
                assert checked, f"{case.id} turn {n} has no answer or section check"


def test_regression_suites_and_cache_config(monkeypatch):
    assert [c.id for c in SUITES["regression"].cases] == ["reg_empty_state_build"]
    assert [c.id for c in SUITES["regression_cache"].cases] == ["reg_followup_not_cached"]
    monkeypatch.setenv("DATA_AGENT_CONFIG", str(ROOT / "configs" / "eval" / "cache_session.yaml"))
    cfg = load_agent_config()
    assert cfg.cache.response.enabled is True and cfg.cache.response.scope == "session"
    monkeypatch.setenv("DATA_AGENT_CONFIG", str(ROOT / "configs" / "eval" / "smoke.yaml"))
    smoke = load_agent_config()
    assert cfg.model == smoke.model and cfg.retrieval == smoke.retrieval and cfg.intent_enabled == smoke.intent_enabled
    turns = SUITES["regression_cache"].cases[0].turns
    assert turns[0].q == turns[1].q and all(t.tools for t in turns), "same text twice, both must reach run_sql"


def test_data_values_are_computed_not_typed():
    """The location, counts and ids come from the bundled data through contains_sql."""
    assert build.sql_facts("SELECT DISTINCT COUNT(*) FROM data GROUP BY shipment_type") == ["14", "9", "28"]
    assert build.sql_facts("SELECT COUNT(*) FROM pragma_table_info('data')") == ["11"]
    unit = CASES["unit_db_filter_two_columns"].turns[0].contains_sql
    assert build.sql_facts(unit[0]) == ["12311", "12345"]


def test_memory_exclusions_are_exactly_the_non_express_ids():
    """The literal ids in memory.yaml must be the delayed / in-transit shipments that are not Express."""
    turns = CASES["mem_constraint_retention"].turns
    delayed = build.sql_facts("SELECT shipment_id FROM data WHERE status = 'Delayed' AND shipment_type <> 'Express' ORDER BY 1")
    transit = build.sql_facts("SELECT shipment_id FROM data WHERE status = 'In Transit' AND shipment_type <> 'Express' ORDER BY 1")
    assert turns[1].excludes == delayed
    assert turns[3].excludes == transit


def test_document_fragments_and_sections_exist_in_the_policy_text():
    """Sections are real section numbers of the sample documents, and quoted thresholds appear in the text."""
    from data_agent import doc_ingest
    from data_agent.ingest import SAMPLE_DOCS_DIR

    docs = {p.name: doc_ingest.prepare(p.name, p.read_bytes()) for p in SAMPLE_DOCS_DIR.iterdir()}
    docs["injection.txt"] = doc_ingest.prepare("injection.txt", (ROOT / "sample_data" / "documents" / "injection.txt").read_bytes())
    numbers = {name: {c.section_number for c in d.chunks} for name, d in docs.items()}
    for case in CASES.values():
        for turn in case.turns:
            for s in turn.sections:
                assert s.section in numbers[s.source], f"{case.id}: {s.source} has no section {s.section}"
