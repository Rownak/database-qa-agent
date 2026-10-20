"""Offline UI tests for app.py using Streamlit's AppTest (Phase 1.6). No LLM calls."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from data_agent import sessions
from data_agent.ingest import SAMPLE_FILE
from tests.conftest import DB_SAMPLES

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def at():
    app = AppTest.from_file(APP, default_timeout=30).run()
    yield app
    sessions.end_session(sessions.ROOT / app.session_state.session_id)


def test_first_load_shows_sample_and_creates_folder(at):
    assert not at.exception
    assert "shipments.csv (sample)" in at.subheader[0].value
    assert at.caption[0].value == "60 rows × 11 columns"
    assert (sessions.ROOT / at.session_state.session_id / "data.db").is_file()


def test_end_session_deletes_folder_and_starts_new(at):
    old = at.session_state.session_id
    at.button[0].click().run()
    assert not (sessions.ROOT / old).exists()
    assert at.session_state.session_id != old
    sessions.end_session(sessions.ROOT / at.session_state.session_id)


def test_idle_session_expires(at):
    old = at.session_state.session_id
    at.session_state.last_active -= sessions.MAX_AGE_SECONDS + 1
    at.run()
    assert at.session_state.session_id != old and not (sessions.ROOT / old).exists()


def test_upload_flow_valid_replaces_invalid_keeps(at):
    sample = SAMPLE_FILE
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    bad = DB_SAMPLES / "duplicate_header.csv"
    at.file_uploader[0].upload(bad.name, bad.read_bytes()).run()
    assert "Duplicate column name" in at.error[0].value
    assert "shipments.csv (sample)" in at.subheader[0].value and len(at.session_state.messages) == 1
    good = DB_SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(good.name, good.read_bytes()).run()
    assert not at.error and "multi_sheet.xlsx" in at.subheader[0].value
    assert at.session_state.messages == [] and "summary" in at.warning[0].value
    assert sample.exists()


def test_app_start_purges_old_folders():
    import streamlit as st

    st.cache_resource.clear()  # simulate a fresh server process
    stale = sessions.new_session_dir("leftover")
    app = AppTest.from_file(APP, default_timeout=30).run()
    try:
        assert not stale.exists()
    finally:
        sessions.end_session(sessions.ROOT / app.session_state.session_id)


def test_end_session_after_upload_returns_to_sample_not_the_old_file(at):
    """The uploader keeps its file across ss.clear(); End session must not reload it."""
    f = DB_SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(f.name, f.read_bytes()).run()
    assert "multi_sheet.xlsx" in at.subheader[0].value
    at.button[0].click().run()
    assert "shipments.csv (sample)" in at.subheader[0].value
    sessions.end_session(sessions.ROOT / at.session_state.session_id)
