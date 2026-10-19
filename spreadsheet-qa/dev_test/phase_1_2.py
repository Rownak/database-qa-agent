from pathlib import Path
import tempfile
from data_agent.ingest import load_file
p = Path("data_agent/sample_data/sales.csv")
r = load_file(p.name, p.read_bytes(), Path(tempfile.mkdtemp()) / 'session')
print(r.row_count, r.column_count)
print(r.schema_summary)
