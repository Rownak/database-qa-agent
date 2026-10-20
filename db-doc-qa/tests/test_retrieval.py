"""Tests for data_agent.retrieval and the search_documents tool (fixed vectors, no network)."""

import base64
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import numpy as np
import pytest

from data_agent import doc_ingest, retrieval, tools
from data_agent.ingest import SAMPLE_DOCS_DIR
from tests.conftest import DOC_SAMPLES

DIM = doc_ingest.EMBED_DIM


def unit(i: int) -> np.ndarray:
    """Basis vector ``i``: orthogonal to every other basis vector."""
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _b64(path) -> dict:
    return {"name": path.name, "b64": base64.b64encode(path.read_bytes()).decode()}


def make_db(tmp_path, paths=None, vectors=True):
    """Builds a docs.db whose chunk n (0-based, in chunk_id order) has vector ``unit(n)``."""
    paths = paths or sorted(SAMPLE_DOCS_DIR.iterdir())
    original = doc_ingest.embed
    doc_ingest.embed = (
        (lambda texts, task_type: np.vstack([unit(i) for i in range(len(texts))])) if vectors
        else (lambda texts, task_type: None)
    )
    try:
        return doc_ingest.build([_b64(p) for p in paths], tmp_path).docs_db_path
    finally:
        doc_ingest.embed = original


def chunk_id(db, **where) -> int:
    cond = " AND ".join(f"{k} = ?" for k in where)
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(f"SELECT chunk_id FROM chunks WHERE {cond}", list(where.values())).fetchone()[0]


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path / "s")


# ------------------------------------------------------------------ retrieval


def test_bm25_finds_exact_term(db):
    res = retrieval.search(db, "Customs Hold", top_k=3)
    assert res.hits[0].source == "international_shipping_procedures.txt"
    assert res.hits[0].matched_by == "bm25" and not res.dense_used


def test_dense_finds_a_paraphrase_without_shared_words(db):
    target = chunk_id(db, source="shipment_delay_policy.pdf", section_number="3.2")
    res = retrieval.search(db, "qwertyuiop", query_vec=unit(target - 1))
    assert res.dense_used
    assert res.hits[0].chunk_id == target and res.hits[0].matched_by == "dense"
    assert res.hits[0].similarity == pytest.approx(1.0)


def test_dense_only_hits_below_min_similarity_are_dropped(db):
    res = retrieval.search(db, "zzzzqqq", query_vec=unit(0))  # no keyword hit
    assert [h.matched_by for h in res.hits] == ["dense"]  # only the chunk with similarity 1.0
    mixed = (unit(0) + unit(1) * 0.3) / np.linalg.norm(unit(0) + unit(1) * 0.3)  # sim to chunk 1 is ~0.29
    assert len(retrieval.search(db, "zzzzqqq", query_vec=mixed, top_k=8).hits) == 1


def test_rrf_ranks_chunks_found_by_both_methods_first(db):
    bm25_top = retrieval.search(db, "Customs Hold").hits[0].chunk_id
    other = chunk_id(db, source="shipment_delay_policy.pdf", section_number="3.2")
    # dense agrees with BM25 on one chunk and prefers another chunk for the rest
    vec = unit(bm25_top - 1) + 0.9 * unit(other - 1)
    res = retrieval.search(db, "Customs Hold", query_vec=vec, top_k=8)
    assert res.hits[0].chunk_id == bm25_top and res.hits[0].matched_by == "both"
    assert {h.matched_by for h in res.hits[1:]} <= {"bm25", "dense"}
    assert res.hits[0].score > res.hits[1].score


def test_source_filter_is_exact(db):
    res = retrieval.search(db, "escalation threshold days", source="shipment_delay_policy.pdf", top_k=8)
    assert res.hits and {h.source for h in res.hits} == {"shipment_delay_policy.pdf"}
    assert retrieval.search(db, "escalation", source="shipment_delay").candidates == 0


def test_section_filter_is_case_insensitive_and_includes_subsections(db):
    res = retrieval.search(db, "threshold days", section="EXPRESS", top_k=8)
    assert res.hits and all("express" in h.section_path.lower() for h in res.hits)
    parent = retrieval.search(db, "escalation", source="shipment_delay_policy.pdf", section="4 Escalation Procedure", top_k=8)
    assert len({h.section_number for h in parent.hits}) > 1  # 4.1, 4.2, ... under the heading


def test_section_filter_treats_wildcards_literally(db):
    assert retrieval.search(db, "policy", section="%").candidates == 0
    assert retrieval.search(db, "policy", section="_").candidates == 0


def test_filter_excluding_everything_reports_zero_candidates(db):
    res = retrieval.search(db, "policy", section="refrigerated")
    assert res.candidates == 0 and not res.hits and "shipment_delay_policy.pdf" in res.sources


def test_missing_vectors_fall_back_to_bm25(tmp_path):
    db = make_db(tmp_path / "s", vectors=False)
    res = retrieval.search(db, "Customs Hold", query_vec=unit(0))
    assert res.hits and not res.dense_used


@pytest.mark.parametrize(
    "query",
    ['Customs Hold?', '"unbalanced', "AND OR NOT NEAR", "a* ^ b: (c) -d", "7-day threshold!", "'; DROP TABLE chunks; --", "", "???"],
)
def test_punctuation_in_query_does_not_break_fts5(db, query):
    retrieval.search(db, query)  # must not raise
    assert retrieval.search(db, "Customs Hold?").hits


def test_database_is_opened_read_only(db):
    with closing(retrieval._connect_ro(db)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM chunks")


# ----------------------------------------------------------------------- tool


def ctx_for(db):
    return SimpleNamespace(state={"docs_db_path": str(db)})


@pytest.fixture
def no_embed(monkeypatch):
    """Query embedding fails: the tool must fall back to BM25."""
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: None)


def test_tool_returns_citable_passages(db, monkeypatch):
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: unit(0)[None, :])
    r = tools.search_documents("Express escalation threshold", section="express", tool_context=ctx_for(db))
    assert r["status"] == "success" and r["passages"]
    p = r["passages"][0]
    assert set(p) == {"chunk_id", "source", "section_path", "section_number", "page_start", "page_end",
                      "text", "score", "matched_by"}
    assert p["section_number"] == "3.2" and (p["page_start"], p["page_end"]) == (1, 2)
    assert "untrusted" in r["notice"] and "note" not in r


def test_tool_caps_top_k_and_truncates_text(db, no_embed):
    assert len(tools.search_documents("shipment", top_k=50, tool_context=ctx_for(db))["passages"]) == 8
    assert len(tools.search_documents("shipment", top_k=0, tool_context=ctx_for(db))["passages"]) == 1


def test_tool_truncates_long_passages(tmp_path, no_embed):
    path = tmp_path / "long.txt"
    path.write_text("Guide\n\n1 Rules\n" + ("word " * 400).strip() + ".", encoding="utf-8")
    db = make_db(tmp_path / "s", [path], vectors=False)
    # semchunk splits at 160 words, so force a long chunk straight into the database
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE chunks SET text = ?", ("x" * 5000,))
        conn.commit()
    r = tools.search_documents("rules", tool_context=ctx_for(db))
    assert all(len(p["text"]) <= 1201 for p in r["passages"]) and r["passages"][0]["text"].endswith("…")


def test_tool_notes_bm25_only(db, no_embed):
    r = tools.search_documents("Customs Hold", tool_context=ctx_for(db))
    assert r["passages"] and "keyword (BM25)" in r["note"]


def test_tool_notes_no_match(db, no_embed):
    r = tools.search_documents("zzzzqqq", tool_context=ctx_for(db))
    assert r["status"] == "success" and r["passages"] == [] and "No passages matched" in r["note"]


def test_tool_notes_filter_excluded_everything(db, no_embed):
    r = tools.search_documents("policy", section="refrigerated", tool_context=ctx_for(db))
    assert r["passages"] == [] and "filter excluded every passage" in r["note"]
    assert "shipment_delay_policy.pdf" in r["note"]


def test_tool_errors_without_documents_and_never_raises(tmp_path, no_embed):
    for state in ({}, {"docs_db_path": str(tmp_path / "missing.db")}):
        r = tools.search_documents("x", tool_context=SimpleNamespace(state=state))
        assert r["status"] == "error" and "No documents are loaded" in r["error_message"]
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    r = tools.search_documents("x", tool_context=ctx_for(bad))
    assert r["status"] == "error"
    assert tools.search_documents("  ", tool_context=ctx_for(bad))["status"] == "error"


def test_results_never_include_another_sessions_documents(tmp_path, no_embed):
    a = make_db(tmp_path / "a", [SAMPLE_DOCS_DIR / "shipment_delay_policy.pdf"], vectors=False)
    b = make_db(tmp_path / "b", [DOC_SAMPLES / "injection.txt"], vectors=False)
    for query in ["Carrier pickups holidays", "Express escalation"]:
        assert {p["source"] for p in tools.search_documents(query, tool_context=ctx_for(a))["passages"]} <= {
            "shipment_delay_policy.pdf"
        }
        assert {p["source"] for p in tools.search_documents(query, tool_context=ctx_for(b))["passages"]} <= {
            "injection.txt"
        }
    assert tools.search_documents("source filter", source="injection.txt", tool_context=ctx_for(a))["passages"] == []


def test_adk_can_declare_the_tool():
    """ADK builds the function declaration from the signature; optional args must parse."""
    from google.adk.tools import FunctionTool

    decl = FunctionTool(tools.search_documents)._get_declaration()
    schema = decl.parameters_json_schema
    assert set(schema["properties"]) == {"query", "source", "section", "top_k"}
    assert schema["required"] == ["query"]
