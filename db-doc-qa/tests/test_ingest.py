"""Tests for data_agent.ingest (Phase 1.2)."""

import sqlite3

import pytest

from data_agent.ingest import IngestError, IngestResult, load_file
from tests.conftest import SAMPLES


def _path(name):
    return SAMPLES / name


@pytest.mark.parametrize("name", ["sales.csv", "sales.xlsx", "multi_sheet.xlsx"])
def test_valid_files_load(name, tmp_path):
    r = load_file(name, _path(name).read_bytes(), tmp_path / "s")
    assert isinstance(r, IngestResult)
    assert (r.row_count, r.column_count) == (50, 5)
    assert "order_date (date) — order date" in r.schema_summary


def test_multi_sheet_reports_ignored_sheet(tmp_path):
    f = SAMPLES / "multi_sheet.xlsx"
    assert load_file(f.name, f.read_bytes(), tmp_path / "s").ignored_sheets == ["summary"]


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
    r = load_file(name, (SAMPLES / name).read_bytes(), session)
    assert isinstance(r, IngestError) and r.message == message
    assert not session.exists()


def test_corrupt_xlsx(tmp_path):
    r = load_file("corrupt.xlsx", (SAMPLES / "corrupt.xlsx").read_bytes(), tmp_path / "s")
    assert isinstance(r, IngestError) and r.message.startswith("Could not read the file")


def test_size_limit(tmp_path):
    r = load_file("big.csv", b"x" * (500 * 1024 + 1), tmp_path / "s")
    assert isinstance(r, IngestError) and "500 KB" in r.message


@pytest.mark.parametrize("data", [b"a,b,a\n1,2,3\n", b"Order ID,order_id\n1,2\n"])
def test_duplicate_headers_rejected(data, tmp_path):
    assert "Duplicate column name" in load_file("t.csv", data, tmp_path / "s").message


def test_column_cleaning(tmp_path):
    r = load_file("t.csv", b"1a,b c\n1,x\n", tmp_path / "s")
    assert r.column_map == {"c_1a": "1a", "b_c": "b c"}


def _rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]


def test_load_sample(tmp_path):
    from data_agent.ingest import load_sample

    r = load_sample(tmp_path / "s")
    assert isinstance(r, IngestResult) and r.row_count == 60 and _rows(r.db_path) == 60


def test_invalid_upload_keeps_existing_db(tmp_path):
    from data_agent.ingest import load_sample

    session = tmp_path / "s"
    db = load_sample(session).db_path
    before = db.read_bytes()
    for name in ("notes.txt", "empty.csv", "duplicate_header.csv", "corrupt.xlsx"):
        assert isinstance(load_file(name, (SAMPLES / name).read_bytes(), session), IngestError)
    assert db.read_bytes() == before
    assert [f.name for f in session.iterdir()] == ["data.db"]


def test_valid_upload_replaces_db(tmp_path):
    from data_agent.ingest import load_sample

    session = tmp_path / "s"
    load_sample(session)
    r = load_file("small.csv", b"a,b\n1,x\n2,y\n", session)
    assert _rows(r.db_path) == 2 and [f.name for f in session.iterdir()] == ["data.db"]


def test_failed_write_keeps_existing_db(tmp_path, monkeypatch):
    import pandas as pd

    from data_agent.ingest import load_sample

    session = tmp_path / "s"
    db = load_sample(session).db_path
    before = db.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_sql", boom)
    with pytest.raises(RuntimeError):
        load_file("small.csv", b"a,b\n1,x\n", session)
    assert db.read_bytes() == before
    assert [f.name for f in session.iterdir()] == ["data.db"]


def test_shipments_sample_schema_and_seeded_rows(tmp_path):
    """The default shipments sample (Phase 3.1) loads with clean, typed columns and the demo rows."""
    from data_agent.ingest import load_sample

    r = load_sample(tmp_path / "s")
    assert isinstance(r, IngestResult) and (r.row_count, r.column_count) == (60, 11)
    expected = [
        "shipment_id", "shipment_type", "status", "origin", "destination", "current_location",
        "ship_date", "expected_delivery_date", "last_update_date", "delivered_date", "delay_days",
    ]
    assert list(r.column_map) == expected and all(k == v for k, v in r.column_map.items())
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
