from pathlib import Path
from types import SimpleNamespace
import tempfile
from data_agent.ingest import load_files
from data_agent import tools
p = Path("sample_data/database/multi_db/sales.csv")
r = load_files([(p.name, p.read_bytes())], Path(tempfile.mkdtemp()) / 'session')
ctx = SimpleNamespace(state={'db_path': str(r.db_path), 'column_map': r.column_map})
print(tools.run_sql('WITH x AS (SELECT 1) DELETE FROM sales', tool_context=ctx))
print(tools.run_sql('SELECT COUNT(*) FROM sales', tool_context=ctx))
