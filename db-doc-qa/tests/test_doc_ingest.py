"""Tests for data_agent.doc_ingest (embeddings mocked; no network)."""

import base64
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import numpy as np
import pytest

from data_agent import doc_ingest
from data_agent.doc_ingest import DocError, PreparedDoc, build, embed, prepare
from data_agent.ingest import SAMPLE_DOCS_DIR
from tests.conftest import DOC_SAMPLES

TOO_LARGE = "Document too large (limit 1 MB / 20 pages)."


def _prep(name: str, data: bytes | None = None, folder=DOC_SAMPLES) -> PreparedDoc:
    result = prepare(name, data if data is not None else (folder / name).read_bytes())
    assert isinstance(result, PreparedDoc), result
    return result


def _policy() -> PreparedDoc:
    return _prep("shipment_delay_policy.pdf", folder=SAMPLE_DOCS_DIR)


def _fake_vectors(texts, task_type):
    """Deterministic unit vectors, one per text."""
    rng = np.random.default_rng(0)
    v = rng.normal(size=(len(texts), doc_ingest.EMBED_DIM)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _b64(path) -> dict:
    return {"name": path.name, "b64": base64.b64encode(path.read_bytes()).decode()}


def _rows(db, sql, *args):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, args).fetchall()


# ----------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "name, data, message",
    [
        ("notes.docx", b"x", "Unsupported document type. Upload a .pdf, .txt or .md file."),
        ("big.txt", b"a" * (1024 * 1024 + 1), TOO_LARGE),
        ("pages.pdf", (DOC_SAMPLES / "oversize_pages.pdf").read_bytes(), TOO_LARGE),
        ("enc.pdf", (DOC_SAMPLES / "encrypted.pdf").read_bytes(), "Could not read the document: the PDF is encrypted."),
        ("latin1.txt", (DOC_SAMPLES / "latin1.txt").read_bytes(), "Could not read the document: the file is not valid UTF-8 text."),
        ("scanned.pdf", (DOC_SAMPLES / "scanned.pdf").read_bytes(), "No readable text found in scanned.pdf."),
        ("empty.txt", b"", "No readable text found in empty.txt."),
        ("blank.md", b"  \n\n \t\n", "No readable text found in blank.md."),
    ],
    ids=["extension", "size", "pages", "encrypted", "not-utf8", "scanned", "empty", "blank"],
)
def test_validation_messages(name, data, message):
    assert prepare(name, data) == DocError(message)


def test_corrupt_pdf_is_reported():
    err = prepare("bad.pdf", b"this is not a pdf")
    assert isinstance(err, DocError) and err.message.startswith("Could not read the document: ")


def test_extension_is_checked_before_size():
    assert prepare("big.exe", b"a" * (2 * 1024 * 1024)).message.startswith("Unsupported")


def test_uppercase_extension_and_bom_accepted():
    doc = _prep("NOTES.TXT", b"\xef\xbb\xbfTitle Here\n\n1 Rules\nSome rule text.")
    assert doc.doc_type == "txt" and doc.chunks[-1].text.startswith("1 Rules")


def test_sample_documents_are_accepted():
    for path in sorted(SAMPLE_DOCS_DIR.iterdir()):
        assert isinstance(prepare(path.name, path.read_bytes()), PreparedDoc), path.name
    assert _prep("structured.md").doc_type == "md"


# ------------------------------------------------------------------- chunking


def _by_number(doc: PreparedDoc, number: str):
    return next(c for c in doc.chunks if c.section_number == number)


def test_policy_sections_paths_numbers_and_pages():
    doc = _policy()
    assert (doc.title, doc.pages, doc.doc_type) == ("Shipment Delay Policy", 3, "pdf")
    express = _by_number(doc, "3.2")
    assert express.section_path == "3 Rules by Shipment Type > 3.2 Express Shipments"
    assert (express.level, express.parent_section) == (2, "3 Rules by Shipment Type")
    assert (express.page_start, express.page_end) == (1, 2)  # heading on p.1, clauses on p.2
    assert "Escalation threshold: 2 days" in express.text
    exc = _by_number(doc, "5.1")
    assert (exc.page_start, exc.page_end) == (2, 3)
    assert _by_number(doc, "4.3").page_start == 2
    assert doc.top_sections[:3] == [
        "1 Purpose and Scope", "2 Definitions and General Rules", "3 Rules by Shipment Type",
    ]


def test_preamble_uses_title_as_path():
    first = _policy().chunks[0]
    assert (first.section_path, first.section_number, first.level) == ("Shipment Delay Policy", "", 0)
    assert first.text.startswith("Document SDP-2026")


def test_markdown_headings_and_text_offsets():
    doc = _prep("structured.md")
    assert doc.title == "Parcel Handling Guide"
    assert _by_number(doc, "1.2").section_path == "1 Receiving > 1.2 Damage check"
    raw = (DOC_SAMPLES / "structured.md").read_text(encoding="utf-8")
    assert all(raw[c.char_start : c.char_end] == c.text for c in doc.chunks)
    assert all((c.page_start, c.page_end) == (None, None) for c in doc.chunks)


def test_chunks_stay_inside_one_section_and_end_on_sentence_boundaries():
    for name in ["shipment_delay_policy.pdf", "international_shipping_procedures.txt"]:
        doc = _prep(name, folder=SAMPLE_DOCS_DIR)
        for c in doc.chunks:
            lines = c.text.split("\n")
            assert not any(doc_ingest._numbered_heading(line.strip()) for line in lines[1:]), c.section_path
            assert c.text.rstrip()[-1] in ".;:)", c.text[-30:]


def test_oversized_section_is_split_and_keeps_its_path():
    clauses = "\n".join(
        f"({chr(97 + i)}) " + " ".join(f"word{i}x{j}" for j in range(40)) + "." for i in range(8)
    )
    doc = _prep("long.txt", f"Long Doc\n\n1 Big Section\n{clauses}\n\n2 Small\nShort text.".encode())
    big = [c for c in doc.chunks if c.section_path == "1 Big Section"]
    assert len(big) > 1
    assert all(len(c.text.split()) <= doc_ingest.CHUNK_WORDS for c in big)
    assert big[0].section_number == "1(a)-(c)" and big[1].section_number.startswith("1(d)")
    assert doc.chunks[-1].section_path == "2 Small"


def test_headings_without_numbers_use_title_like_lines():
    doc = _prep("plain.txt", b"Handbook\n\nReceiving Goods\nScan every parcel at the dock.\n\nDispatch\nLoad by route.")
    assert [c.section_path for c in doc.chunks] == ["Receiving Goods", "Dispatch"]
    assert all(c.section_number == "" and c.level == 1 for c in doc.chunks)


def test_fallback_when_no_headings():
    doc = _prep("no_headings.txt")
    assert doc.chunks and {c.chunk_method for c in doc.chunks} == {"fallback"}
    assert doc.chunks[0].section_path == "no_headings"  # file name stem, since there is no title
    assert len(doc.chunks) > 1 and doc.chunks[1].char_start < doc.chunks[0].char_end  # overlap


def test_structured_documents_use_structure_method():
    assert {c.chunk_method for c in _policy().chunks} == {"structure"}


def test_numbered_list_items_are_not_headings():
    doc = _prep("list.txt", b"Guide\n\n1 Steps\n1. Scan the parcel.\n2. Weigh the parcel.\n24 hours later it ships.")
    assert [c.section_path for c in doc.chunks] == ["1 Steps"]


# ---------------------------------------------------------------------- embed


class _FakeClient:
    calls: list[int] = []

    def __init__(self, *a, **k):
        self.models = SimpleNamespace(embed_content=self._embed)

    def _embed(self, model, contents, config):
        type(self).calls.append(len(contents))
        assert model == "gemini-embedding-001" and config.output_dimensionality == 768
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0] + [0.0] * 766 + [4.0]) for _ in contents])


def test_embed_batches_and_normalises(monkeypatch):
    from google import genai

    _FakeClient.calls = []
    monkeypatch.setattr(genai, "Client", _FakeClient)
    out = embed(["t"] * 250, "RETRIEVAL_DOCUMENT")
    assert _FakeClient.calls == [100, 100, 50]
    assert out.shape == (250, 768) and out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)
    assert np.isclose(out[0, 0], 0.6) and np.isclose(out[0, -1], 0.8)


def test_embed_returns_none_on_api_error(monkeypatch):
    from google import genai

    class Boom(_FakeClient):
        def _embed(self, model, contents, config):
            raise RuntimeError("quota")

    monkeypatch.setattr(genai, "Client", Boom)
    assert embed(["secret text"], "RETRIEVAL_QUERY") is None


# ---------------------------------------------------------------------- build


def test_build_writes_documents_chunks_and_fts(monkeypatch, tmp_path):
    monkeypatch.setattr(doc_ingest, "embed", _fake_vectors)
    docs = [_b64(p) for p in sorted(SAMPLE_DOCS_DIR.iterdir())]
    result = build(docs, tmp_path / "s")

    db = result.docs_db_path
    assert db == tmp_path / "s" / "docs.db" and result.embedded and not result.skipped
    assert not (tmp_path / "s" / "docs.db.tmp").exists()
    assert len(_rows(db, "SELECT * FROM documents")) == 3
    n_chunks = _rows(db, "SELECT count(*) FROM chunks")[0][0]
    assert n_chunks == _rows(db, "SELECT count(*) FROM chunks_fts")[0][0] > 30
    assert len(_rows(db, "SELECT embedding FROM chunks")[0][0]) == 768 * 4
    hit = _rows(
        db,
        "SELECT c.source FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.rowid WHERE chunks_fts MATCH ?",
        '"Customs Hold"',
    )
    assert ("international_shipping_procedures.txt",) in hit
    assert len(result.docs_summary.splitlines()) == 3
    assert "shipment_delay_policy.pdf — Shipment Delay Policy (3 pages" in result.docs_summary


def test_build_skips_bad_documents_and_reports_them(monkeypatch, tmp_path):
    monkeypatch.setattr(doc_ingest, "embed", _fake_vectors)
    docs = [
        _b64(DOC_SAMPLES / "structured.md"),
        _b64(DOC_SAMPLES / "scanned.pdf"),
        {"name": "x.txt", "b64": "!!not base64!!"},
    ]
    result = build(docs, tmp_path)
    assert list(result.skipped) == ["scanned.pdf", "x.txt"]
    assert result.skipped["scanned.pdf"] == "No readable text found in scanned.pdf."
    assert len(_rows(result.docs_db_path, "SELECT * FROM documents")) == 1


def test_build_without_usable_documents_writes_nothing(tmp_path):
    for docs in ([], [_b64(DOC_SAMPLES / "empty.txt")]):
        result = build(docs, tmp_path / "s")
        assert result.docs_db_path is None and result.docs_summary == "No documents loaded"
    assert not (tmp_path / "s").exists()


def test_build_writes_null_embeddings_when_embedding_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: None)
    result = build([_b64(DOC_SAMPLES / "structured.md")], tmp_path)
    db = result.docs_db_path
    assert db.exists() and not result.embedded
    assert _rows(db, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM chunks_fts")[0][0] == 6


def test_embedded_text_has_section_path_in_front(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: seen.append((texts, task_type)))
    build([_b64(DOC_SAMPLES / "structured.md")], tmp_path)
    texts, task = seen[0]
    assert task == "RETRIEVAL_DOCUMENT" and texts[1].startswith("1 Receiving > 1.1 Inbound scan\n")
    # the path is indexed too, so a clause is found through its parent heading
    count = _rows(tmp_path / "docs.db", "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", "receiving")
    assert count[0][0] >= 2


def test_stored_text_keeps_original_wording(monkeypatch, tmp_path):
    monkeypatch.setattr(doc_ingest, "embed", _fake_vectors)
    db = build([_b64(SAMPLE_DOCS_DIR / "shipment_delay_policy.pdf")], tmp_path).docs_db_path
    text = _rows(db, "SELECT text FROM chunks WHERE section_number = '3.2'")[0][0]
    assert text.startswith("3.2 Express Shipments") and "3 Rules by Shipment Type" not in text


def test_fts5_is_available():
    """Guards against Python builds compiled without FTS5."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize = 'porter unicode61')")
        conn.execute("INSERT INTO t VALUES ('delayed shipments')")
        assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'delay'").fetchone() == (1,)
