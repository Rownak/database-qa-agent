import sqlite3
import tempfile
from pathlib import Path

from data_agent.ingest import IngestError, load_file, load_sample

session = Path(tempfile.mkdtemp()) / 'session'


def rows():
    with sqlite3.connect(session / 'data.db') as c:
        return c.execute('SELECT COUNT(*) FROM data').fetchone()[0]


print('sample:', load_sample(session).row_count, rows())

r = load_file('notes.txt', Path('sample_data/database/notes.txt').read_bytes(), session)
print('invalid upload:', r.message if isinstance(r, IngestError) else 'ACCEPTED?', '| rows still', rows())

p = Path('sample_data/database/multi_sheet.xlsx')
r = load_file(p.name, p.read_bytes(), session)
print('valid upload:', r.row_count, r.ignored_sheets, '| rows now', rows())
print('leftover files:', sorted(f.name for f in session.iterdir()))
