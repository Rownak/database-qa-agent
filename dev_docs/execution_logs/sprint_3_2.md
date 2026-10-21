# Sprint 3 — Phase 3.2 Execution Log: Document Ingestion

## Summary
`data_agent/doc_ingest.py` is in place. It validates a PDF, TXT or MD file, splits it into structure-aware chunks with section paths, section numbers and page ranges, embeds the chunks with `gemini-embedding-001`, and writes `docs.db` (`documents`, `chunks`, `chunks_fts`). Nothing else calls it yet: Phase 3.3 adds retrieval, and Phase 3.5 wires `build` into `ensure_data`. **113 tests pass** (30 new).

## What was implemented

**`prepare(name, data)` → `PreparedDoc | DocError`**
- Validation runs in the design §4 order and stops at the first failure. The messages are those in project description §7:
  - extension → "Unsupported document type. Upload a .pdf, .txt or .md file."
  - size above 1 MB, or more than 20 pages → "Document too large (limit 1 MB / 20 pages)."
  - encrypted or corrupt PDF, or non-UTF-8 text → "Could not read the document: <reason>."
  - empty or image-only → "No readable text found in <name>."
- PDF text is extracted page by page. Pages are joined with `\n`, and the start offset of each page is kept, so every character offset maps to a page. TXT and MD are decoded as `utf-8-sig`, with line endings normalised.

**Headings and section paths**
- Numbered headings (`3`, `3.2`) and `#` lines are found line by line. A stack gives each heading its parent: `3 Rules by Shipment Type > 3.2 Express Shipments`.
- Title-like lines (short, capitalised, no closing punctuation) are used as headings only when a document has no numbered or `#` headings. They need a blank line before them in TXT and MD, and at least two candidates in a PDF, because PDF extraction wraps lines and would otherwise produce false headings.
- The title is the first line (title-like), or an unnumbered level-1 `#` line. It is used as the path of the preamble text and is not part of the section path. When there is no title, the file name stem is used.
- `level` is the depth in the path (1 = top level). Preamble and fallback chunks have `level` 0.

**Chunking**
- Each section is split with `semchunk`: 160 words (whitespace-separated), no overlap, and a chunk never crosses into another section. A section that fits stays as one chunk, so the sample policy yields one chunk per sub-section.
- A heading with no body of its own (e.g. `3 Rules by Shipment Type`) produces no chunk. Its children carry the content and the path.
- The first chunk of a section starts with its heading line. So **3.2 Express Shipments**, whose heading is at the bottom of page 1 and its clauses on page 2, is cited as p. 1–2.
- Documents with no headings use the fallback: whole-document semchunk with 15% overlap, `chunk_method = "fallback"`.

**`embed(texts, task_type)`**
- Batches of up to 100, `output_dimensionality=768`, L2-normalised float32. It returns `None` on any error and logs only the exception type, never the text.
- The API key comes from `GOOGLE_API_KEY` (loaded from `.env` locally).

**`build(docs, session_dir)` → `BuildResult`**
- `docs` is the session-state format: a list of `{name, b64}`.
- It runs `prepare` on each document and skips failures. Chunks of all documents go to one embedding call (`RETRIEVAL_DOCUMENT`) with the section path added to the front.
- The temp file is moved over `docs.db` with `os.replace`. The same text (path + chunk) is indexed in `chunks_fts` (`porter unicode61`, `rowid` = `chunk_id`). `chunks.text` keeps the original wording.
- If embedding fails, `docs.db` is still written with null embeddings, and `embedded` is `False`.
- `BuildResult` holds `docs_db_path`, `docs_summary` (one line per document: name, title, pages, chunk count, top-level sections), `skipped` (name → message) and `embedded`. If no document is usable, nothing is written, the path is `None` and the summary is "No documents loaded".

## Decisions and assumptions
- **`(a)` clauses are not section boundaries.** They are found with the clause regex, but a clause stays in its parent section's chunk. That keeps a rule together with its conditions (design §6 says one chunk per sub-section or clause group). A clause range is added to `section_number` only when a long section is split into several chunks, e.g. `1(a)-(c)`. Otherwise, the number is the section's, such as `3.2`. Say if you want every clause as its own chunk with numbers like `3.2(b)`.
- **A numbered line is a heading only if it reads like one:** at most 10 words, starts with a capital letter, no closing punctuation. This keeps list items (`1. Scan the parcel.`) and wrapped fragments (`24 hours of ...`) out.
- **`page_start`/`page_end` are `None` for TXT and MD** (project description §6: PDF only). `PreparedDoc.pages` is 1 for those.
- **`skipped` is a dict of name → message**, so Phase 3.5 can put the reason into `load_error`.
- **Fallback overlap is applied by `semchunk` on words**, so a fallback chunk can start mid-sentence. The "no chunk ends mid-sentence" test therefore covers structured documents only.
- `PreparedDoc` does not keep the full text; the offsets are checked against the source in tests.

## Files
- **Created:** `data_agent/doc_ingest.py`, `tests/test_doc_ingest.py`, `dev_test/phase_3_2.py`, this log.
- **Modified:** `dev_docs/sprints/sprint3.md` (Phase 3.2 tasks ticked).
- No dependency changes: `pypdf`, `semchunk` and `google-genai` were already installed.

## Tests (embeddings mocked)
`tests/test_doc_ingest.py` covers:
- each validation message, the order of the checks, and a BOM and an uppercase extension
- the expected `section_path`, `section_number`, `level` and page range for known sections of the sample policy (3.2 → p. 1–2, 5.1 → p. 2–3)
- no heading line inside a chunk and every chunk ending on `. ; : )`
- an oversized section split into several chunks with the same path and clause ranges
- the fallback for `no_headings.txt`, title-like headings, list items that are not headings, and Markdown (offsets match the source)
- `embed`: batching (250 → 100/100/50), normalisation, and `None` on an API error
- `build`: tables and FTS rows, `"Customs Hold"` found through FTS5, skipped documents, nothing written without a usable document, null embeddings when embedding fails, the path in front of the embedded text, and original wording in `text`
- an FTS5 table can be created (guards against Python builds without FTS5)

## How to run
From `db-doc-qa/`:

```bash
uv run pytest                          # 113 passed
uv run python -m dev_test.phase_3_2    # needs GOOGLE_API_KEY: calls the real embedding API
```

The dev script printed the result for every fixture, the 16 chunks of the delay policy with pages, and a `docs.db` with 37 chunks, all embedded.
