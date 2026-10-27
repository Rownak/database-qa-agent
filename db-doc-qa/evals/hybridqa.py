"""HybridQA adapter: turns the benchmark into a self-contained bundle and scores a run (HybridQA design sections 2-3 and 5.2).

``uv run python -m evals.hybridqa prepare ...`` (from ``db-doc-qa/``) reads the labelled dev
questions of ``benchmark/HybridQA/released_data``, draws a reproducible stratified sample
(``table`` and ``passage`` questions), fetches the Wikipedia table and linked passages of each
sampled question and writes::

    dataset.yaml            an EvalDataset; every case names its own table bundle (``state_file``)
    tables/<safe_id>/       <safe_id>.csv, docs/<Entity>.md, data.db, docs.db, state.json
    reference/dev_reference.json   the official reference format, restricted to the sample
    prepare.json            sources and hashes, seed, counts, skipped qids, chunking / embedding

``score <run_dir> --reference <bundle>/reference/dev_reference.json`` turns the run's ``predictions.jsonl``
into the official format and applies the logic of ``evaluate_script.py`` (MIT, copied below), so our own
``exact_match`` / ``squad_f1`` can be checked against it.

All HybridQA parsing and mapping lives here; the runner, the metrics and the agent stay
dataset-agnostic and only see the bundle. The module reuses ``ingest.load_files`` and
``doc_ingest.build`` and changes none of the existing code.
"""

from __future__ import annotations

import argparse
import base64
import collections
import csv
import hashlib
import io
import json
import random
import re
import sqlite3
import string
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from data_agent import doc_ingest, ingest
from data_agent.config import load_config
from data_agent.logs import get_logger
from evals.dataset import EvalCase, EvalDataset, Expected

log = get_logger(__name__)

RAW_BASE = "https://raw.githubusercontent.com/wenhuchen/WikiTables-WithLinks/master/"
DEFAULT_SEED = 20260101
TYPES = ("table", "passage")

Fetch = Callable[[str], bytes]  # "tables_tok/<id>.json" -> file bytes; raises on failure

_UNSAFE_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
MAX_STEM = 60
EMBED_ATTEMPTS = 6  # the embedding API rate-limits bursts; a failed build is retried after a pause
EMBED_WAIT_S = 20


class PrepareError(Exception):
    """A problem with the input files that the user can fix (missing file or label list)."""


def sha256_bytes(data: bytes) -> str:
    """Returns the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def http_fetch(rel: str) -> bytes:
    """Downloads ``rel`` from the WikiTables-WithLinks repository (the default :data:`Fetch`).

    Raises:
        urllib.error.URLError: If the file does not exist or the network fails.
    """
    url = RAW_BASE + "/".join(quote(part) for part in rel.split("/"))
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def safe_file_stem(text: str, taken: set[str] | None = None) -> str:
    """Turns ``text`` into a Windows-safe file stem: no reserved characters or names, unique in ``taken``.

    Args:
        text: Entity title or table id.
        taken: Lower-cased stems already used; the result is added to it. A repeat gets ``_2``, ``_3`` ...

    Returns:
        A non-empty stem of at most :data:`MAX_STEM` characters.
    """
    stem = _UNSAFE_FILE_CHARS.sub("_", text).strip(" .") or "entity"
    stem = stem[:MAX_STEM].rstrip(" .")
    if stem.lower() in _RESERVED_NAMES:
        stem = f"_{stem}"
    if taken is not None:
        base, n = stem, 2
        while stem.lower() in taken:
            stem, n = f"{base}_{n}", n + 1
        taken.add(stem.lower())
    return stem


def entity_title(link: str) -> str:
    """``/wiki/Emmitt_Smith`` -> ``Emmitt Smith`` (URL-decoded, ``_`` -> space, fragment dropped)."""
    name = unquote(link.rsplit("/wiki/", 1)[-1]).split("#", 1)[0]
    return name.replace("_", " ").strip()


def sql_stem(table_id: str, taken: set[str]) -> str:
    """A SQL- and Windows-safe lower-case stem for a table id, unique within ``taken``."""
    return ingest.table_name_for(f"{table_id[:MAX_STEM]}.csv", taken)


def _header_key(text: str) -> str:
    return _NON_ALNUM.sub("_", text.lower()).strip("_")


def repair_headers(names: list[str]) -> list[str]:
    """Makes HybridQA headers acceptable to the ingestion code, keeping them readable.

    A blank or symbol-only header becomes ``col_<n>``, a numeric one ``col_<text>`` and a repeat
    (after cleaning) gets ``_2``, ``_3`` ... The original text of the others is kept.
    """
    out: list[str] = []
    seen: set[str] = set()
    for pos, raw in enumerate(names, start=1):
        text = " ".join(str(raw).split())
        if not _header_key(text):
            text = f"col_{pos}"
        elif _NUMBER.match(text):
            text = f"col_{text}"
        base, n = text, 2
        while _header_key(text) in seen:
            text, n = f"{base} {n}", n + 1
        seen.add(_header_key(text))
        out.append(text)
    return out


@dataclass
class TableData:
    """One table fetched and converted, ready to be written.

    Attributes:
        table_id: HybridQA table id.
        stem: Safe id used for the folder and the CSV name (and so the SQL table name).
        csv_bytes: The table as CSV (header names plus ``cell_text``; links dropped).
        docs: Entity file name -> Markdown text, in link order.
        entity_files: Link (``/wiki/X``) -> entity file name, for the gold object ids.
        raw_hashes: Fetched file -> sha256.
    """

    table_id: str
    stem: str
    csv_bytes: bytes
    docs: dict[str, str]
    entity_files: dict[str, str]
    raw_hashes: dict[str, str] = field(default_factory=dict)


class CachedFetch:
    """Reads files through a cache folder and records the sha256 of every file used.

    Attributes:
        hashes: ``"tables_tok/<id>.json"`` -> sha256 of the cached or downloaded bytes.
    """

    def __init__(self, cache: Path, fetch: Fetch):
        self.cache, self.fetch, self.hashes = cache, fetch, {}

    def __call__(self, rel: str) -> bytes:
        kind, name = rel.split("/", 1)
        path = self.cache / kind / f"{safe_file_stem(Path(name).stem)}-{sha256_bytes(name.encode())[:8]}.json"
        if path.is_file():
            data = path.read_bytes()
        else:
            data = self.fetch(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.hashes[rel] = sha256_bytes(data)
        return data


def convert_table(table_id: str, table_json: dict, request_json: dict, stem: str) -> TableData:
    """Maps one HybridQA table and its passages to a CSV and entity Markdown files.

    Args:
        table_id: HybridQA table id.
        table_json: Parsed ``tables_tok/<id>.json`` (``header`` and ``data`` of ``[text, links]`` cells).
        request_json: Parsed ``request_tok/<id>.json`` (``/wiki/Entity`` -> passage text).
        stem: Safe id for the CSV file name.

    Returns:
        The converted :class:`TableData`.

    Raises:
        ValueError: If the table has no header or no rows, or none of its links has a passage.
    """
    header = [cell[0] for cell in table_json.get("header") or []]
    rows = [[cell[0] for cell in row] for row in table_json.get("data") or []]
    if not header or not rows:
        raise ValueError("table has no header or no rows")
    width = len(header)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(repair_headers(header))
    for row in rows:
        row = [" ".join(str(c).split()) for c in row[:width]]
        writer.writerow(row + [""] * (width - len(row)))

    links: list[str] = []  # every linked entity, in table order, once
    for row in [table_json.get("header") or []] + (table_json.get("data") or []):
        for cell in row:
            for link in cell[1]:
                if link not in links:
                    links.append(link)
    docs: dict[str, str] = {}
    entity_files: dict[str, str] = {}
    taken: set[str] = set()
    for link in links:
        passage = (request_json.get(link) or "").strip()
        if not passage:
            continue
        title = entity_title(link)
        name = safe_file_stem(title, taken) + ".md"
        docs[name] = f"# {title}\n\n{passage}\n"
        entity_files[link] = name
    if not docs:
        raise ValueError("no linked passage")
    return TableData(table_id, stem, buffer.getvalue().encode("utf-8"), docs, entity_files)


def load_labels(hybridqa: Path, split: str) -> tuple[list[dict], dict, dict[str, str]]:
    """Reads the questions and the type lists.

    Returns:
        ``(questions, reference, hashes)``: the traced questions, ``dev_reference.json`` and the
        sha256 of both files.

    Raises:
        PrepareError: If either file is missing.
    """
    released = hybridqa / "released_data"
    q_path, r_path = released / f"{split}.traced.json", released / f"{split}_reference.json"
    for path in (q_path, r_path):
        if not path.is_file():
            raise PrepareError(f"missing {path}; pass --hybridqa <folder of the HybridQA repository>")
    hashes = {p.name: sha256_bytes(p.read_bytes()) for p in (q_path, r_path)}
    return json.loads(q_path.read_text(encoding="utf-8")), json.loads(r_path.read_text(encoding="utf-8")), hashes


def git_commit(folder: Path) -> str | None:
    """The HEAD commit of the HybridQA repository, or ``None`` if it is not a git checkout."""
    try:
        out = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def select(
    questions: list[dict],
    reference: dict,
    per_type: int | None,
    seed: int,
    one_per_table: bool,
    load: Callable[[str], TableData],
) -> tuple[list[tuple[dict, str]], list[dict], int]:
    """Draws the stratified sample.

    The qids of each type are sorted and shuffled with their own ``random.Random(seed)``. The two
    lists are then walked in alternation. A question is skipped when its table was used before
    (one-per-table) or when ``load`` fails for its table. Both types keep deciding after their own
    quota is full (the table is only reserved), so the decision for any question depends only on
    earlier ones: a larger ``per_type`` extends a smaller sample and never changes it.

    Args:
        questions: Traced questions (``question_id``, ``table_id`` ...).
        reference: ``dev_reference.json`` (``table`` and ``passage`` qid lists).
        per_type: Questions to keep per type, or ``None`` for all.
        seed: Seed of the shuffles.
        one_per_table: Keep at most one question per table, across both types.
        load: Fetches and converts a table by id (raises on failure); called at most once per table.

    Returns:
        ``(chosen, skipped, unlabeled)``: ``(question, type)`` pairs in walk order per type,
        ``{"qid", "type", "reason"}`` for every skipped question, and the number of questions in
        neither list.
    """
    by_id = {q["question_id"]: q for q in questions}
    lists = {t: sorted(q for q in reference.get(t, []) if q in by_id) for t in TYPES}
    for t in TYPES:
        random.Random(seed).shuffle(lists[t])
    labelled = {q for t in TYPES for q in lists[t]}
    unlabeled = sum(1 for q in questions if q["question_id"] not in labelled)

    quota = {t: (per_type if per_type is not None else len(lists[t])) for t in TYPES}
    kept: dict[str, list[dict]] = {t: [] for t in TYPES}
    skipped: list[dict] = []
    used: set[str] = set()
    failed: dict[str, str] = {}
    for position in range(max(len(v) for v in lists.values())):
        for t in TYPES:
            if position >= len(lists[t]) or all(len(kept[x]) >= quota[x] for x in TYPES):
                continue
            q = by_id[lists[t][position]]
            table_id, full = q["table_id"], len(kept[t]) >= quota[t]
            reason = None
            if one_per_table and table_id in used:
                reason = "table already used by another sampled question"
            elif table_id in failed:
                reason = failed[table_id]
            else:
                try:
                    load(table_id)
                except Exception as exc:
                    reason = failed[table_id] = f"{type(exc).__name__}: {exc}"
            if reason:
                if not full:
                    skipped.append({"qid": q["question_id"], "type": t, "reason": reason})
                continue
            used.add(table_id)
            if not full:
                kept[t].append(q)
    chosen = [(q, t) for t in TYPES for q in kept[t]]
    return chosen, skipped, unlabeled


def _write_table(data: TableData, folder: Path, config) -> dict:
    """Writes one table bundle and returns ``{"chunks", "passages"}``.

    Raises:
        PrepareError: If the table cannot be loaded or indexed.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{data.stem}.csv").write_bytes(data.csv_bytes)
    docs_dir = folder / "docs"
    docs_dir.mkdir(exist_ok=True)
    for name, text in data.docs.items():
        (docs_dir / name).write_text(text, encoding="utf-8")
    loaded = ingest.load_files([(f"{data.stem}.csv", data.csv_bytes)], folder)
    if isinstance(loaded, ingest.IngestError):
        raise PrepareError(f"{data.table_id}: {loaded.message}")
    entries = [{"name": n, "b64": base64.b64encode(t.encode("utf-8")).decode()} for n, t in data.docs.items()]
    for attempt in range(1, EMBED_ATTEMPTS + 1):
        built = doc_ingest.build(entries, folder, chunking=config.chunking, embedding=config.embedding)
        if built.docs_db_path is None or built.embedded or attempt == EMBED_ATTEMPTS:
            break
        log.warning("%s: embedding failed (attempt %d); retrying", data.table_id, attempt)
        time.sleep(EMBED_WAIT_S * attempt)
    if built.docs_db_path is None:
        raise PrepareError(f"{data.table_id}: no passage could be indexed")
    if not built.embedded:  # a BM25-only index would be scored as if it were hybrid
        raise PrepareError(f"{data.table_id}: embedding failed after {EMBED_ATTEMPTS} attempts")
    with closing(sqlite3.connect(built.docs_db_path)) as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    state = {
        "db_path": loaded.db_path.name,
        "column_map": loaded.column_map,
        "schema_summary": loaded.schema_summary,
        "docs_db_path": built.docs_db_path.name,
        "docs_summary": built.docs_summary,
    }
    (folder / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"chunks": chunks, "passages": len(data.docs) - len(built.skipped), "passages_skipped": len(built.skipped)}


def prepare(
    hybridqa: Path,
    config: Path,
    out: Path,
    *,
    split: str = "dev",
    per_type: int | None = 25,
    seed: int = DEFAULT_SEED,
    one_per_table: bool = True,
    cache: Path | None = None,
    fetch: Fetch | None = None,
) -> dict:
    """Builds the bundle for a stratified sample of one split.

    Args:
        hybridqa: Folder of the HybridQA repository (holds ``released_data/``).
        config: Run YAML; only ``agent.chunking`` and ``agent.embedding`` are used.
        out: Bundle folder; created if missing.
        split: Split name (``dev``).
        per_type: Questions per type, or ``None`` for all.
        seed: Seed of the sample.
        one_per_table: At most one sampled question per table.
        cache: Folder for the raw table and request files (default ``<out>/../raw``).
        fetch: Reader of ``tables_tok/<id>.json`` / ``request_tok/<id>.json``; ``None`` downloads with :func:`http_fetch`; tests pass a local one.

    Returns:
        The content written to ``prepare.json``.

    Raises:
        PrepareError: For a missing label file, or when no question could be kept.

    Side Effects:
        Downloads the files that are not cached, writes the bundle and calls the embedding API
        once per 100 chunks of each table.
    """
    agent_cfg = load_config(config).agent
    questions, reference, source_hashes = load_labels(hybridqa, split)
    fetcher = CachedFetch(cache if cache is not None else out.parent / "raw", fetch or http_fetch)

    tables: dict[str, TableData] = {}
    stems: set[str] = set()

    def load(table_id: str) -> TableData:
        if table_id not in tables:
            table_json = json.loads(fetcher(f"tables_tok/{table_id}.json"))
            request_json = json.loads(fetcher(f"request_tok/{table_id}.json"))
            data = convert_table(table_id, table_json, request_json, "")
            checked = ingest.read_table("x.csv", data.csv_bytes)  # reject now what load_files would reject later
            if isinstance(checked, ingest.IngestError):
                raise ValueError(checked.message)
            data.stem = sql_stem(table_id, stems)
            tables[table_id] = data
        return tables[table_id]

    chosen, skipped, unlabeled = select(questions, reference, per_type, seed, one_per_table, load)
    if not chosen:
        raise PrepareError("no question could be kept (see the skipped reasons above)")

    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, dict] = {}
    cases: list[EvalCase] = []
    for q, qtype in chosen:
        data = tables[q["table_id"]]
        if data.table_id not in written:
            written[data.table_id] = _write_table(data, out / "tables" / data.stem, agent_cfg)
        answer = str(reference["reference"][q["question_id"]])
        objects = [
            data.entity_files.get(node[2], entity_title(node[2]) + ".md").removesuffix(".md")
            for node in q.get("answer-node", [])
            if len(node) > 3 and node[3] == "passage"
        ]
        cases.append(
            EvalCase(
                id=q["question_id"],
                question=q["question"],
                tags=["hybridqa", qtype],
                state_file=Path(f"tables/{data.stem}/state.json"),
                expected=Expected(
                    reference_answers=[answer],
                    answer_contains=[answer],
                    object_ids=list(dict.fromkeys(objects)),
                ),
            )
        )

    import yaml

    dataset = EvalDataset(name=f"hybridqa_{split}_s{len(cases)}", version=1, cases=cases)
    dumped = dataset.model_dump(mode="json")
    for case in dumped["cases"]:
        case["state_file"] = Path(case["state_file"]).as_posix()  # the same bundle reads on every OS
    (out / "dataset.yaml").write_text(yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True), encoding="utf-8")

    ids = {c.id for c in cases}
    (out / "reference").mkdir(exist_ok=True)
    subset = {
        "reference": {q: a for q, a in reference["reference"].items() if q in ids},
        "table": [q for q in reference["table"] if q in ids],
        "passage": [q for q in reference["passage"] if q in ids],
    }
    (out / "reference" / f"{split}_reference.json").write_text(json.dumps(subset, ensure_ascii=False, indent=1), encoding="utf-8")

    info = {
        "split": split,
        "sources": {
            "released_data": source_hashes,
            "raw_files": dict(sorted(fetcher.hashes.items())),
            "hybridqa_commit": git_commit(hybridqa),
        },
        "sample": {
            "seed": seed,
            "per_type": per_type if per_type is not None else "all",
            "one_per_table": one_per_table,
            "cases_per_type": {t: sum(t in c.tags for c in cases) for t in TYPES},
            "unlabeled_excluded": unlabeled,
            "skipped": skipped,
        },
        "counts": {
            "cases": len(cases),
            "tables": len(written),
            "passages": sum(w["passages"] for w in written.values()),
            "passages_skipped": sum(w["passages_skipped"] for w in written.values()),  # no readable text
            "chunks": sum(w["chunks"] for w in written.values()),
        },
        "chunking": agent_cfg.chunking.model_dump(mode="json"),
        "embedding": agent_cfg.embedding.model_dump(mode="json"),
    }
    (out / "prepare.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return info


# ---- score: the logic of benchmark/HybridQA/evaluate_script.py (MIT), copied so the check stays independent
# of evals.metrics, whose exact_match / squad_f1 it is meant to verify ----


def _official_normalize(s: str) -> str:
    """Lower case, no punctuation, no articles ``a/an/the``, collapsed spaces (the official ``normalize_answer``)."""
    s = "".join(ch for ch in s.lower() if ch not in set(string.punctuation))
    return " ".join(re.sub(re.compile(r"\b(a|an|the)\b", re.UNICODE), " ", s).split())


def _official_exact(gold: str, pred: str) -> int:
    return int(_official_normalize(gold) == _official_normalize(pred))


def _official_f1(gold: str, pred: str) -> float:
    gold_toks = _official_normalize(gold).split() if gold else []
    pred_toks = _official_normalize(pred).split() if pred else []
    num_same = sum((collections.Counter(gold_toks) & collections.Counter(pred_toks)).values())
    if len(gold_toks) == 0 or len(pred_toks) == 0:
        return int(gold_toks == pred_toks)  # if either is empty, F1 is 1 when both are, else 0
    if num_same == 0:
        return 0
    precision, recall = num_same / len(pred_toks), num_same / len(gold_toks)
    return (2 * precision * recall) / (precision + recall)


def official_scores(predictions: list[dict], reference: dict) -> dict[str, float | None]:
    """Scores ``[{question_id, pred}]`` against a reference with the official formulas.

    Args:
        predictions: One ``{"question_id", "pred"}`` per question.
        reference: ``{"reference": {qid: answer}, "table": [qid], "passage": [qid]}``.

    Returns:
        ``table/passage/total exact`` and ``... f1`` on a 0-100 scale (``None`` for a type without questions).

    Raises:
        ValueError: If a question of the reference has no prediction.
    """
    preds = {p["question_id"]: p["pred"] for p in predictions}
    missing = [q for q in reference["reference"] if q not in preds]
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        raise ValueError(f"{len(missing)} reference question(s) have no prediction: {shown}")
    exact = {q: _official_exact(a, preds[q]) for q, a in reference["reference"].items()}
    f1 = {q: _official_f1(a, preds[q]) for q, a in reference["reference"].items()}

    def mean(scores: dict, qids: list[str]) -> float | None:
        return 100.0 * sum(scores[q] for q in qids) / len(qids) if qids else None

    groups = {"table": reference["table"], "passage": reference["passage"], "total": list(reference["reference"])}
    out: dict[str, float | None] = {}
    for name, qids in groups.items():
        out[f"{name} exact"], out[f"{name} f1"] = mean(exact, qids), mean(f1, qids)
    return out


def score(run_dir: Path, reference_path: Path) -> dict:
    """Scores a run folder with the official logic and writes ``official_scores.json`` into it.

    Args:
        run_dir: A run folder holding ``predictions.jsonl`` (its ``answer`` is the first line when the run used
            ``eval.prediction_answer: first_line``).
        reference_path: ``reference/dev_reference.json`` of the bundle.

    Returns:
        The scores of :func:`official_scores` plus ``n`` (questions scored).

    Raises:
        PrepareError: If a file is missing, or a reference question has no prediction.
    """
    pred_path = run_dir / "predictions.jsonl"
    for path in (pred_path, reference_path):
        if not path.is_file():
            raise PrepareError(f"missing {path}" + ("; run with eval.predictions=true" if path == pred_path else ""))
    predictions = [
        {"question_id": row["qa_id"], "pred": row["answer"]}
        for row in map(json.loads, pred_path.read_text(encoding="utf-8").splitlines())
        if row.get("qa_id")
    ]
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    try:
        scores = official_scores(predictions, reference)
    except ValueError as exc:
        raise PrepareError(f"{pred_path}: {exc}") from exc
    result = {**scores, "n": len(reference["reference"])}
    (run_dir / "official_scores.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _per_type(value: str) -> int | None:
    return None if value == "all" else int(value)


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; see the module docstring. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m evals.hybridqa")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="build the bundle for a stratified sample")
    p.add_argument("--hybridqa", type=Path, required=True, help="folder of the HybridQA repository")
    p.add_argument("--config", type=Path, required=True, help="run YAML (chunking and embedding settings)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--split", default="dev")
    p.add_argument("--per-type", type=_per_type, default=25, help="questions per type, or 'all'")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--cache", type=Path, default=Path("../benchmark/hybridqa_prepared/raw"))
    p.add_argument("--one-per-table", action=argparse.BooleanOptionalAction, default=True)
    s = sub.add_parser("score", help="score a run folder with the official HybridQA logic")
    s.add_argument("run_dir", type=Path)
    s.add_argument("--reference", type=Path, required=True, help="<bundle>/reference/dev_reference.json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "score":
        try:
            result = score(args.run_dir, args.reference)
        except PrepareError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    try:
        info = prepare(
            args.hybridqa, args.config, args.out, split=args.split, per_type=args.per_type,
            seed=args.seed, one_per_table=args.one_per_table, cache=args.cache,
        )
    except PrepareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"sample": {k: v for k, v in info["sample"].items() if k != "skipped"}, "counts": info["counts"]}, indent=2))
    print(f"skipped: {len(info['sample']['skipped'])}\nbundle: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
