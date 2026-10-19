"""Offline UI tests for app.py using Streamlit's AppTest (Phase 1.6). No LLM calls."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from data_agent import sessions

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def at():
    app = AppTest.from_file(APP, default_timeout=30).run()
    yield app
    sessions.end_session(sessions.ROOT / app.session_state.session_id)


def test_first_load_shows_sample_and_creates_folder(at):
    assert not at.exception
    assert "sales.csv (sample)" in at.subheader[0].value
    assert at.caption[0].value == "50 rows × 5 columns"
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
