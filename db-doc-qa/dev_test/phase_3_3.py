"""Phase 3.3: hybrid search over the sample documents with real embeddings; tunes MIN_DENSE_SIM.

Needs GOOGLE_API_KEY. For each labelled query it prints the best dense similarity of a
relevant chunk, and for off-topic queries the best dense similarity of any chunk. A good
threshold sits above the off-topic maximum and below the relevant minimum.
"""
import base64
import tempfile
from contextlib import closing
from pathlib import Path

import numpy as np

from data_agent import doc_ingest, retrieval
from data_agent.ingest import SAMPLE_DOCS_DIR

docs = [{'name': p.name, 'b64': base64.b64encode(p.read_bytes()).decode()} for p in sorted(SAMPLE_DOCS_DIR.iterdir())]
db = doc_ingest.build(docs, tempfile.mkdtemp()).docs_db_path

# (query, source, section numbers that answer it)
RELEVANT = [
    ('late parcel', 'shipment_delay_policy.pdf', {'2.1'}),
    ('How long can an Express shipment be late before we escalate?', 'shipment_delay_policy.pdf', {'3.2'}),
    ('who do I contact for a Tier 2 escalation', 'shipment_delay_policy.pdf', {'4.3'}),
    ('what happens if a parcel is damaged or lost', 'shipment_delay_policy.pdf', {'5.1'}),
    ('what can a customer do if a shipment is late', 'customer_communication_guidelines.pdf', None),
    ('papers needed to send goods abroad', 'international_shipping_procedures.txt', {'2.1', '2.2'}),
    ('parcel stopped by customs', 'international_shipping_procedures.txt', {'3.1', '3.2'}),
    ('international escalation threshold', 'international_shipping_procedures.txt', {'4.2'}),
]
OFF_TOPIC = ['What is the policy for lost pets?', 'best pizza recipe', 'how do I reset my password', 'weather forecast for tomorrow']

print(f'MIN_DENSE_SIM = {retrieval.MIN_DENSE_SIM}\n')
rel_sims = []
for query, source, numbers in RELEVANT:
    vec = doc_ingest.embed([query], 'RETRIEVAL_QUERY')[0]
    res = retrieval.search(db, query, source=source, top_k=20, query_vec=vec)
    dense = [h for h in res.hits if h.similarity is not None]
    # similarity of the best chunk that is actually relevant
    best = max((h.similarity for h in dense if numbers is None or h.section_number in numbers), default=None)
    top = res.hits[0]
    print(f'REL  best relevant sim={best if best is None else round(best, 3)}  top hit: §{top.section_number} {top.matched_by} | {query}')
    if best is not None:
        rel_sims.append(best)

off_sims = []
with closing(retrieval._connect_ro(db)) as conn:
    rows = conn.execute('SELECT embedding FROM chunks').fetchall()
mat = np.vstack([np.frombuffer(r[0], dtype=np.float32) for r in rows])
for query in OFF_TOPIC:
    vec = doc_ingest.embed([query], 'RETRIEVAL_QUERY')[0]
    top = float((mat @ vec).max())
    off_sims.append(top)
    print(f'OFF  max sim over all chunks={top:.3f} | {query}')

print(f'\nrelevant: min={min(rel_sims):.3f}  off-topic: max={max(off_sims):.3f}')
print('hybrid example (no filter): "Customs Hold"')
vec = doc_ingest.embed(['Customs Hold'], 'RETRIEVAL_QUERY')[0]
for h in retrieval.search(db, 'Customs Hold', top_k=4, query_vec=vec).hits:
    sim = '-' if h.similarity is None else f'{h.similarity:.2f}'
    print(f'  {h.score:.4f} {h.matched_by:5} sim={sim:5} {h.source} §{h.section_number} p.{h.page_start}')
