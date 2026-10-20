"""Offline tests for data_agent.agent (Phase 1.4). The live Gemini call is in dev_test/phase_1_4.py."""

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_agent import sessions
from data_agent.agent import AGENT_MODEL, INSTRUCTION, ensure_data, root_agent
from tests.conftest import DB_SAMPLES


def test_agent_definition():
    assert root_agent.name == "data_analyst"
    assert root_agent.model == AGENT_MODEL == "gemini-3.5-flash-lite"
    assert [t.__name__ for t in root_agent.tools] == [
        "get_table_info", "get_sample_rows", "get_column_stats", "run_sql",
    ]


def test_instruction_has_schema_placeholder_and_no_other_braces():
    assert "{schema_summary}" in INSTRUCTION
    assert "{" not in INSTRUCTION.replace("{schema_summary}", "").replace("{load_error?}", "")


def test_instruction_covers_required_rules():
    for text in ("double quotes", "Never estimate", "once more", "Source:", "read-only"):
        assert text in INSTRUCTION


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Redirects the session folders to a temp dir."""
    monkeypatch.setattr(sessions, "ROOT", tmp_path)
    return tmp_path


def _ctx(session_id="s1", **state):
    return SimpleNamespace(state=dict(state), session=SimpleNamespace(id=session_id))


def _upload(path):
    return {"upload_name": path.name, "upload_b64": base64.b64encode(path.read_bytes()).decode()}


def test_ensure_data_builds_sample_when_no_upload(root):
    ctx = _ctx()
    assert ensure_data(ctx) is None
    assert ctx.state["schema_summary"].startswith("shipment_id")
    assert ctx.state["db_path"] == str(root / "s1" / "data.db")
    assert Path(ctx.state["db_path"]).is_file() and "load_error" not in ctx.state


def test_ensure_data_builds_valid_upload(root):
    ctx = _ctx(**_upload(DB_SAMPLES / "multi_sheet.xlsx"))
    ensure_data(ctx)
    assert Path(ctx.state["db_path"]).is_file() and "load_error" not in ctx.state


def test_ensure_data_invalid_upload_falls_back_to_sample(root):
    ctx = _ctx(**_upload(DB_SAMPLES / "duplicate_header.csv"))
    ensure_data(ctx)
    assert "Duplicate column name" in ctx.state["load_error"]
    assert ctx.state["schema_summary"].startswith("shipment_id")  # the sample


def test_ensure_data_corrupt_base64_falls_back_to_sample(root):
    ctx = _ctx(upload_name="x.csv", upload_b64="***not base64***")
    ensure_data(ctx)
    assert "corrupted" in ctx.state["load_error"]
    assert Path(ctx.state["db_path"]).is_file()


def test_ensure_data_rebuilds_upload_after_db_deleted(root):
    ctx = _ctx(**_upload(DB_SAMPLES / "multi_sheet.xlsx"))
    ensure_data(ctx)
    db = Path(ctx.state["db_path"])
    db.unlink()  # what a new Cloud Run instance looks like
    ensure_data(ctx)
    assert db.is_file()


def test_ensure_data_noop_when_db_present(root):
    ctx = _ctx()
    ensure_data(ctx)
    db = Path(ctx.state["db_path"])
    before = (db.stat().st_mtime_ns, dict(ctx.state))
    ensure_data(ctx)
    assert (db.stat().st_mtime_ns, ctx.state) == before


def test_ensure_data_keeps_preset_state_from_the_streamlit_app(root, loaded):
    state = {
        "db_path": str(loaded.db_path),
        "column_map": loaded.column_map,
        "schema_summary": "keep me",
    }
    ctx = SimpleNamespace(state=dict(state), session=SimpleNamespace(id="other"))
    ensure_data(ctx)
    assert ctx.state == state and not (root / "other").exists()


def test_ensure_data_sweeps_stale_folders(root):
    old = root / "old"
    old.mkdir()
    os.utime(old, (0, 0))
    ensure_data(_ctx())
    assert not old.exists() and (root / "s1").is_dir()
