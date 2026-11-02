"""Builds the response-cache workload from the LakeQuest bank sample (Sprint 9, Phase 9.2).

Run once from ``db-doc-qa/``: ``uv run python -m dev_test.phase_9_2_build [--regen]``.

Writes ``benchmark/lakequest_prepared/bank_sample_cache/`` (git-ignored with the rest of ``benchmark/``):
``dataset.yaml`` with five passes in run order (U unique, E exact, P paraphrase, N near miss, S stale),
a ``state.json`` that points at the original bank bundle, ``paraphrases.json`` (generated once, then
frozen: delete it or pass ``--regen`` to generate again), and two mutated bundles for the stale probes.
The original bundle is never modified.
"""

import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import yaml

from data_agent import cache

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT.parent / "benchmark" / "lakequest_prepared" / "bank_sample"
OUT = ROOT.parent / "benchmark" / "lakequest_prepared" / "bank_sample_cache"
MODEL = "gemini-3.5-flash-lite"

PROMPT = """\
Rewrite the question below so that it asks exactly the same thing in different words and with a different \
sentence structure. Keep every person name, client id, number, amount, policy name and quoted phrase exactly \
as written. Do not add or drop any information and do not answer it. Output only the rewritten question.

Question:
{question}
"""

_CLIENT_RE = re.compile(r"([A-Z][\w'’-]+ [A-Z][\w'’-]+) \(client_id: (\d+)\)")  # "First Last (client_id: N)"
_POLICY_RE = re.compile(r"\b[a-z]+(?:-[a-z]+)+\b")
_STRICT_PREFIXES = ("n:", "i:", "f:", "q:")  # numbers, ids, file names, quoted strings


def half(cases: list[dict], which: int) -> list[dict]:
    """Splits the cases into two disjoint halves per route (hybrid, table, document): every other case."""
    routes: dict[str, list[dict]] = {}
    for c in cases:
        routes.setdefault(next(t for t in c["tags"] if t in ("hybrid", "table", "document")), []).append(c)
    return [c for group in routes.values() for i, c in enumerate(group) if i % 2 == which]


def check_paraphrase(original: str, text: str) -> list[str]:
    """Returns what a paraphrase changed that it must not: ids, numbers, quoted text, names, policy ids."""
    problems = []
    a, b = cache.entity_tokens(original), cache.entity_tokens(text)
    for token in sorted(t for t in a if t.startswith(_STRICT_PREFIXES) and t not in b):
        problems.append(f"missing {token}")
    for token in sorted(t for t in b if t.startswith(_STRICT_PREFIXES) and t not in a):
        problems.append(f"added {token}")
    for name, _ in _CLIENT_RE.findall(original):
        if name not in text:
            problems.append(f"missing name {name}")
    for policy in _POLICY_RE.findall(original):
        if policy not in text:
            problems.append(f"missing policy {policy}")
    return problems


def write_paraphrases(cases: list[dict], path: Path) -> dict[str, str]:
    """Generates one paraphrase per case with Gemini (retrying until the identifiers match) and freezes them."""
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types

    load_dotenv()
    client = genai.Client()
    out: dict[str, str] = {}
    for c in cases:
        for attempt in range(4):
            reply = client.models.generate_content(
                model=MODEL,
                contents=PROMPT.format(question=c["question"]),
                config=types.GenerateContentConfig(temperature=0.7),
            )
            text = " ".join((reply.text or "").split())
            problems = check_paraphrase(c["question"], text)
            if text and not problems:
                out[c["id"]] = text
                break
            print(f"  {c['id']} attempt {attempt + 1} rejected: {problems or 'empty'}")
        else:
            raise SystemExit(f"no valid paraphrase for {c['id']}")
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def swap_client(question: str, others: list[tuple[int, str]]) -> str | None:
    """Replaces the client name and id of a question with another client's; ``None`` if it names none."""
    m = _CLIENT_RE.search(question)
    if not m:
        return None
    cid, name = others.pop(0)
    return question[: m.start()] + f"{name} (client_id: {cid})" + question[m.end():]


def make_near_misses(cases: list[dict], con: sqlite3.Connection, policies: list[str]) -> list[dict]:
    """Builds 10 near-miss questions: 4 hybrid and 3 table with another client, 3 document with another policy."""
    used = {int(m.group(2)) for c in cases for m in [_CLIENT_RE.search(c["question"])] if m}
    rows = con.execute(
        'SELECT c.client_id, c.first_name || " " || c.last_name FROM client c '
        "WHERE c.client_id IN (SELECT client_id FROM bank_account) ORDER BY c.client_id"
    ).fetchall()
    others = [(cid, name) for cid, name in rows if cid not in used][::37]
    cited = {p for c in cases for p in _POLICY_RE.findall(c["question"])}
    free_policies = [p for p in policies if p not in cited]
    out = []
    plan = [("hybrid", 4), ("table", 3), ("document", 3)]
    for route, n in plan:
        pool = [c for c in cases if route in c["tags"]][1::2][:n] if route != "document" else [
            c for c in cases if route in c["tags"] and _POLICY_RE.search(c["question"])
        ][1::3][:n]
        for c in pool:
            if route == "document":
                old = _POLICY_RE.search(c["question"]).group(0)
                text = c["question"].replace(old, free_policies.pop(0), 1)
            else:
                text = swap_client(c["question"], others)
            assert text and text != c["question"], c["id"]
            out.append({"id": f"{c['id']}-near", "question": text, "tags": [*c["tags"], "pass_near_miss"],
                        "expected": {}})
    assert len(out) == 10, len(out)
    return out


def mutate_data(dst: Path) -> dict[str, int]:
    """Copies ``bank.db`` and changes two counts; returns the new truths ``{client_id: count}``."""
    shutil.copy(BASE / "bank.db", dst)
    with closing(sqlite3.connect(dst)) as con:
        con.execute(
            'DELETE FROM "transaction" WHERE transaction_id IN (SELECT t.transaction_id FROM "transaction" t '
            "JOIN bank_account a ON a.account_id = t.account_id WHERE a.client_id = 62 ORDER BY t.transaction_id LIMIT 10)"
        )
        con.execute(
            "DELETE FROM bank_account WHERE account_id = (SELECT MAX(account_id) FROM bank_account WHERE client_id = 552)"
        )
        # the client table keeps a precomputed count that the agent may read instead of counting rows
        con.execute(
            "UPDATE client SET n_transactions = (SELECT COUNT(*) FROM \"transaction\" t JOIN bank_account a "
            "ON a.account_id = t.account_id WHERE a.client_id = 62) WHERE client_id = 62"
        )
        con.commit()
        tx = con.execute(
            'SELECT COUNT(*) FROM "transaction" t JOIN bank_account a ON a.account_id = t.account_id WHERE a.client_id = 62'
        ).fetchone()[0]
        acc = con.execute("SELECT COUNT(*) FROM bank_account WHERE client_id = 552").fetchone()[0]
    return {"62": tx, "552": acc}


def mutate_docs(dst: Path, sources: list[str]) -> None:
    """Copies ``docs.db`` and removes whole policies (documents, chunks and their search index rows)."""
    shutil.copy(BASE / "docs.db", dst)
    with closing(sqlite3.connect(dst)) as con:
        for source in sources:
            con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT chunk_id FROM chunks WHERE source = ?)", (source,))
            con.execute("DELETE FROM chunks WHERE source = ?", (source,))
            con.execute("DELETE FROM documents WHERE source = ?", (source,))
        con.commit()
        left = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"  docs.db without {sources}: {left} chunks left")


def main() -> None:
    """Writes the derived dataset and the mutated bundles."""
    spec = yaml.safe_load((BASE / "dataset.yaml").read_text(encoding="utf-8"))
    cases = spec["cases"]
    by_id = {c["id"]: c for c in cases}
    OUT.mkdir(exist_ok=True)
    state = json.loads((BASE / "state.json").read_text(encoding="utf-8"))
    policies = [line.split(" — ")[0] .removesuffix(".md") for line in state["docs_summary"].splitlines()]

    half_a, half_b = half(cases, 0), half(cases, 1)
    para_path = OUT / "paraphrases.json"
    if "--regen" in sys.argv or not para_path.is_file():
        print("generating paraphrases ...")
        paras = write_paraphrases(half_b, para_path)
    else:
        paras = json.loads(para_path.read_text(encoding="utf-8"))
    for c in half_b:
        assert not check_paraphrase(c["question"], paras[c["id"]]), c["id"]

    with closing(sqlite3.connect(f"file:{BASE / 'bank.db'}?mode=ro", uri=True)) as con:
        near = make_near_misses(cases, con, policies)

    # stale probes: mutated copies; the questions are verbatim originals, the expected answers the new truths
    (OUT / "stale_data").mkdir(exist_ok=True)
    (OUT / "stale_docs").mkdir(exist_ok=True)
    truth = mutate_data(OUT / "stale_data" / "bank.db")
    print("  stale data truths:", truth)
    gone = ["bankruptcy-notice-servicing.md", "records-access-privacy-expedite.md"]
    mutate_docs(OUT / "stale_docs" / "docs.db", gone)
    stale_data = {**state, "db_path": "bank.db", "docs_db_path": "../../bank_sample/docs.db"}
    kept = [line for line in state["docs_summary"].splitlines() if line.split(" — ")[0] not in gone]
    stale_docs = {**state, "db_path": "../../bank_sample/bank.db", "docs_db_path": "docs.db", "docs_summary": "\n".join(kept)}
    (OUT / "stale_data" / "state.json").write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
    (OUT / "stale_docs" / "state.json").write_text(json.dumps(stale_docs, indent=2), encoding="utf-8")
    (OUT / "state.json").write_text(
        json.dumps({**state, "db_path": "../bank_sample/bank.db", "docs_db_path": "../bank_sample/docs.db"}, indent=2),
        encoding="utf-8",
    )
    shutil.copy(BASE / "prepare.json", OUT / "prepare.json")  # the settings guard looks for it here

    stale = []
    for cid, key in (("tbl-1", "62"), ("tbl-3", "552")):
        orig = by_id[cid]
        assert str(orig["expected"]["answer_contains"][0]) != str(truth[key]), cid
        stale.append({
            "id": f"{cid}-stale", "question": orig["question"], "tags": [*orig["tags"], "pass_stale"],
            "state_file": "stale_data/state.json",
            "expected": {"tools": ["run_sql"], "answer_contains": [str(truth[key])], "reference_answers": [str(truth[key])]},
        })
    for cid in ("doc-4", "doc-7"):
        orig = by_id[cid]
        stale.append({
            "id": f"{cid}-stale", "question": orig["question"], "tags": [*orig["tags"], "pass_stale"],
            "state_file": "stale_docs/state.json", "expected": {"tools": ["search_documents"]},
        })

    def tagged(c: dict, suffix: str, tag: str, question: str | None = None) -> dict:
        return {**c, "id": f"{c['id']}-{suffix}", "tags": [*c["tags"], tag], "question": question or c["question"]}

    workload = (
        [{**c, "tags": [*c["tags"], "pass_unique"]} for c in cases]
        + [tagged(c, "exact", "pass_exact") for c in half_a]
        + [tagged(c, "para", "pass_paraphrase", paras[c["id"]]) for c in half_b]
        + near
        + stale
    )
    out = {**spec, "name": "lakequest_bank_sample_cache", "state_file": "state.json", "cases": workload}
    (OUT / "dataset.yaml").write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    counts = {t: sum(t in c["tags"] for c in workload) for t in
              ("pass_unique", "pass_exact", "pass_paraphrase", "pass_near_miss", "pass_stale")}
    print("written", OUT / "dataset.yaml", counts, "total", len(workload))
    print("\nparaphrases to read by hand:")
    for c in half_b:
        print(f"- {c['id']}\n    orig: {' '.join(c['question'].split())[:230]}\n    para: {paras[c['id']][:230]}")
    print("\nnear misses:")
    for c in near:
        print(f"- {c['id']}: {' '.join(c['question'].split())[:200]}")


if __name__ == "__main__":
    main()
