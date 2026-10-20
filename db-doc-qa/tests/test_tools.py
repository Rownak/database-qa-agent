"""Tests for data_agent.tools (Phase 1.3)."""

import sqlite3
from types import SimpleNamespace

import pytest

from data_agent import tools


def row_count(loaded):
    with sqlite3.connect(loaded.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]


def test_table_info(ctx):
    r = tools.get_table_info(ctx)
    assert (r["row_count"], r["column_count"]) == (50, 5)
    assert {"name": "order_date", "original_name": "order date", "type": "TEXT"} in r["columns"]


def test_sample_rows_capped(ctx):
    assert tools.get_sample_rows(3, tool_context=ctx)["row_count"] == 3
    assert tools.get_sample_rows(99, tool_context=ctx)["row_count"] == 20


def test_column_stats(ctx):
    num = tools.get_column_stats("sales_amount", tool_context=ctx)
    assert num["count"] == 50 and {"min", "max", "mean", "median"} <= set(num)
    assert tools.get_column_stats("category", tool_context=ctx)["distinct_count"] == 5
    bad = tools.get_column_stats("nope", tool_context=ctx)
    assert bad["status"] == "error" and "order_id" in bad["error_message"]


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM data",
        "WITH x AS (SELECT 1) DELETE FROM data",
        "SELECT 1; DROP TABLE data",
        "PRAGMA table_info(data)",
        "SELECT 1 /* attach */",
    ],
)
def test_writes_rejected_and_data_unchanged(query, ctx, loaded):
    assert tools.run_sql(query, tool_context=ctx)["status"] == "error"
    assert row_count(loaded) == 50


def test_connection_is_read_only(ctx):
    with tools.closing(tools._connect_ro(ctx)) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("WITH x AS (SELECT 1) DELETE FROM data")


def test_run_sql_ok_with_trailing_semicolon(ctx):
    r = tools.run_sql("SELECT COUNT(*) AS n FROM data;", tool_context=ctx)
    assert r["status"] == "success" and r["rows"] == [[50]]


def test_truncation_and_cell_limit(ctx):
    big = tools.run_sql(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500) SELECT x FROM c",
        tool_context=ctx,
    )
    assert big["row_count"] == 100 and big["truncated"] is True
    cell = tools.run_sql("SELECT printf('%.*c', 500, 'a')", tool_context=ctx)["rows"][0][0]
    assert len(cell) == tools.MAX_CELL_CHARS + 1


def test_timeout(ctx, monkeypatch):
    monkeypatch.setattr(tools, "QUERY_TIMEOUT_S", 1)
    r = tools.run_sql(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
        tool_context=ctx,
    )
    assert r["status"] == "error" and "timed out" in r["error_message"]


def test_no_file_loaded_is_error_not_exception():
    r = tools.run_sql("SELECT 1", tool_context=SimpleNamespace(state={}))
    assert r["status"] == "error"
