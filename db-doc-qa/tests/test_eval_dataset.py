"""Tests for evals.dataset: the shipped dataset and its validation."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from evals.dataset import dataset_files, load_dataset

SHIPPED = "evals/datasets/shipments_v1.yaml"


def test_shipped_dataset_loads():
    ds = load_dataset(SHIPPED)
    assert ds.name == "shipments_v1" and ds.version == 1
    ids = [c.id for c in ds.cases]
    assert len(ids) == len(set(ids)) and 5 <= len(ids) <= 6
    assert {"db_location", "doc_express_threshold", "hybrid_delayed_express", "conflict_international", "refuse_write"} <= set(ids)
    assert dataset_files(ds) == (None, None)  # bundled samples


def test_expected_fields_are_optional_and_empty_tools_is_kept():
    ds = load_dataset(SHIPPED)
    by_id = {c.id: c for c in ds.cases}
    assert by_id["refuse_write"].expected.tools == []  # "call no tool" differs from "not checked"
    assert by_id["db_location"].expected.sections == []


def _write(tmp_path, data):
    path = tmp_path / "d.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_duplicate_case_id_is_rejected(tmp_path):
    case = {"id": "a", "question": "q"}
    with pytest.raises(ValidationError, match="duplicate case id: a"):
        load_dataset(_write(tmp_path, {"name": "n", "version": 1, "cases": [case, case]}))


def test_unknown_key_and_unknown_tool_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_dataset(_write(tmp_path, {"name": "n", "version": 1, "cases": [{"id": "a", "question": "q", "extra": 1}]}))
    bad = {"id": "a", "question": "q", "expected": {"tools": ["drop_table"]}}
    with pytest.raises(ValidationError):
        load_dataset(_write(tmp_path, {"name": "n", "version": 1, "cases": [bad]}))


def test_docs_folder_expands_to_sorted_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.txt").write_text("b")
    (tmp_path / "docs" / "a.txt").write_text("a")
    ds = load_dataset(_write(tmp_path, {"name": "n", "version": 1, "docs": str(tmp_path / "docs"), "cases": []}))
    table, docs = dataset_files(ds)
    assert table is None and [p.name for p in docs] == ["a.txt", "b.txt"]


def test_case_level_state_file_loads_and_defaults_to_none(tmp_path):
    data = {
        "name": "n", "version": 1,
        "cases": [{"id": "a", "question": "q", "state_file": "tables/a/state.json"}, {"id": "b", "question": "q"}],
    }
    ds = load_dataset(_write(tmp_path, data))
    assert ds.cases[0].state_file == Path("tables/a/state.json") and ds.cases[1].state_file is None
    assert ds.state_file is None
    assert all(c.state_file is None for c in load_dataset(SHIPPED).cases)  # existing datasets are unchanged
