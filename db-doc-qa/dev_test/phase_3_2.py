"""Phase 3.2: validate, chunk, embed (real Gemini call) and index the sample documents."""
import base64
import sqlite3
import tempfile
from pathlib import Path

from data_agent.doc_ingest import DocError, build, prepare
from data_agent.ingest import SAMPLE_DOCS_DIR

for path in sorted(Path('sample_data/documents').iterdir()):
    r = prepare(path.name, path.read_bytes())
    print(f'{path.name:22} ->', r.message if isinstance(r, DocError) else f'{r.pages} page(s), {len(r.chunks)} chunks, {r.chunks[0].chunk_method}')

policy = prepare('shipment_delay_policy.pdf', (SAMPLE_DOCS_DIR / 'shipment_delay_policy.pdf').read_bytes())
print('\nshipment_delay_policy.pdf chunks:')
for c in policy.chunks:
    print(f'  {c.section_number or "-":5} p.{c.page_start}-{c.page_end}  {c.section_path}')

docs = [{'name': p.name, 'b64': base64.b64encode(p.read_bytes()).decode()} for p in sorted(SAMPLE_DOCS_DIR.iterdir())]
res = build(docs, tempfile.mkdtemp())  # calls the real embedding API; needs GOOGLE_API_KEY
print(f'\ndocs.db: {res.docs_db_path}\nembedded: {res.embedded}, skipped: {res.skipped}\n{res.docs_summary}')
with sqlite3.connect(res.docs_db_path) as conn:
    print('chunks:', conn.execute('SELECT count(*) FROM chunks').fetchone()[0],
          '| with embedding:', conn.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0])
