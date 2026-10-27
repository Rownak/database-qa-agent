import sqlite3
import tempfile
from pathlib import Path

from data_agent.ingest import IngestError, load_files, load_sample

session = Path(tempfile.mkdtemp()) / 'session'


def rows():
    with sqlite3.connect(session / 'data.db') as c:
        return c.execute('SELECT COUNT(*) FROM "' + c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()[0] + '"').fetchone()[0]


print('sample:', load_sample(session).tables[0].row_count, rows())

r = load_files([('notes.txt', Path('sample_data/database/notes.txt').read_bytes())], session)
print('invalid upload:', r.message if isinstance(r, IngestError) else 'ACCEPTED?', '| rows still', rows())

p = Path('sample_data/database/multi_sheet.xlsx')
r = load_files([(p.name, p.read_bytes())], session)
print('valid upload:', r.tables[0].row_count, r.tables[0].ignored_sheets, '| rows now', rows())
print('leftover files:', sorted(f.name for f in session.iterdir()))
