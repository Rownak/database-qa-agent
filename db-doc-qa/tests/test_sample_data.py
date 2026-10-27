"""Tests for the sample_data layout and the default sample files (Phase 3.1)."""

from data_agent import ingest
from data_agent.ingest import LoadResult, load_sample
from tests.conftest import DB_SAMPLES, DOC_SAMPLES


def test_default_sample_lives_in_the_package_default_folder():
    """The files a new session loads ship inside data_agent/, so adk deploy includes them."""
    pkg = ingest.SAMPLE_DIR.parent.parent
    assert pkg.name == "data_agent" and ingest.SAMPLE_DIR.name == "default"
    assert ingest.SAMPLE_FILE == ingest.SAMPLE_DIR / "shipments.csv" and ingest.SAMPLE_FILE.is_file()


def test_default_documents_cover_pdf_and_text():
    names = sorted(p.name for p in ingest.SAMPLE_DOCS_DIR.iterdir())
    assert names == [
        "customer_communication_guidelines.pdf",
        "international_shipping_procedures.txt",
        "shipment_delay_policy.pdf",
    ]


def test_test_fixtures_are_split_into_database_and_document_folders():
    db = {p.suffix for p in DB_SAMPLES.iterdir()}
    docs = {p.suffix for p in DOC_SAMPLES.iterdir()}
    assert db <= {".csv", ".xlsx", ".txt"}  # notes.txt is the unsupported-table-type fixture
    assert docs == {".pdf", ".txt", ".md"}
    assert not [p for p in DB_SAMPLES.parent.iterdir() if p.is_file()]  # nothing loose in sample_data/


def test_markdown_fixture_has_hash_headings_and_clauses():
    text = (DOC_SAMPLES / "structured.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# Parcel Handling Guide"
    assert any(line.startswith("## ") for line in lines) and any(line.startswith("### ") for line in lines)
    assert any(line.startswith("(a) ") for line in lines)


def test_default_sample_still_loads(tmp_path):
    assert isinstance(load_sample(tmp_path / "s"), LoadResult)
