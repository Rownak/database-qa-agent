from pathlib import Path
import tempfile
from data_agent.ingest import load_files
p = Path("sample_data/database/multi_db/sales.csv")
r = load_files([(p.name, p.read_bytes())], Path(tempfile.mkdtemp()) / 'session')
print(r.tables[0].row_count, r.tables[0].column_count)
print(r.schema_summary)
