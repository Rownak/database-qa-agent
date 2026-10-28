import json
import re
import sqlite3
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np
import yaml

from data_agent import retrieval
from data_agent.config import RetrievalConfig

# usage (from db-doc-qa/): uv run python -m dev_test.phase_8_4 [run_dir]   (default: the newest *_lat_lakequest)
EXPECTED = {"table": "database", "document": "document", "hybrid": "hybrid"}  # case tag -> expected intent; scoring only
INTENTS = list(dict.fromkeys(EXPECTED.values()))
INTENT_OFF_RUN = Path("runs/20261026-133728_lakequest_bank_sample")  # the earlier run without the classifier

med = lambda v: st.median(v) if v else float("nan")
stage = lambda r, name: sum(s["s"] for s in r["timing"]["stages"] if s["stage"] == name)

run = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(Path("runs").glob("*_lat_lakequest"))[-1]
rows = [json.loads(line) for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines()]
cfg = yaml.safe_load((run / "config.yaml").read_text(encoding="utf-8"))
metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
state_file = Path(cfg["dataset"]["path"]).parent / "state.json"
state = json.loads(state_file.read_text(encoding="utf-8"))
bank_db, docs_db = (state_file.parent / state[k] for k in ("db_path", "docs_db_path"))
ok = [r for r in rows if not r["error"]]
print(f"run {run.name}: {len(rows)} turns, {len(rows) - len(ok)} errors, timing.ok on {sum(r['timing']['ok'] for r in ok)}/{len(ok)}\n")

# 1. route summary (seconds)
print("1. Route summary (seconds; n = turns)")
print(f"{'Route':<10}{'n':>4}{'p50':>7}{'max':>7}{'p50 w/o intent':>16}{'avg tools':>11}{'llm p50':>9}{'tool p50':>10}{'intent p50':>12}")
for route in ("hybrid", "table", "document", "all"):
    sel = [r for r in ok if route == "all" or route in r["tags"]]
    if not sel:  # a partial run (for example dataset.limit) may lack a route
        continue
    print(f"{route:<10}{len(sel):>4}{med([r['latency_s'] for r in sel]):>7.2f}{max(r['latency_s'] for r in sel):>7.2f}"
          f"{med([r['latency_s'] - stage(r, 'intent') for r in sel]):>16.2f}{st.mean(len(r['calls']) for r in sel):>11.2f}"
          f"{med([stage(r, 'llm') for r in sel]):>9.2f}{med([stage(r, 'tool') for r in sel]):>10.2f}{med([stage(r, 'intent') for r in sel]):>12.2f}")
intents = [stage(r, "intent") for r in ok]
print(f"intent stage: p50 {med(intents):.2f}s  max {max(intents):.2f}s  share of a turn (median) {med([stage(r, 'intent') / r['latency_s'] for r in ok]):.0%}")

# 2. routing accuracy: the case tag is used here only, to score
print("\n2. Routing: case tag (rows) x predicted intent (columns), counts")
conf = Counter((tag, r["intent"]) for r in ok for tag in EXPECTED if tag in r["tags"])
print(f"{'tag':<10}" + "".join(f"{i:>10}" for i in INTENTS) + f"{'n':>6}{'correct':>9}")
for tag, want in EXPECTED.items():
    n = sum(conf[(tag, i)] for i in INTENTS)
    if not n:
        continue
    print(f"{tag:<10}" + "".join(f"{conf[(tag, i)]:>10}" for i in INTENTS) + f"{n:>6}{conf[(tag, want)] / n:>9.0%}")
print(f"overall routing accuracy {sum(conf[(t, w)] for t, w in EXPECTED.items()) / sum(conf.values()):.1%}")
earlier = json.loads((INTENT_OFF_RUN / "metrics.json").read_text())["metrics"]["tool_sequence_match"] if INTENT_OFF_RUN.is_dir() else None
print(f"tool_sequence_match: this run {metrics['metrics']['tool_sequence_match']:.3f} | intent-off run {earlier if earlier is None else round(earlier, 3)}")

# 3. dense retrieval, offline: replay each recorded search with a fixed random vector (the scan cost ignores its values)
rcfg = RetrievalConfig(**cfg["agent"]["retrieval"])
with closing(sqlite3.connect(f"{docs_db.resolve().as_uri()}?mode=ro", uri=True)) as c:
    total = c.execute("SELECT count(*) FROM chunks").fetchone()[0]
    dim = len(c.execute("SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()[0]) // 4
vec = np.random.default_rng(0).standard_normal(dim).astype(np.float32)
vec /= np.linalg.norm(vec)


def timed(fn, n=3):
    ms = []
    for _ in range(n):
        t = time.perf_counter()  # not monotonic(): on Windows that ticks only every ~15.6 ms
        out = fn()
        ms.append((time.perf_counter() - t) * 1000)
    return med(ms), out


def traced_single(r, name):  # {call index: traced tool seconds} for tool batches that held exactly one call
    out, i = {}, 0
    for s in r["timing"]["stages"]:
        if s["stage"] == "tool":
            if len(s["names"]) == 1 and s["names"][0] == name:
                out[i] = s["s"]
            i += len(s["names"])
    return out


band = lambda n: "<= 50" if n <= 50 else "51-500" if n <= 500 else "> 500"
groups = defaultdict(list)  # band -> [(hybrid ms, bm25-only ms, traced tool ms or None)]
scanned, empty = [], 0
for r in ok:
    tool_s = traced_single(r, "search_documents")
    for i, call in enumerate(r["calls"]):
        if call["name"] != "search_documents":
            continue
        a = call["args"]
        if not str(a.get("query") or "").strip():  # an empty call is rejected by the tool: nothing to replay
            empty += 1
            continue
        k = max(1, min(int(a.get("top_k", 5)), rcfg.max_top_k))
        args = (docs_db, a["query"], a.get("source") or None, a.get("section") or None, k)
        full, res = timed(lambda: retrieval.search(*args, vec, rcfg))
        keyword, _ = timed(lambda: retrieval.search(*args, None, rcfg.model_copy(update={"mode": "bm25"})))
        scanned.append(res.candidates)
        groups[band(res.candidates)].append((full, keyword, tool_s[i] * 1000 if i in tool_s else None))
print(f"\n3. Dense retrieval (milliseconds; {len(scanned)} replayed search_documents calls, {empty} empty calls skipped; "
      f"docs.db has {total} chunks, {dim}-dim vectors)")
print(f"{'Chunks scanned':<16}{'calls':>6}{'search med':>12}{'search max':>12}{'dense part med':>16}{'traced tool med':>17}{'search share':>14}")
for name in ("<= 50", "51-500", "> 500"):
    g = groups.get(name, [])
    if not g:
        continue
    tool = [t for _, _, t in g if t is not None]
    share = med([f for f, _, t in g if t is not None]) / med(tool) if tool else float("nan")
    print(f"{name:<16}{len(g):>6}{med([f for f, _, _ in g]):>12.1f}{max(f for f, _, _ in g):>12.1f}"
          f"{med([f - k for f, k, _ in g]):>16.1f}{med(tool):>17.1f}{share:>14.1%}")
print(f"calls scanning all {total} chunks: {sum(n == total for n in scanned)} of {len(scanned)}; "
      f"filtered by source/section: {sum(n < total for n in scanned)}; matching no chunk: {sum(n == 0 for n in scanned)}")

# 4. SQL execution, offline: re-run each recorded query read-only on bank.db
uri = f"{bank_db.resolve().as_uri()}?mode=ro"
with closing(sqlite3.connect(uri, uri=True)) as c:
    size = {t: (c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0], len(c.execute(f'PRAGMA table_info("{t}")').fetchall()))
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def run_query(sql):
    with closing(sqlite3.connect(uri, uri=True)) as conn:  # a new connection per call, like the tool
        conn.execute(sql).fetchmany(101)


sql_groups, failed = defaultdict(list), 0
for r in ok:
    tool_s = traced_single(r, "run_sql")
    for i, call in enumerate(r["calls"]):
        if call["name"] != "run_sql":
            continue
        sql = str(call["args"].get("query") or "").strip().rstrip(";")
        if not sql:
            failed += 1
            continue
        tables = [t for t in re.findall(r'\b(?:FROM|JOIN)\s+"?([A-Za-z_]\w*)"?', sql, re.I) if t in size]
        kind = "aggregate" if re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(|\bGROUP\s+BY\b", sql, re.I) else "join" if re.search(r"\bJOIN\b", sql, re.I) else "lookup"
        try:
            ms, _ = timed(lambda: run_query(sql))
        except sqlite3.Error:
            failed += 1
            continue
        big = max(tables, key=lambda t: size[t][0]) if tables else "(none)"
        label = f"{big} ({size[big][0]:,} x {size[big][1]})" if tables else big
        sql_groups[(label, kind)].append((ms, tool_s[i] * 1000 if i in tool_s else None))
print(f"\n4. SQL execution (milliseconds; {sum(map(len, sql_groups.values()))} recorded run_sql calls, {failed} not re-runnable)")
print(f"{'Largest table (rows x cols)':<34}{'class':<11}{'calls':>6}{'SQL med':>9}{'SQL max':>9}{'traced tool med':>17}")
small = [v for v in sql_groups.values() if len(v) < 3]
rows_out = [(k[0], k[1], v) for k, v in sorted(sql_groups.items(), key=lambda kv: -len(kv[1])) if len(v) >= 3]
rows_out.append(("merged groups with < 3 calls", "mixed", [x for v in small for x in v]))
for label, kind, v in rows_out:
    if v:
        tool = [t for _, t in v if t is not None]
        print(f"{label:<34}{kind:<11}{len(v):>6}{med([m for m, _ in v]):>9.2f}{max(m for m, _ in v):>9.2f}{med(tool):>17.2f}")
print("table sizes (rows x cols): " + ", ".join(f"{t} {r:,}x{c}" for t, (r, c) in sorted(size.items(), key=lambda kv: -kv[1][0])))
