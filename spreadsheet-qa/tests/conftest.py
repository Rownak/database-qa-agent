"""Shared fixtures: a loaded copy of sales.csv in a temporary session directory."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from data_agent.ingest import SAMPLE_FILE, load_file

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"  # every sample except sales.csv


@pytest.fixture
def loaded(tmp_path):
    """Returns the IngestResult for sales.csv loaded into a temp session dir."""
    f = SAMPLE_FILE
    return load_file(f.name, f.read_bytes(), tmp_path / "session")


@pytest.fixture
def ctx(loaded):
    """A fake ADK tool_context whose state holds the session's database info."""
    return SimpleNamespace(
        state={
            "db_path": str(loaded.db_path),
            "column_map": loaded.column_map,
            "schema_summary": loaded.schema_summary,
        }
    )
