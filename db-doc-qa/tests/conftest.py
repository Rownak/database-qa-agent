"""Shared fixtures: a loaded copy of the sales.csv fixture in a temporary session directory."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from data_agent.ingest import load_file

DB_SAMPLES = Path(__file__).resolve().parent.parent / "sample_data" / "database"  # table fixtures, including sales.csv
DOC_SAMPLES = Path(__file__).resolve().parent.parent / "sample_data" / "documents"  # document fixtures
SALES = DB_SAMPLES / "sales.csv"  # the v1 dataset; tool tests rely on its known values


@pytest.fixture
def loaded(tmp_path):
    """Returns the IngestResult for sales.csv loaded into a temp session dir."""
    f = SALES
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


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """Tests run in local (in-process) mode unless they set API_URL themselves."""
    monkeypatch.delenv("API_URL", raising=False)


@pytest.fixture
def fake_embed(monkeypatch):
    """Replaces ``doc_ingest.embed`` with unit vectors (no network); returns the call log."""
    import numpy as np

    from data_agent import doc_ingest

    calls = []

    def _embed(texts, task_type):
        calls.append((len(texts), task_type))
        vecs = np.zeros((len(texts), doc_ingest.EMBED_DIM), dtype=np.float32)
        vecs[:, 0] = 1.0
        return vecs

    monkeypatch.setattr(doc_ingest, "embed", _embed)
    return calls
