"""Offline UI tests for app.py using Streamlit's AppTest (Phase 1.6). No LLM calls."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from data_agent import sessions
from data_agent.ingest import SAMPLE_FILE
from tests.conftest import DB_SAMPLES, data_files, end_session_button

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def at():
    app = AppTest.from_file(APP, default_timeout=30).run()
    yield app
    sessions.end_session(sessions.ROOT / app.session_state.session_id)


def test_first_load_shows_sample_and_creates_folder(at):
    assert not at.exception
    assert "**shipments.csv** (sample)" in data_files(at)
    assert "table `data` · 60 rows × 11 columns" in data_files(at)
    assert (sessions.ROOT / at.session_state.session_id / "data.db").is_file()


def test_end_session_deletes_folder_and_starts_new(at):
    old = at.session_state.session_id
    end_session_button(at).click().run()
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
    assert "**shipments.csv** (sample)" in data_files(at) and len(at.session_state.messages) == 1
    good = DB_SAMPLES / "multi_sheet.xlsx"
    at.file_uploader[0].upload(good.name, good.read_bytes()).run()
    assert not at.error and "**multi_sheet.xlsx**" in data_files(at) and "shipments.csv" not in data_files(at)
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
    assert "multi_sheet.xlsx" in data_files(at)
    end_session_button(at).click().run()
    assert "**shipments.csv** (sample)" in data_files(at)
    sessions.end_session(sessions.ROOT / at.session_state.session_id)


def test_end_session_and_idle_expiry_remove_docs_db_with_the_folder(at):
    folder = sessions.ROOT / at.session_state.session_id
    (folder / "docs.db").write_bytes(b"x")
    end_session_button(at).click().run()
    assert not folder.exists()
    second = sessions.ROOT / at.session_state.session_id
    (second / "docs.db").write_bytes(b"x")
    at.session_state.last_active -= sessions.MAX_AGE_SECONDS + 1
    at.run()
    assert not second.exists()
    sessions.end_session(sessions.ROOT / at.session_state.session_id)


# --- several data files (Phase 6.1) ---


def _upload(at, name, data=None):
    at.file_uploader[0].upload(name, data if data is not None else (DB_SAMPLES / name).read_bytes()).run()


def _tables(at):
    return [e["table"] for e in at.session_state.table_entries]


def _db_tables(at):
    import sqlite3
    from contextlib import closing

    db = sessions.ROOT / at.session_state.session_id / "data.db"
    with closing(sqlite3.connect(db)) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))


def test_second_and_third_files_are_added_as_tables(at):
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _upload(at, "sales.csv")
    assert _tables(at) == ["sales"] and at.session_state.messages == []
    _upload(at, "people.csv", b"name,age\nann,30\n")
    _upload(at, "multi_sheet.xlsx")
    assert not at.error and _tables(at) == ["sales", "people", "multi_sheet"]
    assert _db_tables(at) == ["multi_sheet", "people", "sales"]
    text = data_files(at)
    assert "table `people` · 1 rows × 2 columns" in text and "shipments.csv" not in text
    assert at.selectbox(key="preview_table").options == ["sales", "people", "multi_sheet"]


def test_fourth_new_file_is_rejected_and_changes_nothing(at):
    for name in ("a.csv", "b.csv", "c.csv"):
        _upload(at, name, b"x\n1\n")
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _upload(at, "d.csv", b"x\n1\n")
    assert at.error[0].value == "Up to 3 data files per session. Remove one first."
    assert _tables(at) == ["a", "b", "c"] and len(at.session_state.messages) == 1


def test_same_name_replaces_in_place_even_at_the_limit(at):
    for name, data in (("a.csv", b"x\n1\n"), ("b.csv", b"x\n1\n2\n"), ("c.csv", b"x\n1\n")):
        _upload(at, name, data)
    _upload(at, "B.CSV", b"y\n1\n2\n3\n")
    assert not at.error and _tables(at) == ["a", "b", "c"]
    assert [e["rows"] for e in at.session_state.table_entries] == [1, 3, 1]
    assert [e["name"] for e in at.session_state.table_entries] == ["a.csv", "B.CSV", "c.csv"]


def test_same_stem_files_get_suffixed_tables(at):
    _upload(at, "sales.csv")
    _upload(at, "sales.xlsx")
    assert _tables(at) == ["sales", "sales_2"] and _db_tables(at) == ["sales", "sales_2"]


def test_invalid_file_keeps_tables_and_chat(at):
    _upload(at, "sales.csv")
    at.session_state.messages = [{"role": "user", "content": "hi"}]
    _upload(at, "duplicate_header.csv")
    assert "Duplicate column name" in at.error[0].value
    assert _tables(at) == ["sales"] and len(at.session_state.messages) == 1


def test_remove_keeps_the_others_and_removing_all_restores_the_sample(at):
    _upload(at, "sales.csv")
    _upload(at, "people.csv", b"name\nann\n")
    at.button(key="remove_table_sales.csv").click().run()
    assert _tables(at) == ["people"] and _db_tables(at) == ["people"]
    at.button(key="remove_table_people.csv").click().run()
    assert "**shipments.csv** (sample)" in data_files(at) and _db_tables(at) == ["data"]
    assert not at.session_state.tables_custom


def test_data_files_do_not_touch_the_documents(at):
    docs = list(at.session_state.doc_entries)
    _upload(at, "sales.csv")
    _upload(at, "people.csv", b"name\nann\n")
    assert at.session_state.doc_entries == docs and not at.session_state.docs_custom
