"""Runs an evaluation: ask every case, score the answers, aggregate and save (design section 5.2).

:func:`run_eval` takes an already validated :class:`~data_agent.config.RunConfig` and a
folder, so it does not depend on how the config was assembled (a Hydra wrapper could call
it unchanged). ``eval.mode`` selects the agent mode (the full agent, one session per case)
or the retrieval mode (search only, no LLM, for tuning chunking and retrieval).
"""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

from data_agent import doc_ingest, retrieval
from data_agent.config import RunConfig
from data_agent.events import TurnResult
from data_agent.ingest import SAMPLE_DOCS_DIR
from data_agent.runner import AgentSession, files_state
from evals import report
from evals.dataset import EvalCase, dataset_files, load_dataset
from evals.metrics import AGENT_METRICS, METRICS, RETRIEVAL_METRICS


def _select_cases(cfg: RunConfig, cases: list[EvalCase]) -> list[EvalCase]:
    """Applies ``dataset.case_ids`` and ``dataset.limit``; an unknown id is an error."""
    ids = cfg.dataset.case_ids
    if ids:
        unknown = set(ids) - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in ids]
    return cases[: cfg.dataset.limit] if cfg.dataset.limit else cases


def _ask(session_factory: Callable, cfg: RunConfig, state: dict, question: str) -> TurnResult:
    """Asks one question in a fresh session and closes it; any failure becomes ``TurnResult.error``."""
    session = None
    try:
        session = session_factory(cfg.agent, state)
        return session.ask(question)
    except Exception as exc:
        return TurnResult("", [], None, 0.0, f"{type(exc).__name__}: {exc}")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass  # cleanup must not hide the result


def _row(case: EvalCase, repeat: int, result: TurnResult, names: list[str]) -> dict:
    """One line of ``results.jsonl``: the turn and its scores."""
    return {
        "id": case.id,
        "repeat": repeat,
        "tags": case.tags,
        "question": case.question,
        "answer": result.answer,
        "intent": result.intent,
        "calls": report.short_calls(result.calls),
        "metrics": {n: METRICS[n](case, result) for n in names},
        "latency_s": round(result.latency_s, 3),
        "error": result.error,
    }


def _retrieve(db_path: Path, question: str, cfg: RunConfig) -> TurnResult:
    """Runs one retrieval (no LLM) and wraps the hits as a ``search_documents`` call.

    The hits are shaped like the tool's passages, so the same metrics apply in both
    modes. A failure becomes ``TurnResult.error``.
    """
    started = time.monotonic()
    rcfg = cfg.agent.retrieval
    try:
        vec = None
        if rcfg.mode != "bm25":  # BM25-only retrieval needs no query embedding
            vec = doc_ingest.embed([question], "RETRIEVAL_QUERY", **doc_ingest.embed_options(cfg.agent.embedding))
        found = retrieval.search(
            db_path, question, top_k=rcfg.max_top_k, query_vec=None if vec is None else vec[0], cfg=rcfg
        )
        passages = [
            {"source": h.source, "section_number": h.section_number, "section_path": h.section_path, "matched_by": h.matched_by}
            for h in found.hits
        ]
        calls = [{"name": "search_documents", "args": {"query": question}, "response": {"passages": passages}}]
        return TurnResult("", calls, None, time.monotonic() - started)
    except Exception as exc:
        return TurnResult("", [], None, time.monotonic() - started, f"{type(exc).__name__}: {exc}")


def _run_retrieval(cfg: RunConfig, cases: list[EvalCase], docs: list[Path] | None, names: list[str]) -> list[dict]:
    """Builds ``docs.db`` once and scores retrieval for every case that has ``expected.sections``."""
    paths = docs if docs is not None else sorted(p for p in SAMPLE_DOCS_DIR.iterdir() if p.is_file())
    entries = [{"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode()} for p in paths]
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="eval_retrieval_") as tmp:
        built = doc_ingest.build(entries, tmp, chunking=cfg.agent.chunking, embedding=cfg.agent.embedding)
        if built.docs_db_path is None:
            raise ValueError("retrieval mode: no usable document to index")
        for case in cases:
            if not case.expected.sections:
                continue
            for repeat in range(cfg.eval.repeats):
                rows.append(_row(case, repeat, _retrieve(built.docs_db_path, case.question, cfg), names))
    return rows


def _aggregate(rows: list[dict], names: list[str]) -> dict:
    """Mean per metric over the rows where it applies, plus counts and latency percentiles."""
    metrics: dict[str, float | None] = {}
    for name in names:
        values = [r["metrics"][name] for r in rows if r["metrics"].get(name) is not None]
        metrics[name] = sum(values) / len(values) if values else None
    latencies = [r["latency_s"] for r in rows if not r["error"]]
    return {
        "n": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "latency_p50_s": float(np.percentile(latencies, 50)) if latencies else None,
        "latency_p95_s": float(np.percentile(latencies, 95)) if latencies else None,
        "metrics": metrics,
    }


def run_eval(cfg: RunConfig, run_dir: Path, session_factory: Callable = AgentSession) -> dict:
    """Runs the dataset and saves the results.

    Args:
        cfg: The validated run configuration. ``eval.mode`` is ``agent`` (full agent; the
            session factory is used) or ``retrieval`` (search only, no LLM, only the cases
            with ``expected.sections``). Only the metrics of ``eval.metrics`` that suit the
            mode are computed (:data:`evals.metrics.AGENT_METRICS` or ``RETRIEVAL_METRICS``).
        run_dir: Folder for ``config.yaml``, ``results.jsonl`` and ``metrics.json``.
        session_factory: ``(AgentConfig, state) -> session`` with ``ask`` and ``close``;
            tests pass a fake that returns canned results. Unused in retrieval mode.

    Returns:
        ``{"n", "errors", "latency_p50_s", "latency_p95_s", "metrics": {name: mean | None}}``.
        ``n`` counts case runs (cases times repeats); an errored run is scored like any
        other, with its empty answer.

    Side Effects:
        Writes the run folder. The config is written before the first question, so a
        crashed run still records what it was. Agent mode with the real session factory
        calls the model; retrieval mode embeds the chunks once and each query once.
    """
    report.write_config(run_dir, cfg)
    ds = load_dataset(cfg.dataset.path)
    table, docs = dataset_files(ds)
    cases = _select_cases(cfg, ds.cases)
    allowed = RETRIEVAL_METRICS if cfg.eval.mode == "retrieval" else AGENT_METRICS
    names = [m for m in cfg.eval.metrics if m in allowed]

    if cfg.eval.mode == "retrieval":
        rows = _run_retrieval(cfg, cases, docs, names)
    else:
        rows = []
        for case in cases:
            for repeat in range(cfg.eval.repeats):
                result = _ask(session_factory, cfg, files_state(table, docs), case.question)
                rows.append(_row(case, repeat, result, names))
    metrics = _aggregate(rows, names)
    report.write_run(run_dir, cfg, rows, metrics)
    return metrics
