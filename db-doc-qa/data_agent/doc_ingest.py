"""Document ingestion: validate, chunk, embed and index uploaded documents in ``docs.db``.

This module is the document half of the pipeline in the design (sections 4 and 5);
:mod:`data_agent.ingest` is the table half. It has two entry points that share one
pipeline:

* :func:`prepare` validates one document and splits it into structure-aware chunks.
  The UI calls it before accepting an upload; it never calls the embedding API.
* :func:`build` runs :func:`prepare` for every document of a session, embeds the
  chunks and writes ``<session_dir>/docs.db`` (tables ``documents``, ``chunks`` and the
  FTS5 table ``chunks_fts``). ``ensure_data`` calls it on the first turn and after a
  Cloud Run instance restart. Retrieval (:mod:`data_agent.retrieval`) only reads the file.

Failures follow the :mod:`data_agent.ingest` pattern: expected problems are returned as a
:class:`DocError` with a user-facing message, not raised.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import semchunk
from pypdf import PdfReader

from data_agent.config import ChunkingConfig, EmbeddingConfig
from data_agent.logs import get_logger

log = get_logger(__name__)

DOCS_DB_FILENAME = "docs.db"
SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md")
MAX_DOC_BYTES = 1024 * 1024
MAX_PAGES = 20
# Aliases of the config defaults, kept so existing imports and tests keep working.
CHUNK_WORDS = ChunkingConfig().chunk_words
FALLBACK_OVERLAP = ChunkingConfig().fallback_overlap
EMBED_MODEL = EmbeddingConfig().model
EMBED_DIM = EmbeddingConfig().dim
EMBED_BATCH = EmbeddingConfig().batch
SUMMARY_SECTIONS = 8  # top-level sections listed per document in ``docs_summary``

TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]

_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
_MD_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")
_CLAUSE_RE = re.compile(r"^\(?([a-z]{1,3})\)\s+\S")
_TITLE_WORD_RE = re.compile(r"^[A-Z][\w'&/-]*$|^[A-Z0-9&/-]+$")
_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
_NO_TITLE_END = (".", ";", ":", ",", "!", "?")


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage of a document, with the metadata used for citations.

    Attributes:
        chunk_index: Position of the chunk within its document (0-based).
        section_path: Headings from the top down, e.g. ``3 Rules > 3.2 Express Shipments``.
        section_number: ``3.2``, or ``3.2(b)`` / ``3.2(a)-(c)`` when a long section was
            split; empty when the heading has no number.
        level: Depth of the section in the path (1 = top level); 0 for fallback chunks.
        parent_section: Path of the parent section, empty at the top level.
        page_start: First page the chunk touches (PDF only, else ``None``).
        page_end: Last page the chunk touches (PDF only, else ``None``).
        chunk_method: ``structure`` or ``fallback``.
        char_start: Offset of the chunk in the document's extracted text.
        char_end: End offset (exclusive) in the same text.
        text: The original wording of the passage.
    """

    chunk_index: int
    section_path: str
    section_number: str
    level: int
    parent_section: str
    page_start: int | None
    page_end: int | None
    chunk_method: str
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True)
class PreparedDoc:
    """A validated, chunked document (nothing embedded or written yet).

    Attributes:
        name: Original file name.
        doc_type: ``pdf``, ``txt`` or ``md``.
        title: Document title; the file name stem when none is found.
        pages: Page count (1 for TXT and MD).
        chunks: The document's chunks, in reading order.
        top_sections: Labels of the top-level sections, for ``docs_summary``.
    """

    name: str
    doc_type: str
    title: str
    pages: int
    chunks: list[Chunk]
    top_sections: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocError:
    """Outcome of a failed validation.

    Attributes:
        message: User-facing explanation of the first validation failure.
    """

    message: str


@dataclass(frozen=True)
class BuildResult:
    """Outcome of :func:`build`.

    Attributes:
        docs_db_path: Path of ``docs.db``, or ``None`` when no document was usable.
        docs_summary: One line per document for the agent instruction.
        skipped: File name -> message for each document that failed validation.
        embedded: Whether the chunks were embedded (``False`` means BM25-only retrieval).
    """

    docs_db_path: Path | None
    docs_summary: str
    skipped: dict[str, str] = field(default_factory=dict)
    embedded: bool = False


@dataclass(frozen=True)
class _Heading:
    """A heading found in the extracted text (internal)."""

    start: int  # offset of the line start
    end: int  # offset just after the line's text
    level: int  # relative depth; only the ordering matters
    number: str
    label: str  # e.g. "3.2 Express Shipments"


def prepare(name: str, data: bytes, chunking: ChunkingConfig | None = None) -> PreparedDoc | DocError:
    """Validates a document and splits it into structure-aware chunks.

    Validation runs in a fixed order and stops at the first failure: extension, size,
    readability (PDF: opens, not encrypted, at most 20 pages; TXT/MD: valid UTF-8), then
    non-empty text. The embedding API is not called.

    Args:
        name: Original file name; its extension selects the reader.
        data: Raw file content.
        chunking: Chunking settings; the defaults when ``None``.

    Returns:
        A :class:`PreparedDoc`, or a :class:`DocError` describing the first failure.
    """
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return DocError("Unsupported document type. Upload a .pdf, .txt or .md file.")
    if len(data) > MAX_DOC_BYTES:
        return DocError("Document too large (limit 1 MB / 20 pages).")

    pages = _read_pdf(data) if ext == ".pdf" else _read_text(data)
    if isinstance(pages, DocError):
        return pages
    text, page_starts = _join_pages(pages)
    if not text.strip():
        return DocError(f"No readable text found in {name}.")

    doc_type = ext[1:]
    base = Path(name).name
    return _chunk_document(base, doc_type, text, page_starts if doc_type == "pdf" else None, chunking or ChunkingConfig())


def embed(texts: list[str], task_type: TaskType, embedding: EmbeddingConfig | None = None) -> np.ndarray | None:
    """Embeds texts (default ``gemini-embedding-001``, 768 dimensions, L2-normalised).

    ``embedding.provider`` selects the backend: ``gemini`` (default) or a local ``ollama`` server.

    Args:
        texts: Strings to embed, sent in batches of up to 100.
        task_type: ``RETRIEVAL_DOCUMENT`` for chunks, ``RETRIEVAL_QUERY`` for queries.
        embedding: Model, dimension and batch size; the defaults when ``None``.

    Returns:
        A float32 array of shape ``(len(texts), dim)`` with unit-length rows, or ``None``
        if the API call fails for any reason (a warning is logged without the texts).
    """
    emb = embedding or EmbeddingConfig()
    if not texts:
        return np.zeros((0, emb.dim), dtype=np.float32)
    try:
        vectors = _embed_ollama(texts, task_type, emb) if emb.provider == "ollama" else _embed_gemini(texts, task_type, emb)
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.shape != (len(texts), emb.dim):
            raise ValueError(f"unexpected embedding shape {arr.shape}")
    except Exception as exc:  # network, quota, auth, malformed response: all fall back to BM25
        log.warning("embedding failed (%s); documents will be searched with BM25 only", type(exc).__name__)
        return None
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.where(norms == 0, 1.0, norms)


def _embed_gemini(texts: list[str], task_type: TaskType, emb: EmbeddingConfig) -> list[list[float]]:
    """Embeds with the Gemini API, in batches of ``emb.batch``."""
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types

    load_dotenv()  # local runs read GOOGLE_API_KEY from .env; Cloud Run sets it directly
    client = genai.Client()
    config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=emb.dim)
    vectors: list[list[float]] = []
    for i in range(0, len(texts), emb.batch):
        resp = client.models.embed_content(model=emb.model, contents=texts[i : i + emb.batch], config=config)
        vectors.extend(e.values for e in resp.embeddings)
    return vectors


def _embed_ollama(texts: list[str], task_type: TaskType, emb: EmbeddingConfig) -> list[list[float]]:
    """Embeds with a local Ollama server (``POST {base_url}/api/embed``), in batches of ``emb.batch``.

    The ``nomic-embed-text`` models are trained with a task prefix, so one is added for them
    (``search_document: `` / ``search_query: ``); other models get the text unchanged.
    """
    import json
    import urllib.request

    prefix = ""
    if emb.model.startswith("nomic-embed-text"):
        prefix = "search_document: " if task_type == "RETRIEVAL_DOCUMENT" else "search_query: "
    vectors: list[list[float]] = []
    for i in range(0, len(texts), emb.batch):
        body = json.dumps({"model": emb.model, "input": [prefix + t for t in texts[i : i + emb.batch]]}).encode()
        request = urllib.request.Request(
            emb.base_url.rstrip("/") + "/api/embed", body, {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            vectors.extend(json.loads(response.read())["embeddings"])
    return vectors


def embed_options(embedding: EmbeddingConfig | None) -> dict:
    """Returns the ``embedding=`` keyword for :func:`embed`, or ``{}`` for the default settings.

    Callers splat this into their ``embed`` call. Leaving the argument out for the default
    keeps test doubles written as ``embed(texts, task_type)`` working.
    """
    return {"embedding": embedding} if embedding is not None and embedding != EmbeddingConfig() else {}


def build(
    docs: list[dict],
    session_dir: str | Path,
    *,
    chunking: ChunkingConfig | None = None,
    embedding: EmbeddingConfig | None = None,
) -> BuildResult:
    """Prepares, embeds and indexes a session's documents into ``<session_dir>/docs.db``.

    Documents that fail validation are skipped and reported; the others stay usable. If
    embedding fails, the file is still written, with null embeddings.

    Args:
        docs: Session-state documents, each ``{"name": str, "b64": str}``.
        session_dir: Per-session directory; created if missing when there is something
            to write.
        chunking: Chunking settings; the defaults when ``None``.
        embedding: Embedding settings; the defaults when ``None``.

    Returns:
        A :class:`BuildResult`. ``docs_db_path`` is ``None`` when no document was usable.

    Side Effects:
        Calls the embedding API. Writes a temp file and moves it over ``docs.db``
        atomically, so a failure leaves any existing ``docs.db`` untouched.
    """
    prepared: list[PreparedDoc] = []
    skipped: dict[str, str] = {}
    for doc in docs:
        name = str(doc.get("name", "")) or "(unnamed)"
        try:
            raw = base64.b64decode(doc.get("b64", ""), validate=True)
        except (binascii.Error, ValueError):
            skipped[name] = "Could not read the document: invalid encoding."
            continue
        result = prepare(name, raw, chunking)
        if isinstance(result, DocError):
            skipped[name] = result.message
            log.warning("skipped a document: %s", result.message.split(":")[0])
        else:
            prepared.append(result)

    if not prepared:
        return BuildResult(None, "No documents loaded", skipped)

    all_chunks = [(i, c) for i, d in enumerate(prepared, start=1) for c in d.chunks]
    vectors = embed([_index_text(c) for _, c in all_chunks], "RETRIEVAL_DOCUMENT", **embed_options(embedding))

    db_path = _write_db(prepared, all_chunks, vectors, Path(session_dir))
    summary = "\n".join(_summary_line(d) for d in prepared)
    return BuildResult(db_path, summary, skipped, embedded=vectors is not None)


# --------------------------------------------------------------------------- reading


def _read_pdf(data: bytes) -> list[str] | DocError:
    """Extracts the text of each page of a PDF.

    Args:
        data: Raw PDF bytes.

    Returns:
        One string per page, or a :class:`DocError` for unreadable, encrypted or
        over-long files.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return DocError("Could not read the document: the PDF is encrypted.")
        if len(reader.pages) > MAX_PAGES:
            return DocError("Document too large (limit 1 MB / 20 pages).")
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises many types for corrupt files
        return DocError(f"Could not read the document: {_reason(exc)}.")


def _read_text(data: bytes) -> list[str] | DocError:
    """Decodes a TXT or MD file as UTF-8 (a BOM is accepted).

    Args:
        data: Raw file bytes.

    Returns:
        A single-element page list, or a :class:`DocError` if the bytes are not UTF-8.
    """
    try:
        return [data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")]
    except UnicodeDecodeError:
        return DocError("Could not read the document: the file is not valid UTF-8 text.")


def _reason(exc: Exception) -> str:
    """Returns a short, single-line reason for a reader exception."""
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:120]


def _join_pages(pages: list[str]) -> tuple[str, list[int]]:
    """Joins page texts into one string and records where each page starts.

    Args:
        pages: Text of each page.

    Returns:
        ``(text, page_starts)`` where ``page_starts[i]`` is the offset of page ``i + 1``.
    """
    starts, parts, pos = [], [], 0
    for page in pages:
        starts.append(pos)
        parts.append(page)
        pos += len(page) + 1  # the "\n" separator
    return "\n".join(parts), starts


def _page_at(page_starts: list[int], offset: int) -> int:
    """Returns the 1-based page that contains ``offset``."""
    page = 1
    for i, start in enumerate(page_starts, start=1):
        if start <= offset:
            page = i
    return page


# ------------------------------------------------------------------------- structure


def _lines(text: str) -> list[tuple[int, int, str]]:
    """Splits text into ``(start, end, stripped_line)`` keeping character offsets."""
    out, pos = [], 0
    for raw in text.split("\n"):
        out.append((pos, pos + len(raw), raw.strip()))
        pos += len(raw) + 1
    return out


def _is_title_like(line: str) -> bool:
    """Tells whether a line looks like a title: short, capitalised, no closing punctuation."""
    words = line.split()
    if not 1 <= len(words) <= 8 or line.endswith(_NO_TITLE_END):
        return False
    if _NUMBERED_RE.match(line) or _CLAUSE_RE.match(line) or not any(c.isalpha() for c in line):
        return False
    return all(w.lower() in _SMALL_WORDS or _TITLE_WORD_RE.match(w) for w in words)


def _numbered_heading(line: str) -> tuple[str, str] | None:
    """Matches ``3.2 Express Shipments``; returns ``(number, label)``.

    A numbered line is a heading only if it reads like one (short, starts with a capital
    letter, no closing punctuation); this keeps list items and wrapped sentence
    fragments such as ``24 hours of ...`` out.
    """
    m = _NUMBERED_RE.match(line)
    if not m:
        return None
    number, rest = m.groups()
    if len(rest.split()) > 10 or not rest[0].isupper() or rest.endswith(_NO_TITLE_END):
        return None
    return number, f"{number} {rest}"


def _find_headings(text: str, doc_type: str) -> tuple[list[_Heading], str | None, int]:
    """Finds headings line by line.

    Numbered headings and ``#`` lines are tried first. Only when a document has neither
    does the title-like rule apply, because plain-text extraction wraps lines and would
    otherwise produce false headings.

    Args:
        text: The document's extracted text.
        doc_type: ``pdf``, ``txt`` or ``md``.

    Returns:
        ``(headings, title, body_start)``: the headings in order, the title (or ``None``),
        and the offset where the text after the title line starts.
    """
    lines = _lines(text)
    first = next((i for i, (_, _, s) in enumerate(lines) if s), None)
    found: list[_Heading] = []
    for _, (start, end, line) in enumerate(lines):
        if not line:
            continue
        md = _MD_RE.match(line)
        if md:
            hashes, body = md.groups()
            num = _numbered_heading(body)
            number, label = num if num else ("", body)
            found.append(_Heading(start, end, len(hashes), number, label))
            continue
        num = _numbered_heading(line)
        if num:
            found.append(_Heading(start, end, num[0].count(".") + 1, num[0], num[1]))

    title: str | None = None
    body_start = 0
    if first is not None:
        f_start, f_end, f_line = lines[first]
        # An unnumbered level-1 "#" line, or a title-like first line, names the document.
        lead = next((h for h in found if h.start == f_start), None)
        if lead is not None and lead.level == 1 and not lead.number and doc_type != "pdf":
            title, body_start = lead.label, f_end
            found.remove(lead)
        elif lead is None and _is_title_like(f_line) and len(f_line.split()) <= 8:
            title, body_start = f_line, f_end

    if not found:
        found = _title_like_headings(lines, doc_type, skip=first if title else None)
    return found, title, body_start


def _title_like_headings(
    lines: list[tuple[int, int, str]], doc_type: str, skip: int | None
) -> list[_Heading]:
    """Finds headings for documents with no numbering or ``#`` lines.

    Args:
        lines: ``(start, end, line)`` for every line.
        doc_type: ``pdf``, ``txt`` or ``md``.
        skip: Index of the title line, which is not a section heading.

    Returns:
        Level-1 headings; empty if there are too few candidates to trust. PDFs have no
        blank lines to anchor on, so they need at least two candidates.
    """
    cands = []
    for i, (start, end, line) in enumerate(lines):
        if i == skip or not line or not _is_title_like(line):
            continue
        prev_blank = i == 0 or not lines[i - 1][2]
        if doc_type != "pdf" and not prev_blank:
            continue
        cands.append(_Heading(start, end, 1, "", line))
    return cands if len(cands) >= (2 if doc_type == "pdf" else 1) else []


def _chunk_document(
    name: str, doc_type: str, text: str, page_starts: list[int] | None, chunking: ChunkingConfig
) -> PreparedDoc | DocError:
    """Builds the chunks of one document, structure-aware or by the fallback.

    Args:
        name: File name (also the title when the text has none).
        doc_type: ``pdf``, ``txt`` or ``md``.
        text: Extracted text of the whole document.
        page_starts: Start offset of each page, or ``None`` for TXT and MD.
        chunking: Chunk size and fallback overlap.

    Returns:
        A :class:`PreparedDoc`, or a :class:`DocError` if no chunk could be made.
    """
    headings, title, body_start = _find_headings(text, doc_type)
    title = title or Path(name).stem
    chunker = semchunk.chunkerify(lambda t: len(t.split()), chunking.chunk_words)

    def pages_of(start: int, end: int) -> tuple[int | None, int | None]:
        if page_starts is None:
            return None, None
        return _page_at(page_starts, start), _page_at(page_starts, max(start, end - 1))

    raw: list[tuple[str, str, int, str, int, int, str]] = []  # path, number, level, parent, start, end, method
    top_sections: list[str] = []

    if not headings:
        method = "fallback"
        for chunk_text, (s, e) in zip(*chunker(text, offsets=True, overlap=chunking.fallback_overlap)):
            raw.append((title, "", 0, "", s, e, method))
    else:
        method = "structure"
        # Text before the first heading (minus the title line) is the preamble.
        bounds = [(body_start, headings[0].start, None)]
        bounds += [
            (h.start, headings[i + 1].start if i + 1 < len(headings) else len(text), h)
            for i, h in enumerate(headings)
        ]
        stack: list[_Heading] = []
        for start, end, heading in bounds:
            if heading is None:
                path, number, level, parent = title, "", 0, ""
            else:
                while stack and stack[-1].level >= heading.level:
                    stack.pop()
                stack.append(heading)
                labels = [h.label for h in stack]
                path, number, level = " > ".join(labels), heading.number, len(stack)
                parent = " > ".join(labels[:-1])
                if level == 1:
                    top_sections.append(heading.label)
                body_from = heading.end
                if not text[body_from:end].strip():
                    continue  # a heading with no body of its own; its children carry the content
            segment = text[start:end]
            if not segment.strip():
                continue
            pieces = list(zip(*chunker(segment, offsets=True)))
            for chunk_text, (s, e) in pieces:
                num = _clause_number(number, chunk_text) if len(pieces) > 1 else number
                raw.append((path, num, level, parent, start + s, start + e, method))

    chunks = []
    for path, number, level, parent, s, e, m in raw:
        piece = text[s:e].strip()
        if not piece:
            continue
        s += len(text[s:e]) - len(text[s:e].lstrip())
        ps, pe = pages_of(s, s + len(piece))
        chunks.append(Chunk(len(chunks), path, number, level, parent, ps, pe, m, s, s + len(piece), piece))
    if not chunks:
        return DocError(f"No readable text found in {name}.")
    n_pages = len(page_starts) if page_starts else 1
    return PreparedDoc(name, doc_type, title, n_pages, chunks, top_sections)


def _clause_number(number: str, chunk_text: str) -> str:
    """Appends the clause range of a split section, e.g. ``3.2(a)-(c)``.

    Args:
        number: The section number (may be empty).
        chunk_text: Text of one chunk of a section that was split into several.

    Returns:
        ``number`` plus the first and last ``(x)`` clause markers found at the start of
        a line in the chunk, or ``number`` unchanged if there are none.
    """
    marks = [m.group(1) for line in chunk_text.split("\n") if (m := _CLAUSE_RE.match(line.strip()))]
    if not marks or not number:
        return number
    return f"{number}({marks[0]})" if len(marks) == 1 or marks[0] == marks[-1] else f"{number}({marks[0]})-({marks[-1]})"


# -------------------------------------------------------------------------- storage


def _index_text(chunk: Chunk) -> str:
    """Text that is embedded and indexed: the section path in front of the chunk."""
    return f"{chunk.section_path}\n{chunk.text}"


def _summary_line(doc: PreparedDoc) -> str:
    """One line describing a document for the agent instruction."""
    shown = doc.top_sections[:SUMMARY_SECTIONS]
    more = ", ..." if len(doc.top_sections) > SUMMARY_SECTIONS else ""
    pages = f"{doc.pages} page{'s' if doc.pages != 1 else ''}" if doc.doc_type == "pdf" else doc.doc_type.upper()
    sections = f"; sections: {', '.join(shown)}{more}" if shown else ""
    return f"{doc.name} — {doc.title} ({pages}, {len(doc.chunks)} chunks{sections})"


def _write_db(
    docs: list[PreparedDoc],
    all_chunks: list[tuple[int, Chunk]],
    vectors: np.ndarray | None,
    session_dir: Path,
) -> Path:
    """Writes ``docs.db`` atomically.

    Args:
        docs: Prepared documents; ``doc_id`` is the 1-based position in this list.
        all_chunks: ``(doc_id, chunk)`` pairs in the order the vectors were computed.
        vectors: One row per chunk, or ``None`` to store null embeddings.
        session_dir: Directory to hold the file.

    Returns:
        Path of ``docs.db``.

    Side Effects:
        Creates ``session_dir``; replaces ``docs.db`` only after a successful write.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = session_dir / DOCS_DB_FILENAME
    tmp_path = session_dir / (DOCS_DB_FILENAME + ".tmp")
    try:
        tmp_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(tmp_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE documents (
                    doc_id INTEGER PRIMARY KEY, source TEXT, doc_type TEXT,
                    doc_title TEXT, pages INTEGER);
                CREATE TABLE chunks (
                    chunk_id INTEGER PRIMARY KEY, doc_id INTEGER, chunk_index INTEGER,
                    source TEXT, doc_type TEXT, doc_title TEXT,
                    section_path TEXT, section_number TEXT, level INTEGER, parent_section TEXT,
                    page_start INTEGER, page_end INTEGER, chunk_method TEXT,
                    char_start INTEGER, char_end INTEGER, text TEXT, embedding BLOB);
                CREATE VIRTUAL TABLE chunks_fts USING fts5(content, tokenize = 'porter unicode61');
                """
            )
            by_id = {i: d for i, d in enumerate(docs, start=1)}
            conn.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                [(i, d.name, d.doc_type, d.title, d.pages) for i, d in by_id.items()],
            )
            for n, (doc_id, c) in enumerate(all_chunks):
                d = by_id[doc_id]
                blob = vectors[n].astype(np.float32).tobytes() if vectors is not None else None
                cur = conn.execute(
                    "INSERT INTO chunks (doc_id, chunk_index, source, doc_type, doc_title, section_path,"
                    " section_number, level, parent_section, page_start, page_end, chunk_method,"
                    " char_start, char_end, text, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, c.chunk_index, d.name, d.doc_type, d.title, c.section_path, c.section_number,
                     c.level, c.parent_section, c.page_start, c.page_end, c.chunk_method,
                     c.char_start, c.char_end, c.text, blob),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, content) VALUES (?, ?)", (cur.lastrowid, _index_text(c))
                )
            conn.commit()
        os.replace(tmp_path, db_path)  # atomic; the connection above is already closed
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return db_path
