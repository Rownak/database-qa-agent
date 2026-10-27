"""Tests for data_agent.tools (Phase 1.3; the table is ``sales`` since Phase 6.1)."""

import sqlite3
from types import SimpleNamespace

import pytest

from data_agent import tools


def row_count(loaded):
    with sqlite3.connect(loaded.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM sales",
        "WITH x AS (SELECT 1) DELETE FROM sales",
        "SELECT 1; DROP TABLE sales",
        "PRAGMA table_info(sales)",
        "SELECT 1 /* attach */",
    ],
)
def test_writes_rejected_and_data_unchanged(query, ctx, loaded):
    assert tools.run_sql(query, tool_context=ctx)["status"] == "error"
    assert row_count(loaded) == 50


def test_connection_is_read_only(ctx):
    with tools.closing(tools._connect_ro(ctx)) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("WITH x AS (SELECT 1) DELETE FROM sales")


def test_run_sql_ok_with_trailing_semicolon(ctx):
    r = tools.run_sql("SELECT COUNT(*) AS n FROM sales;", tool_context=ctx)
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


def test_run_sql_joins_two_uploaded_tables(tmp_path):
    """Phase 6.2: the fixture pair customers.csv / orders.csv answers a cross-table question."""
    from data_agent.ingest import load_files
    from tests.conftest import DB_SAMPLES

    r = load_files([(n, (DB_SAMPLES / n).read_bytes()) for n in ("customers.csv", "orders.csv")], tmp_path / "s")
    ctx = SimpleNamespace(state={"db_path": str(r.db_path)})
    out = tools.run_sql(
        'SELECT c."name", SUM(o."amount") FROM "orders" o JOIN "customers" c ON c."customer_id" = o."customer_id" '
        'GROUP BY c."customer_id" ORDER BY 2 DESC LIMIT 1',
        tool_context=ctx,
    )
    assert out["status"] == "success" and out["rows"] == [["Grace Hopper", 499.99]]
