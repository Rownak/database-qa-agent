import tempfile
from pathlib import Path

from data_agent.config import AgentConfig
from data_agent.ingest import load_files
from data_agent.runner import AgentSession, files_state

S = Path('sample_data/database')
files = [S / 'multi_db' / 'sales.csv', S / 'sales.xlsx', S / 'multi_sheet.xlsx']

r = load_files([(p.name, p.read_bytes()) for p in files], Path(tempfile.mkdtemp()) / 'session')
print([(t.table, t.file_name, t.row_count) for t in r.tables])
print(r.schema_summary)
print('fourth file ->', load_files([(p.name, p.read_bytes()) for p in files] + [('x.csv', b'a\n1\n')], tempfile.mkdtemp()).message)

# Live agent (needs GOOGLE_API_KEY): a question that needs two tables.
session = AgentSession(AgentConfig(), files_state(files[:2], []))
turn = session.ask('Compare the total revenue in the sales and sales_2 tables. Do they match?')
session.close()
print(turn.answer)
for c in turn.calls:
    if c['name'] == 'run_sql':
        print('SQL:', c['args'].get('query'))
