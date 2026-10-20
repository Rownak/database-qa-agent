"""Tests for data_agent.sessions (Phase 1.6)."""

import os
import time

from data_agent import sessions


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
