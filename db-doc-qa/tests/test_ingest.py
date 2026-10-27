"""Tests for data_agent.ingest (Phase 1.2; multiple tables per session since Phase 6.1)."""

import sqlite3
from contextlib import closing

import pytest

from data_agent.ingest import (
    MAX_TABLES,
    IngestError,
    LoadResult,
    ParsedTable,
    load_files,
    load_parsed,
    load_sample,
    read_table,
    table_name_for,
)
from tests.conftest import DB_SAMPLES, sample_path


def _path(name):
    return sample_path(name)


def _file(name):
    return name, _path(name).read_bytes()


def _tables(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))


def _rows(db_path, table):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


# --- single-file validation (read_table) ---


@pytest.mark.parametrize("name", ["sales.csv", "sales.xlsx", "multi_sheet.xlsx"])
def test_valid_files_load(name, tmp_path):
    r = load_files([_file(name)], tmp_path / "s")
    assert isinstance(r, LoadResult) and len(r.tables) == 1
    assert (r.tables[0].row_count, r.tables[0].column_count) == (50, 5)
    assert "order_date (date) — order date" in r.schema_summary


def test_multi_sheet_reports_ignored_sheet(tmp_path):
    r = load_files([_file("multi_sheet.xlsx")], tmp_path / "s")
    assert r.tables[0].ignored_sheets == ["summary"]


@pytest.mark.parametrize(
    "name,message",
    [
        ("notes.txt", "Unsupported file type. Upload a .csv or .xlsx file."),
        ("empty.csv", "The file has no data rows or no columns."),
        ("headerless.csv", "No header row detected."),
        ("numeric_header.csv", "Column 1 header must be text, not a number: '1001'."),
        ("duplicate_header.csv", "Duplicate column name: 'Revenue' (column 3) and 'revenue' (column 4)."),
    ],
)
def test_invalid_files_give_specific_error_and_no_db(name, message, tmp_path):
    session = tmp_path / "s"
    r = load_files([_file(name)], session)
    assert isinstance(r, IngestError) and r.message == message
    assert not session.exists()
    assert read_table(*_file(name)) == IngestError(message)  # read_table alone writes nothing


def test_corrupt_xlsx(tmp_path):
    r = load_files([_file("corrupt.xlsx")], tmp_path / "s")
    assert isinstance(r, IngestError) and r.message.startswith("Could not read the file")


def test_size_limit(tmp_path):
    r = load_files([("big.csv", b"x" * (500 * 1024 + 1))], tmp_path / "s")
    assert isinstance(r, IngestError) and "500 KB" in r.message


@pytest.mark.parametrize("data", [b"a,b,a\n1,2,3\n", b"Order ID,order_id\n1,2\n"])
def test_duplicate_headers_rejected(data, tmp_path):
    assert "Duplicate column name" in load_files([("t.csv", data)], tmp_path / "s").message


def test_column_cleaning(tmp_path):
    r = load_files([("t.csv", b"1a,b c\n1,x\n")], tmp_path / "s")
    assert r.column_map == {"t": {"c_1a": "1a", "b_c": "b c"}}


# --- table names ---


@pytest.mark.parametrize(
    "file_name,taken,expected",
    [
        ("sales.csv", set(), "sales"),
        ("Sales 2024.CSV", set(), "sales_2024"),
        ("student-info (final).xlsx", set(), "student_info_final"),
        ("2024.csv", set(), "t_2024"),
        ("2024 sales.csv", set(), "t_2024_sales"),
        ("sqlite_stuff.csv", set(), "t_sqlite_stuff"),
        ("???.csv", set(), "table"),
        ("sales.csv", {"sales"}, "sales_2"),
        ("sales.xlsx", {"sales", "sales_2"}, "sales_3"),
    ],
)
def test_table_name_for(file_name, taken, expected):
    assert table_name_for(file_name, taken) == expected


def test_same_stem_files_get_distinct_tables(tmp_path):
    r = load_files([_file("sales.csv"), _file("sales.xlsx")], tmp_path / "s")
    assert [t.table for t in r.tables] == ["sales", "sales_2"]
    assert _tables(r.db_path) == ["sales", "sales_2"]


# --- several files ---


def test_one_two_and_three_files_give_that_many_tables(tmp_path):
    files = [_file("sales.csv"), ("people.csv", b"name,age\nann,30\nbob,41\n"), ("2024.csv", b"x\n1\n")]
    for n in (1, 2, 3):
        r = load_files(files[:n], tmp_path / f"s{n}")
        assert len(r.tables) == n and len(_tables(r.db_path)) == n
    assert _tables(r.db_path) == ["people", "sales", "t_2024"]
    assert _rows(r.db_path, "people") == 2 and _rows(r.db_path, "sales") == 50
    assert r.column_map["people"] == {"name": "name", "age": "age"}
    for block in ('"sales" — 50 rows, from sales.csv', '"people" — 2 rows, from people.csv', '"t_2024" — 1 rows'):
        assert block in r.schema_summary
    assert "age (INTEGER) — age" in r.schema_summary


def test_more_than_the_limit_is_rejected_and_writes_nothing(tmp_path):
    files = [(f"f{i}.csv", b"a\n1\n") for i in range(MAX_TABLES + 1)]
    session = tmp_path / "s"
    r = load_files(files, session)
    assert isinstance(r, IngestError) and str(MAX_TABLES) in r.message
    assert not session.exists()
    assert isinstance(load_parsed([], session), IngestError)


def test_one_bad_file_fails_the_load_and_names_the_file(tmp_path):
    session = tmp_path / "s"
    r = load_files([_file("sales.csv"), _file("duplicate_header.csv")], session)
    assert isinstance(r, IngestError) and r.message.startswith("duplicate_header.csv: Duplicate column name")
    assert not session.exists()


def test_load_parsed_takes_read_table_results(tmp_path):
    parsed = read_table(*_file("sales.csv"))
    assert isinstance(parsed, ParsedTable) and parsed.file_name == "sales.csv"
    assert _tables(load_parsed([parsed], tmp_path / "s").db_path) == ["sales"]


# --- the sample and atomic replacement ---


def test_load_sample(tmp_path):
    r = load_sample(tmp_path / "s")
    assert isinstance(r, LoadResult) and r.tables[0].table == "data" and r.tables[0].row_count == 60
    assert _rows(r.db_path, "data") == 60
    assert r.schema_summary.startswith('"data" — 60 rows, from shipments.csv')


def test_invalid_upload_keeps_existing_db(tmp_path):
    session = tmp_path / "s"
    db = load_sample(session).db_path
    before = db.read_bytes()
    for name in ("notes.txt", "empty.csv", "duplicate_header.csv", "corrupt.xlsx"):
        assert isinstance(load_files([_file(name)], session), IngestError)
    assert db.read_bytes() == before
    assert [f.name for f in session.iterdir()] == ["data.db"]


def test_valid_upload_replaces_db(tmp_path):
    session = tmp_path / "s"
    load_sample(session)
    r = load_files([("small.csv", b"a,b\n1,x\n2,y\n")], session)
    assert _tables(r.db_path) == ["small"] and _rows(r.db_path, "small") == 2
    assert [f.name for f in session.iterdir()] == ["data.db"]


def test_failed_write_keeps_existing_db(tmp_path, monkeypatch):
    import pandas as pd

    session = tmp_path / "s"
    db = load_sample(session).db_path
    before = db.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_sql", boom)
    with pytest.raises(RuntimeError):
        load_files([("small.csv", b"a,b\n1,x\n")], session)
    assert db.read_bytes() == before
    assert [f.name for f in session.iterdir()] == ["data.db"]


def test_shipments_sample_schema_and_seeded_rows(tmp_path):
    """The default shipments sample (Phase 3.1) loads with clean, typed columns and the demo rows."""
    r = load_sample(tmp_path / "s")
    assert isinstance(r, LoadResult) and (r.tables[0].row_count, r.tables[0].column_count) == (60, 11)
    expected = [
        "shipment_id", "shipment_type", "status", "origin", "destination", "current_location",
        "ship_date", "expected_delivery_date", "last_update_date", "delivered_date", "delay_days",
    ]
    cmap = r.column_map["data"]
    assert list(cmap) == expected and all(k == v for k, v in cmap.items())
    for line in ("shipment_id (INTEGER)", "delay_days (INTEGER)", "ship_date (date)", "delivered_date (date)"):
        assert line in r.schema_summary

    with sqlite3.connect(r.db_path) as conn:
        rows = conn.execute(
            "SELECT shipment_id, shipment_type, status, delay_days FROM data "
            "WHERE shipment_id BETWEEN 12345 AND 12349 OR shipment_id = 99999 ORDER BY shipment_id"
        ).fetchall()
    assert rows == [
        (12345, "Express", "Delayed", 3),
        (12346, "Standard", "Delayed", 1),
        (12347, "International", "Customs Hold", 6),
        (12348, "Standard", "In Transit", 0),
        (12349, "Refrigerated", "Delayed", 2),
    ]
