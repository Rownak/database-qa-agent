"""Phase 5.2: prepares the real Open Dev bank bundle and prints what is in it (embeds ~2k chunks: needs the API key)."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from evals.dataset import load_dataset
from evals.lakequest import prepare

bench = Path("../benchmark/LakeQuest")
out = Path("../benchmark/lakequest_prepared/bank_dev")
if not (bench / "open_dev/input").exists():
    raise SystemExit(f"{bench / 'open_dev'} is missing: download Open Dev from Codabench first (see sprint5.md, Phase 5.1)")

prepare(
    bench / "public_data/public_lake_data.zip", bench / "open_dev/input", bench / "open_dev/reference",
    "bank", Path("configs/eval/lakequest_bank_dev.yaml"), out,
)
with closing(sqlite3.connect(out / "bank.db")) as conn:
    print("tables:", [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
with closing(sqlite3.connect(out / "docs.db")) as conn:
    print("chunks:", conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
print("cases:", len(load_dataset(out / "dataset.yaml").cases))
print("docs_summary (first 5 lines):")
print("\n".join(json.loads((out / "state.json").read_text(encoding="utf-8"))["docs_summary"].splitlines()[:5]))
