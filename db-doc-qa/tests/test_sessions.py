"""Tests for data_agent.sessions (Phase 1.6)."""

import base64
import os
import time

from data_agent import doc_ingest, sessions
from tests.conftest import DOC_SAMPLES


def test_new_session_dir_and_end_session(tmp_path):
    d = sessions.new_session_dir("abc", tmp_path)
    assert d == tmp_path / "abc" and d.is_dir()
    (d / "data.db").write_bytes(b"x")
    sessions.end_session(d)
    assert not d.exists()
    sessions.end_session(d)  # already gone: no error


def test_purge_all(tmp_path):
    root = tmp_path / "adk_sheet_qa"
    sessions.new_session_dir("a", root)
    sessions.new_session_dir("b", root)
    sessions.purge_all(root)
    assert not root.exists()
    sessions.purge_all(root)  # missing root: no error


def test_sweep_stale_removes_only_old_folders(tmp_path):
    old = sessions.new_session_dir("old", tmp_path)
    fresh = sessions.new_session_dir("fresh", tmp_path)
    past = time.time() - 31 * 60
    os.utime(old, (past, past))
    assert sessions.sweep_stale(root=tmp_path) == [old]
    assert not old.exists() and fresh.exists()


def test_touch_keeps_session_from_being_swept(tmp_path):
    d = sessions.new_session_dir("s", tmp_path)
    past = time.time() - 31 * 60
    os.utime(d, (past, past))
    sessions.touch(d)
    assert sessions.sweep_stale(root=tmp_path) == [] and d.exists()


def test_sweep_stale_missing_root(tmp_path):
    assert sessions.sweep_stale(root=tmp_path / "nope") == []


# --- Phase 3.6: docs.db goes with the session folder (no new cleanup code) ---


def _session_with_docs_db(root, monkeypatch):
    folder = sessions.new_session_dir("s", root)
    (folder / "data.db").write_bytes(b"x")
    data = base64.b64encode((DOC_SAMPLES / "structured.md").read_bytes()).decode()
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: None)  # BM25-only build: no network
    assert doc_ingest.build([{"name": "structured.md", "b64": data}], folder).docs_db_path == folder / "docs.db"
    return folder


def test_end_session_removes_docs_db(tmp_path, monkeypatch):
    folder = _session_with_docs_db(tmp_path, monkeypatch)
    sessions.end_session(folder)
    assert not folder.exists()


def test_purge_all_removes_docs_db(tmp_path, monkeypatch):
    root = tmp_path / "adk_sheet_qa"
    folder = _session_with_docs_db(root, monkeypatch)
    sessions.purge_all(root)
    assert not folder.exists() and not (folder / "docs.db").exists()


def test_sweep_stale_removes_docs_db_of_an_idle_session(tmp_path, monkeypatch):
    folder = _session_with_docs_db(tmp_path, monkeypatch)
    past = time.time() - 31 * 60
    os.utime(folder, (past, past))
    assert sessions.sweep_stale(root=tmp_path) == [folder]
    assert not (folder / "docs.db").exists()
