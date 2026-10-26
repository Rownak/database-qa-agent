"""Phase 5.3: builds the real bank_sample bundle from the public snapshot and prints what is in it (embeds ~2.5k chunks: needs the API key)."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from evals.dataset import load_dataset
from evals.lakequest import prepare

out = Path("../benchmark/lakequest_prepared/bank_sample")
prepare(
    Path("../benchmark/LakeQuest/public_data/public_lake_data.zip"), None, None,
    "bank", Path("configs/eval/lakequest_bank.yaml"), out,
)
with closing(sqlite3.connect(out / "bank.db")) as conn:
    print("tables:", [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
with closing(sqlite3.connect(out / "docs.db")) as conn:
    print("chunks:", conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
cases = load_dataset(out / "dataset.yaml").cases
print("cases per type:", {t: sum(t in c.tags for c in cases) for t in ("hybrid", "table", "document")})
for kind in ("hybrid", "table", "document"):
    case = next(c for c in cases if kind in c.tags)
    print(f"\n[{kind}] {case.id}: {case.question}")
    print(json.dumps(case.expected.model_dump(mode="json", exclude_defaults=True), indent=2, ensure_ascii=False)[:700])
