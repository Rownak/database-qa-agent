"""LakeQuest adapter: turns the benchmark files into a self-contained bundle (LakeQuest design section 4).

``uv run python -m evals.lakequest prepare ...`` (from ``db-doc-qa/``) reads the public lake
zip plus the question (and optionally reference) files of one split and writes::

    bank.db        only the tables listed in corpus_objects.parquet (never the benchmark build tables)
    docs.db        the policy documents, chunked and embedded once with doc_ingest.build
    state.json     the prebuilt session state (paths relative to the file)
    dataset.yaml   an EvalDataset with one case per question of the domain
    prepare.json   source hashes, counts and the chunking / embedding settings used

Without ``--questions`` it builds ``bank_sample`` instead (internal evaluation, 40 cases made from the
public snapshot alone; see :func:`build_sample_cases`) and also writes ``reference/references.jsonl``
in the scorer's format.

The agent only ever receives this bundle, so the answer tables that ship inside the lake
database (``persona_workflows`` and others) can never reach it. The module uses the existing
ingestion and evaluation code and changes none of it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import random
import re
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent import doc_ingest
from data_agent.config import load_config
from data_agent.logs import get_logger
from evals.dataset import EvalCase, EvalDataset, Expected, ExpectedSection

log = get_logger(__name__)

QUESTION_COLUMNS = ("qa_id", "question", "domain")
REFERENCE_COLUMNS = ("qa_id", "answer_short", "answer_long", "gold_object_ids")

SAMPLE_SEED = 20260101
SAMPLE_COUNTS = {"hybrid": 20, "table": 10, "document": 10}
_SECTION_REF_RE = re.compile(r"\bSections?\s+(\d+(?:\s*(?:,|and|&|/|-)\s*\d+)*)", re.IGNORECASE)
_MD_SECTION_RE = re.compile(r"^## (\d+) (.+?)\s*$", re.MULTILINE)
_TABLE_QUESTIONS = {  # kind -> (question template, tables used by the answer's SQL)
    "accounts": ("How many accounts does {first} {last} (client_id: {id}) have?", ["client", "bank_account"]),
    "transactions": ("How many transactions does {first} {last} (client_id: {id}) have?", ["client", "bank_account", "transaction"]),
    "cheques": ("How many cheques does {first} {last} (client_id: {id}) have?", ["client", "bank_account", "cheque"]),
}
_TABLE_COUNT_SQL = {
    "accounts": 'SELECT COUNT(*) FROM "bank_account" WHERE "client_id" = ?',
    "transactions": 'SELECT COUNT(*) FROM "transaction" t JOIN "bank_account" a ON a."account_id" = t."account_id" WHERE a."client_id" = ?',
    "cheques": 'SELECT COUNT(*) FROM "cheque" c JOIN "bank_account" a ON a."account_id" = c."account_id" WHERE a."client_id" = ?',
}

# Policies use "## 5) Purpose"; the chunker only numbers headings like "## 5 Purpose".
_HEADING_RE = re.compile(r"^(#{1,6}[ \t]+\d+(?:\.\d+)*)\)[ \t]+", re.MULTILINE)


class PrepareError(Exception):
    """A problem with the input files that the user can fix (missing table, column or file)."""


def sha256(path: Path) -> str:
    """Returns the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalise_policy(title: str, document: str) -> str:
    """Returns a policy as Markdown the chunker can number: ``# title`` first, ``## N) `` as ``## N ``."""
    return f"# {title.strip()}\n\n{_HEADING_RE.sub(r'\1 ', document.strip())}\n"


def _find_file(location: Path, stem: str) -> Path:
    """Resolves ``location`` (a file, or a folder holding ``<stem>.parquet`` / ``<stem>.jsonl``) to a file."""
    if location.is_file():
        return location
    for suffix in (".parquet", ".jsonl"):
        candidate = location / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    raise PrepareError(f"no {stem}.parquet or {stem}.jsonl in {location}")


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_json(path, lines=True, dtype=False)


def _require(df: pd.DataFrame, columns: tuple[str, ...], path: Path) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise PrepareError(f"{path}: missing column(s) {', '.join(missing)}; found {', '.join(map(str, df.columns))}")


def _as_list(value: Any) -> list[str]:
    """Reads a list cell (array, JSON text, comma text or scalar), like the official scorer."""
    if value is None or (isinstance(value, float) and value != value):
        return []
    if hasattr(value, "tolist"):
        return _as_list(value.tolist())
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            return _as_list(json.loads(text))
        except json.JSONDecodeError:
            pass
    if "," in text:
        return [p.strip() for p in text.split(",") if p.strip()]
    return [text] if text else []


def _text(value: Any) -> str:
    return "" if value is None or (isinstance(value, float) and value != value) else str(value).strip()


def read_lake(lake_zip: Path, domain: str, work: Path) -> tuple[pd.DataFrame, list[dict], Path]:
    """Reads one lake out of the public zip.

    Args:
        lake_zip: ``public_lake_data.zip``.
        domain: Lake name, for example ``bank``.
        work: Folder that receives the extracted SQLite file.

    Returns:
        ``(corpus_objects, policy_rows, lake_db_path)``.

    Raises:
        PrepareError: If the zip lacks the domain's corpus list, policies or database.
    """
    base = f"raw_lake_snapshot/lakes/{domain}"
    with zipfile.ZipFile(lake_zip) as z:
        names = set(z.namelist())
        corpus_name = f"lakes/{domain}/corpus_objects.parquet"
        policies_name = f"{base}/policy_documents.jsonl"
        dbs = sorted(n for n in names if n.startswith(f"{base}/structured/") and n.endswith(".db"))
        for needed, found in ((corpus_name, corpus_name in names), (policies_name, policies_name in names), (f"{base}/structured/*.db", bool(dbs))):
            if not found:
                raise PrepareError(f"{lake_zip}: {needed} not found")
        corpus = pd.read_parquet(io.BytesIO(z.read(corpus_name)))
        rows = [json.loads(line) for line in z.read(policies_name).decode("utf-8").splitlines() if line.strip()]
        z.extract(dbs[0], work)
    _require(corpus, ("object_id", "object_type"), Path(corpus_name))
    return corpus, rows, work / dbs[0]


def build_bank_db(lake_db: Path, tables: list[str], out: Path) -> dict[str, int]:
    """Copies only ``tables`` from the lake database into a new ``out`` file.

    The list comes from ``corpus_objects.parquet`` (a whitelist taken from the data), so the
    benchmark build tables are left behind. Indexes on the copied tables are kept, and every ``*_id``
    column gets one, because the agent's queries have a 5-second limit.

    Args:
        lake_db: The extracted ``financial_data_lake.db``.
        tables: Table names to copy.
        out: Target file; replaced if it exists.

    Returns:
        Row count per copied table.

    Raises:
        PrepareError: If a whitelisted table is missing from the lake database.
    """
    out.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(out)) as conn:
        conn.execute("ATTACH DATABASE ? AS lake", (str(lake_db.resolve()),))
        present = {r[0] for r in conn.execute("SELECT name FROM lake.sqlite_master WHERE type = 'table'")}
        missing = [t for t in tables if t not in present]
        if missing:
            raise PrepareError(f"{lake_db.name}: whitelisted table(s) missing: {', '.join(missing)}")
        for table in tables:
            conn.execute(f"CREATE TABLE main.{_quote(table)} AS SELECT * FROM lake.{_quote(table)}")
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM main.{_quote(table)}").fetchone()[0]
        indexes = conn.execute(
            "SELECT sql FROM lake.sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND tbl_name IN (%s)"
            % ",".join("?" * len(tables)),
            tables,
        ).fetchall()
        for (sql,) in indexes:
            try:
                conn.execute(sql)  # CREATE INDEX ... ON "table"(...) resolves in main, where the copy lives
            except sqlite3.Error as exc:
                log.warning("index not copied: %s", exc)
        # The lake ships without indexes, but run_sql aborts a query after 5 seconds, so a join on a
        # 125k-row table must not scan. The "_id" columns are the join keys of the questions.
        for table in tables:
            for _, column, *_ in conn.execute(f"PRAGMA main.table_info({_quote(table)})").fetchall():
                if column.lower().endswith("_id"):
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {_quote(f'idx_{table}_{column}')} ON {_quote(table)}({_quote(column)})"
                    )
        conn.commit()
        conn.execute("DETACH DATABASE lake")
        conn.execute("VACUUM")
    return counts


def schema_summary(db: Path) -> str:
    """One line per table: ``name (N rows): column type, column type, ...``."""
    lines = []
    with closing(sqlite3.connect(db)) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            columns = ", ".join(f"{r[1]} {r[2] or 'TEXT'}" for r in conn.execute(f"PRAGMA table_info({_quote(table)})"))
            rows = conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            lines.append(f'"{table}" ({rows} rows): {columns}')
    return "\n".join(lines)


def build_cases(
    questions: pd.DataFrame, references: pd.DataFrame | None, domain: str
) -> list[EvalCase]:
    """Builds one case per question of ``domain``.

    Args:
        questions: Table with ``qa_id``, ``question`` and ``domain``.
        references: Table with ``qa_id``, ``answer_short``, ``answer_long`` and
            ``gold_object_ids``, or ``None`` (Open Test), which leaves every ``expected`` empty.
        domain: Only questions of this domain are kept.

    Returns:
        The cases, in question order. A question without a reference row has an empty ``expected``.
    """
    by_id = {}
    if references is not None:
        by_id = {str(r["qa_id"]).strip(): r for r in references.to_dict("records")}
    cases = []
    for row in questions[questions["domain"].astype(str).str.strip() == domain].to_dict("records"):
        qa_id = str(row["qa_id"]).strip()
        expected = Expected()
        ref = by_id.get(qa_id)
        if ref is not None:
            answers = [a for a in (_text(ref["answer_short"]), _text(ref["answer_long"])) if a]
            expected = Expected(reference_answers=answers, object_ids=_as_list(ref["gold_object_ids"]))
        cases.append(EvalCase(id=qa_id, question=_text(row["question"]), tags=[domain], expected=expected))
    return cases


def read_validation_clients(lake_zip: Path, domain: str) -> set[int]:
    """Client ids that ``split_entities.parquet`` assigns to the validation split."""
    name = f"lakes/{domain}/split_entities.parquet"
    with zipfile.ZipFile(lake_zip) as z:
        if name not in z.namelist():
            raise PrepareError(f"{lake_zip}: {name} not found")
        split = pd.read_parquet(io.BytesIO(z.read(name)))
    _require(split, ("entity_id", "split"), Path(name))
    ids = split.loc[split["split"] == "validation", "entity_id"].astype(str)
    return {int(i.split(":", 1)[1]) for i in ids if i.startswith("client:") and i.split(":", 1)[1].isdigit()}


def policy_sections(markdown: str) -> dict[str, tuple[str, str]]:
    """Top-level sections of a normalised policy: number -> (title, first paragraph)."""
    matches = list(_MD_SECTION_RE.finditer(markdown))
    out = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[m.end():end].strip()
        out[m.group(1)] = (m.group(2).strip(), " ".join(body.split("\n\n")[0].split()))
    return out


def referenced_sections(text: str, available: dict) -> list[str]:
    """Section numbers named as "Section N" / "Sections N and M" in ``text`` that exist in the policy, in order."""
    found: list[str] = []
    for m in _SECTION_REF_RE.finditer(text):
        for number in re.findall(r"\d+", m.group(1)):
            if number in available and number not in found:
                found.append(number)
    return found


def build_sample_cases(
    lake_db: Path,
    bank_db: Path,
    policies: dict[str, str],
    validation_clients: set[int],
    seed: int = SAMPLE_SEED,
    counts: dict[str, int] | None = None,
) -> list[EvalCase]:
    """Generates the ``bank_sample`` cases: ``hybrid``, then ``table``, then ``document``.

    The generator is the only reader of ``persona_workflows`` (in ``lake_db``); the agent's
    ``bank.db`` never contains it. Only validation clients are used and the order is fixed
    by ``seed``, so two runs give the same cases.

    Args:
        lake_db: The extracted lake database (has ``persona_workflows``).
        bank_db: The whitelisted copy, used to compute the exact table answers.
        policies: Policy id -> normalised Markdown (as written to ``docs.db``).
        validation_clients: Client ids of the validation split.
        seed: Seed of the random choices.
        counts: Cases per type; :data:`SAMPLE_COUNTS` when ``None``.

    Returns:
        The cases. The ``table`` and ``document`` types may be shorter than asked when the
        data has no suitable client or policy; a warning is logged.

    Raises:
        PrepareError: If there is no hybrid question of a validation client.
    """
    counts = counts or SAMPLE_COUNTS
    rng = random.Random(seed)
    with closing(sqlite3.connect(f"file:{lake_db.resolve().as_posix()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT qa_id, client_id, policy_id, question, answer_direct, answer_reasoning, sources "
            "FROM persona_workflows ORDER BY qa_id"
        ).fetchall()
    rows = [r for r in rows if r[1] in validation_clients and r[2] in policies][: counts["hybrid"]]
    if not rows:
        raise PrepareError("persona_workflows has no question of a validation client")

    cases: list[EvalCase] = []
    for qa_id, _, policy_id, question, direct, reasoning, sources in rows:
        sections = referenced_sections(reasoning or "", policy_sections(policies[policy_id]))
        cases.append(
            EvalCase(
                id=qa_id,
                question=question,
                tags=["bank", "hybrid"],
                expected=Expected(
                    tools=["run_sql", "search_documents"],
                    sections=[ExpectedSection(source=f"{policy_id}.md", section=n) for n in sections],
                    reference_answers=[a for a in (_text(direct), _text(reasoning)) if a],
                    object_ids=[*_as_list(sources), policy_id],
                ),
            )
        )

    client_ids = list(dict.fromkeys(r[1] for r in rows))
    cases += _table_cases(bank_db, client_ids, counts["table"], rng)
    policy_ids = sorted(dict.fromkeys(r[2] for r in rows))
    cases += _document_cases(policy_ids, policies, counts["document"], rng)
    return cases


def _table_cases(bank_db: Path, client_ids: list[int], wanted: int, rng: random.Random) -> list[EvalCase]:
    """Count questions about the given clients; the answer is computed with SQL on ``bank.db``."""
    kinds = list(_TABLE_QUESTIONS)
    pool = [(cid, kind) for cid in client_ids for kind in kinds]
    rng.shuffle(pool)
    cases: list[EvalCase] = []
    with closing(sqlite3.connect(f"file:{bank_db.resolve().as_posix()}?mode=ro", uri=True)) as conn:
        for cid, kind in pool:
            if len(cases) == wanted:
                break
            name = conn.execute('SELECT "first_name", "last_name" FROM "client" WHERE "client_id" = ?', (cid,)).fetchone()
            count = conn.execute(_TABLE_COUNT_SQL[kind], (cid,)).fetchone()[0]
            if name is None or count == 0:  # a zero answer says nothing about the agent's lookup
                continue
            template, tables = _TABLE_QUESTIONS[kind]
            cases.append(
                EvalCase(
                    id=f"tbl-{len(cases) + 1}",
                    question=template.format(first=name[0], last=name[1], id=cid),
                    tags=["bank", "table"],
                    expected=Expected(
                        tools=["run_sql"], answer_contains=[str(count)], reference_answers=[str(count)], object_ids=tables
                    ),
                )
            )
    if len(cases) < wanted:
        log.warning("only %d of %d table cases could be built", len(cases), wanted)
    return cases


def _document_cases(policy_ids: list[str], policies: dict[str, str], wanted: int, rng: random.Random) -> list[EvalCase]:
    """Section-lookup and purpose questions about policies; half of each when ``wanted`` is even."""
    order = policy_ids[:]
    rng.shuffle(order)
    n_section = wanted // 2
    used: set[tuple[str, str]] = set()  # (kind, policy): ask about each policy once per question type
    cases: list[EvalCase] = []
    for i in range(wanted):
        kind = "section" if i < n_section else "purpose"
        for step in range(len(order)):  # the first policy (in shuffled order) that can serve this question
            policy_id = order[(i + step) % len(order)]
            if (kind, policy_id) in used:
                continue
            sections = policy_sections(policies[policy_id])
            if kind == "section":
                options = [(n, t) for n, (t, _) in sections.items() if n != "1" and not t.lower().startswith("purpose")]
                if not options:
                    continue
                number, title = rng.choice(options)
                question = f"Under policy {policy_id}, which section covers {title}?"
                expected = Expected(
                    tools=["search_documents"],
                    sections=[ExpectedSection(source=f"{policy_id}.md", section=number)],
                    answer_contains=[number],
                    reference_answers=[number],  # the same value as answer_short in references.jsonl, so answer_f1 matches the official scorer
                    object_ids=[policy_id],
                )
            else:
                purpose = next(((n, p) for n, (t, p) in sections.items() if t.lower().startswith("purpose") and p), None)
                if purpose is None:
                    continue
                question = f"What is the purpose of policy {policy_id}?"
                expected = Expected(
                    tools=["search_documents"],
                    sections=[ExpectedSection(source=f"{policy_id}.md", section=purpose[0])],
                    reference_answers=[purpose[1]],
                    object_ids=[policy_id],
                )
            used.add((kind, policy_id))
            cases.append(EvalCase(id=f"doc-{len(cases) + 1}", question=question, tags=["bank", "document"], expected=expected))
            break
    if len(cases) < wanted:
        log.warning("only %d of %d document cases could be built", len(cases), wanted)
    return cases


def reference_rows(cases: list[EvalCase], domain: str) -> list[dict]:
    """The cases in the official scorer's reference format (``answer_short`` / ``answer_long``)."""
    rows = []
    for c in cases:
        answers = c.expected.reference_answers or c.expected.answer_contains
        rows.append(
            {
                "qa_id": c.id,
                "domain": domain,
                "question": c.question,
                "answer_short": answers[0] if answers else "",
                "answer_long": answers[1] if len(answers) > 1 else "",
                "gold_object_ids": c.expected.object_ids,
                "gold_provenance_ids": [],
            }
        )
    return rows


def prepare(
    lake_zip: Path,
    questions: Path | None,
    references: Path | None,
    domain: str,
    config: Path,
    out: Path,
    *,
    seed: int = SAMPLE_SEED,
    counts: dict[str, int] | None = None,
) -> dict:
    """Builds the bundle for one domain and split, or the ``bank_sample`` bundle.

    Args:
        lake_zip: ``public_lake_data.zip``.
        questions: A questions file, or a folder holding ``questions.parquet`` / ``.jsonl``.
            ``None`` builds ``bank_sample`` from the snapshot (only for ``bank``).
        references: A references file or folder (Open Dev), or ``None`` (Open Test).
        domain: Lake and question domain, for example ``bank``.
        config: Run YAML; its ``agent.chunking`` and ``agent.embedding`` are used for ``docs.db``.
        out: Output folder; created if missing.
        seed: Seed of the ``bank_sample`` choices (sample mode only).
        counts: ``bank_sample`` cases per type (sample mode only); :data:`SAMPLE_COUNTS` by default.

    Returns:
        The content written to ``prepare.json``.

    Raises:
        PrepareError: For a missing file, table or column.

    Side Effects:
        Writes the files into ``out`` (plus ``reference/references.jsonl`` in sample mode) and
        calls the embedding API once per 100 chunks.
    """
    agent_cfg = load_config(config).agent
    sample = questions is None
    q_path = r_path = q_df = None
    cases: list[EvalCase] = []
    if sample:
        if references:
            raise PrepareError("--references needs --questions")
        if domain != "bank":
            raise PrepareError("bank_sample is only defined for the bank domain")
        validation = read_validation_clients(Path(lake_zip), domain)
    else:
        q_path = _find_file(Path(questions), "questions")
        r_path = _find_file(Path(references), "references") if references else None
        q_df = _read_table(q_path)
        _require(q_df, QUESTION_COLUMNS, q_path)
        r_df = None
        if r_path is not None:
            r_df = _read_table(r_path)
            _require(r_df, REFERENCE_COLUMNS, r_path)
        cases = build_cases(q_df, r_df, domain)
        if not cases:
            raise PrepareError(f"{q_path}: no question with domain {domain!r}")

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lakequest_") as tmp:
        corpus, policy_rows, lake_db = read_lake(Path(lake_zip), domain, Path(tmp))
        tables = sorted(corpus.loc[corpus["object_type"] == "table", "object_id"].astype(str))
        policy_ids = set(corpus.loc[corpus["object_type"] == "policy_document", "object_id"].astype(str))
        table_counts = build_bank_db(lake_db, tables, out / "bank.db")
        selected = [r for r in policy_rows if str(r.get("object_id")) in policy_ids]
        texts = {str(r["object_id"]): normalise_policy(str(r.get("title") or r["object_id"]), str(r["document"])) for r in selected}
        if sample:  # inside the block: the generator reads persona_workflows from the extracted lake database
            cases = build_sample_cases(lake_db, out / "bank.db", texts, validation, seed, counts)

    entries = [{"name": f"{pid}.md", "b64": base64.b64encode(text.encode("utf-8")).decode()} for pid, text in texts.items()]
    built = doc_ingest.build(entries, out, chunking=agent_cfg.chunking, embedding=agent_cfg.embedding)
    if built.docs_db_path is None:
        raise PrepareError("no policy document could be indexed")
    with closing(sqlite3.connect(built.docs_db_path)) as conn:
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    skipped_ids = set(built.skipped)
    titles = {f"{r['object_id']}.md": str(r.get("title") or r["object_id"]).strip() for r in selected}
    docs_summary = "\n".join(f"{name} — {title}" for name, title in titles.items() if name not in skipped_ids)
    log.info("policies: %d documents, %d chunks, %d skipped", len(selected) - len(skipped_ids), chunk_count, len(skipped_ids))

    state = {
        "db_path": "bank.db",
        "column_map": {},
        "schema_summary": schema_summary(out / "bank.db"),
        "docs_db_path": built.docs_db_path.name,
        "docs_summary": docs_summary,
    }
    (out / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    dataset = EvalDataset(name=f"lakequest_{domain}_sample" if sample else f"lakequest_{domain}", version=1, cases=cases, state_file=Path("state.json"))
    import yaml

    (out / "dataset.yaml").write_text(
        yaml.safe_dump(dataset.model_dump(mode="json", exclude_defaults=False), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    if sample:
        (out / "reference").mkdir(exist_ok=True)
        with (out / "reference" / "references.jsonl").open("w", encoding="utf-8") as f:
            for row in reference_rows(cases, domain):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    info = {
        "domain": domain,
        "sources": {
            "lake_zip": {"file": Path(lake_zip).name, "sha256": sha256(Path(lake_zip))},
            "questions": {"file": q_path.name, "sha256": sha256(q_path)} if q_path else None,
            "references": {"file": r_path.name, "sha256": sha256(r_path)} if r_path else None,
        },
        "counts": {
            "tables": table_counts,
            "policies": len(selected) - len(skipped_ids),
            "policies_skipped": len(skipped_ids),
            "chunks": chunk_count,
            "questions_in_file": len(q_df) if q_df is not None else None,
            "cases": len(cases),
            "cases_per_type": {t: sum(t in c.tags for c in cases) for t in SAMPLE_COUNTS} if sample else None,
            "cases_with_reference": sum(bool(c.expected.reference_answers) for c in cases),
        },
        "sample": {"seed": seed, "counts": counts or SAMPLE_COUNTS} if sample else None,
        "chunking": agent_cfg.chunking.model_dump(mode="json"),
        "embedding": agent_cfg.embedding.model_dump(mode="json"),
    }
    (out / "prepare.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return info


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; see the module docstring. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m evals.lakequest")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="build the bundle for one domain and split")
    p.add_argument("--lake-zip", type=Path, required=True)
    p.add_argument("--questions", type=Path, help="questions file or folder (omit to build bank_sample)")
    p.add_argument("--references", type=Path, help="references file or folder (omit for Open Test)")
    p.add_argument("--domain", default="bank")
    p.add_argument("--config", type=Path, required=True, help="run YAML (chunking and embedding settings)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=SAMPLE_SEED, help="bank_sample seed")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        info = prepare(args.lake_zip, args.questions, args.references, args.domain, args.config, args.out, seed=args.seed)
    except PrepareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info["counts"], indent=2))
    print(f"bundle: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
