"""Hybrid retrieval over a session's ``docs.db``: BM25 + dense search fused with RRF.

This is the search half of the document pipeline (design section 5). :mod:`data_agent.doc_ingest`
writes ``docs.db``; :func:`search` only reads it, through a ``mode=ro`` connection, and is
called by the ``search_documents`` tool in :mod:`data_agent.tools`. The pipeline is:

1. filter chunks by ``source`` (exact) and ``section`` (substring of ``section_path``);
2. BM25 over the FTS5 index (top 20);
3. dense cosine search over the stored vectors with numpy (top 20);
4. Reciprocal Rank Fusion (k = 60) after dropping dense hits that are not similar enough.

Vectors are read from the session's own file on every call and never cached, so one session
can never see another session's passages.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import numpy as np

TOP_N = 20  # candidates taken from each ranker before fusion
RRF_K = 60
# Dense hits below this cosine similarity are dropped. Tuned in dev_test/phase_3_3.py (see sprint_3_3.md).
MIN_DENSE_SIM = 0.62

_WORD_RE = re.compile(r"\w+")


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk.

    Attributes:
        chunk_id: Primary key of the chunk in ``docs.db``.
        source: File name of the document.
        section_path: Full heading path of the chunk.
        section_number: Section number such as ``3.2``; may be empty.
        page_start: First page (PDF only, else ``None``).
        page_end: Last page (PDF only, else ``None``).
        text: Stored chunk text, untrimmed.
        score: Fused RRF score; higher is better.
        matched_by: ``bm25``, ``dense`` or ``both``.
        similarity: Cosine similarity to the query, or ``None`` if dense search found no match.
    """

    chunk_id: int
    source: str
    section_path: str
    section_number: str
    page_start: int | None
    page_end: int | None
    text: str
    score: float
    matched_by: str
    similarity: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """Outcome of :func:`search`.

    Attributes:
        hits: The top ``top_k`` chunks in fused order.
        candidates: Number of chunks left after applying the filters.
        dense_used: Whether dense search ran (vectors and a query vector were both available).
        sources: Distinct document names in the file, for filter hints.
    """

    hits: list[Hit] = field(default_factory=list)
    candidates: int = 0
    dense_used: bool = False
    sources: list[str] = field(default_factory=list)


def search(
    docs_db_path: str | Path,
    query: str,
    source: str | None = None,
    section: str | None = None,
    top_k: int = 5,
    query_vec: np.ndarray | None = None,
) -> SearchResult:
    """Searches one session's documents with BM25 and dense retrieval, fused by RRF.

    Args:
        docs_db_path: Path of the session's ``docs.db``.
        query: Natural-language query. Its words are quoted before they reach FTS5, so
            punctuation and FTS operators in the text cannot break the syntax.
        source: Exact document file name to restrict to, or ``None``.
        section: Case-insensitive substring of ``section_path`` to restrict to, or ``None``.
            It also matches every subsection under a matching heading.
        top_k: Number of chunks to return.
        query_vec: Embedding of the query. If ``None``, or if the chunks have no stored
            vectors, only BM25 is used.

    Returns:
        A :class:`SearchResult`.

    Raises:
        sqlite3.Error: If the file cannot be opened or queried.
    """
    where, params = _filters(source, section)
    with closing(_connect_ro(docs_db_path)) as conn:
        sources = [r[0] for r in conn.execute("SELECT source FROM documents ORDER BY doc_id")]
        candidates = conn.execute(f"SELECT count(*) FROM chunks c WHERE {where}", params).fetchone()[0]
        if not candidates:
            return SearchResult([], 0, False, sources)

        bm25_ids = _bm25(conn, query, where, params)
        dense, dense_used = _dense(conn, query_vec, where, params)
        # Weak dense matches are dropped before fusion, so they neither add a result nor
        # count as agreement with BM25. Dense results are sorted, so only the tail goes.
        dense = [(cid, sim) for cid, sim in dense if sim >= MIN_DENSE_SIM]
        dense_sim = dict(dense)
        dense_ids = [cid for cid, _ in dense]

        scores: dict[int, float] = {}
        for ids in (bm25_ids, dense_ids):
            for rank, cid in enumerate(ids, start=1):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        bm25_set = set(bm25_ids)
        top = sorted(scores, key=lambda cid: (-scores[cid], cid))[: max(1, top_k)]
        rows = _fetch(conn, top)

    hits = []
    for cid in top:
        src, path, num, p0, p1, text = rows[cid]
        in_bm25, in_dense = cid in bm25_set, cid in dense_sim
        matched = "both" if in_bm25 and in_dense else "bm25" if in_bm25 else "dense"
        hits.append(Hit(cid, src, path, num, p0, p1, text, round(scores[cid], 5), matched, dense_sim.get(cid)))
    return SearchResult(hits, candidates, dense_used, sources)


def _connect_ro(docs_db_path: str | Path) -> sqlite3.Connection:
    """Opens ``docs.db`` read-only; callers wrap the connection in ``contextlib.closing``."""
    uri = quote(Path(docs_db_path).resolve().as_posix(), safe="/:")
    return sqlite3.connect(f"file:{uri}?mode=ro", uri=True)


def _filters(source: str | None, section: str | None) -> tuple[str, list]:
    """Builds the SQL condition for the ``source`` and ``section`` filters.

    Args:
        source: Exact file name, or ``None``/empty for no filter.
        section: Substring of ``section_path``, or ``None``/empty for no filter.

    Returns:
        ``(condition_on_alias_c, parameters)``; the condition is ``1`` with no filters.
    """
    conds, params = ["1"], []
    if source:
        conds.append("c.source = ?")
        params.append(source)
    if section:
        escaped = section.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append("c.section_path LIKE ? ESCAPE '\\'")  # LIKE is case-insensitive for ASCII
        params.append(f"%{escaped}%")
    return " AND ".join(conds), params


def _fts_query(query: str) -> str | None:
    """Turns free text into an FTS5 query: each word quoted, joined with ``OR``.

    Returns:
        The query string, or ``None`` if the text contains no words.
    """
    words = _WORD_RE.findall(query)
    return " OR ".join(f'"{w}"' for w in words) or None


def _bm25(conn: sqlite3.Connection, query: str, where: str, params: list) -> list[int]:
    """Returns up to ``TOP_N`` chunk ids ranked by BM25 (best first)."""
    match = _fts_query(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
        f"WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts), c.chunk_id LIMIT ?",
        [match, *params, TOP_N],
    ).fetchall()
    return [r[0] for r in rows]


def _dense(
    conn: sqlite3.Connection, query_vec: np.ndarray | None, where: str, params: list
) -> tuple[list[tuple[int, float]], bool]:
    """Ranks the filtered chunks by cosine similarity to the query vector.

    Args:
        conn: Open read-only connection.
        query_vec: Query embedding, or ``None``.
        where: Filter condition from :func:`_filters`.
        params: Filter parameters.

    Returns:
        ``(ranked, used)``: up to ``TOP_N`` ``(chunk_id, similarity)`` pairs, best first,
        and whether dense search could run at all.
    """
    if query_vec is None:
        return [], False
    rows = conn.execute(
        f"SELECT c.chunk_id, c.embedding FROM chunks c WHERE c.embedding IS NOT NULL AND {where}", params
    ).fetchall()
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    norm = np.linalg.norm(q)
    if not rows or norm == 0:
        return [], False
    q = q / norm
    ids = [r[0] for r in rows]
    mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    if mat.shape[1] != q.shape[0]:
        return [], False
    sims = mat @ q  # stored vectors are unit length, so the dot product is the cosine
    order = np.argsort(-sims, kind="stable")[:TOP_N]
    return [(ids[i], float(sims[i])) for i in order], True


def _fetch(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple]:
    """Loads the display columns of the given chunks, keyed by chunk id."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT chunk_id, source, section_path, section_number, page_start, page_end, text "
        f"FROM chunks WHERE chunk_id IN ({marks})",
        ids,
    ).fetchall()
    return {r[0]: r[1:] for r in rows}
