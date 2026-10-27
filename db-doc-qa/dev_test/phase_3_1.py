"""Phase 3.1: the shipments sample, the sample policy documents and the document fixtures."""
import tempfile
from pathlib import Path

import semchunk
from pypdf import PdfReader

from data_agent.ingest import load_sample

r = load_sample(Path(tempfile.mkdtemp()))
print(f'default sample: {r.tables[0].row_count} rows x {r.tables[0].column_count} columns')
print(r.schema_summary)

for f in sorted(Path('data_agent/sample_data/default/docs').iterdir()):
    if f.suffix == '.pdf':
        pages = [p.extract_text() for p in PdfReader(f).pages]
        print(f'\n{f.name}: {len(pages)} page(s); page 2 starts with: {pages[1].splitlines()[0][:60]!r}')
    else:
        print(f'\n{f.name}: {len(f.read_text(encoding="utf-8").splitlines())} lines')

for name in ['scanned.pdf', 'encrypted.pdf', 'oversize_pages.pdf']:
    reader = PdfReader(Path('sample_data/documents') / name)
    detail = 'encrypted' if reader.is_encrypted else f'{len(reader.pages)} page(s), text={reader.pages[0].extract_text()!r:.40}'
    print(f'fixture {name}: {detail}')

chunker = semchunk.chunkerify(lambda t: len(t.split()), 160)
text = Path('data_agent/sample_data/default/docs/international_shipping_procedures.txt').read_text(encoding='utf-8')
print(f'\nsemchunk smoke test: {len(chunker(text))} chunk(s) of <=160 words from the international procedures')

md = Path('sample_data/documents/structured.md').read_text(encoding='utf-8')
print('markdown fixture headings:', [l for l in md.splitlines() if l.startswith('#')][:4], '...')
