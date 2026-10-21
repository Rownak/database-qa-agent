"""Builds the sample policy documents and the PDF test fixtures (developer tool).

The demo's default documents live in ``data_agent/sample_data/default/docs/``. Their text is
kept as plain ``.txt`` sources in ``scripts/sample_docs/`` so it can be reviewed and
edited; this script renders two of them to PDF with ``fpdf2`` (a dev-only dependency)
and copies the third as-is, so the sample set covers both supported formats. It also
writes the PDF fixtures the document-ingestion tests need (image-only, encrypted, too
many pages) into ``sample_data/documents/``. The generated files are committed; rerun the script
only after editing a source.

Run from ``db-doc-qa/``::

    uv run python scripts/make_sample_pdfs.py
"""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "scripts" / "sample_docs"
DOCS_OUT = ROOT / "data_agent" / "sample_data" / "default" / "docs"
FIXTURES_OUT = ROOT / "sample_data" / "documents"

PDF_SOURCES = ("shipment_delay_policy.txt", "customer_communication_guidelines.txt")
TEXT_SOURCES = ("international_shipping_procedures.txt",)

_HEADING_RE = re.compile(r"^\d+(\.\d+)*\s+\S")
_CLAUSE_RE = re.compile(r"^\([a-z]{1,3}\)\s")
_LINE_H = 6


def render_pdf(source: Path, target: Path) -> int:
    """Renders one sample ``.txt`` source as a simple PDF.

    The first line becomes the title, numbered lines become bold headings, and
    ``(a)`` clauses are indented, so the PDF keeps the structure that structure-aware
    chunking looks for.

    Args:
        source: The ``.txt`` file to render.
        target: The PDF path to write.

    Returns:
        The number of pages written.

    Side Effects:
        Writes ``target``.
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 9, lines[0], new_x="LMARGIN", new_y="NEXT")

    for line in lines[1:]:
        if not line.strip():
            pdf.ln(2)
        elif _HEADING_RE.match(line):
            top_level = "." not in line.split()[0]
            pdf.ln(2 if top_level else 0)
            pdf.set_font("Helvetica", "B", 13 if top_level else 11)
            pdf.multi_cell(0, _LINE_H + 1, line, new_x="LMARGIN", new_y="NEXT")
        elif _CLAUSE_RE.match(line):
            pdf.set_font("Helvetica", "", 11)
            pdf.set_x(pdf.l_margin + 6)
            pdf.multi_cell(0, _LINE_H, line, new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("Helvetica", "", 11)
            pdf.multi_cell(0, _LINE_H, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(target))
    return pdf.pages_count


def make_scanned(target: Path) -> None:
    """Writes a PDF whose only content is an image of text (no extractable text).

    Args:
        target: The PDF path to write.
    """
    img = Image.new("RGB", (1240, 400), "white")
    ImageDraw.Draw(img).text((40, 40), "Scanned policy page: this text is an image.", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.image(buf, x=10, y=10, w=190)
    pdf.output(str(target))


def make_encrypted(target: Path) -> None:
    """Writes a one-page PDF protected by a user password.

    Args:
        target: The PDF path to write.
    """
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_text_pdf(["Confidential policy."]))))
    writer.encrypt(user_password="secret", owner_password="owner")
    with target.open("wb") as f:
        writer.write(f)


def make_oversize(target: Path, pages: int = 21) -> None:
    """Writes a small text PDF with one page more than the 20-page limit.

    Args:
        target: The PDF path to write.
        pages: Number of pages to write.
    """
    target.write_bytes(_text_pdf([f"Page {i} of the oversize fixture." for i in range(1, pages + 1)]))


def _text_pdf(pages: list[str]) -> bytes:
    """Returns the bytes of a PDF with one short line of text per page."""
    pdf = FPDF(format="A4")
    pdf.set_font("Helvetica", "", 12)
    for text in pages:
        pdf.add_page()
        pdf.cell(0, 10, text)
    return bytes(pdf.output())


def main() -> None:
    """Regenerates every sample document and PDF fixture, then prints a summary."""
    DOCS_OUT.mkdir(parents=True, exist_ok=True)
    for name in PDF_SOURCES:
        target = DOCS_OUT / name.replace(".txt", ".pdf")
        print(f"{target.relative_to(ROOT)}: {render_pdf(SOURCES / name, target)} page(s)")
    for name in TEXT_SOURCES:
        shutil.copyfile(SOURCES / name, DOCS_OUT / name)
        print(f"{(DOCS_OUT / name).relative_to(ROOT)}: copied")

    make_scanned(FIXTURES_OUT / "scanned.pdf")
    make_encrypted(FIXTURES_OUT / "encrypted.pdf")
    make_oversize(FIXTURES_OUT / "oversize_pages.pdf")
    print("fixtures: scanned.pdf, encrypted.pdf, oversize_pages.pdf")


if __name__ == "__main__":
    main()
