import sqlite3
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from data_agent import sessions
from data_agent.ingest import IngestError, load_file

S = Path('sample_data')
print('--- files ---')
for n in ['sales.csv', 'sales.xlsx', 'multi_sheet.xlsx', 'notes.txt', 'empty.csv', 'headerless.csv',
          'numeric_header.csv', 'duplicate_header.csv', 'corrupt.xlsx']:
    sd = Path('tmp_session_17') / n
    r = load_file(n, S.joinpath(n).read_bytes(), sd)
    print(n, '->', r.message if isinstance(r, IngestError) else (r.row_count, r.column_count, r.ignored_sheets),
          '| db created:', (sd / 'data.db').exists())

df = pd.read_csv('sample_data/sales.csv')
print('--- pandas ---')
print('avg sales_amount:', df['sales_amount'].mean())
print('top revenue category:', df.groupby('category')['revenue'].sum().idxmax())

at = AppTest.from_file('../app.py', default_timeout=120).run()
sid = at.session_state.session_id
db = sessions.ROOT / sid / 'data.db'


def rows():
    with sqlite3.connect(db) as c:
        return c.execute('SELECT COUNT(*) FROM data').fetchone()[0]


for q in ['How many records are there?', 'What is the average sales amount?',
          'Which category has the highest total revenue?', 'What is the profit margin?',
          'Delete all rows', 'How many records are there?']:
    at.chat_input[0].set_value(q).run()
    m = at.session_state.messages[-1]
    print('\nQ:', q, '\nA:', m['content'], '\ncalls:', [(c['name'], c['args']) for c in m['calls']], '| rows in db:', rows())

at.button[0].click().run()
print('\nfolder after End session:', (sessions.ROOT / sid).exists())

print('\n--- upload flow ---')
at = AppTest.from_file('../app.py', default_timeout=120).run()
at.chat_input[0].set_value('How many records are there?').run()
print('messages before upload:', len(at.session_state.messages))
for n in ['duplicate_header.csv', 'multi_sheet.xlsx']:
    p = S / n
    at.file_uploader[0].upload(p.name, p.read_bytes()).run()
    print(n, '| errors:', [e.value for e in at.error], '| subheader:', at.subheader[0].value,
          '| messages:', len(at.session_state.messages), '| warning:', [w.value for w in at.warning])
sessions.end_session(sessions.ROOT / at.session_state.session_id)
