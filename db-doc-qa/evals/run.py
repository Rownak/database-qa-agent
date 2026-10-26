"""Runs an evaluation: ask every case, score the answers, aggregate and save (design section 5.2).

:func:`run_eval` takes an already validated :class:`~data_agent.config.RunConfig` and a
folder, so it does not depend on how the config was assembled (a Hydra wrapper could call
it unchanged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from data_agent.config import RunConfig
from data_agent.events import TurnResult
from data_agent.runner import AgentSession, files_state
from evals import report
from evals.dataset import EvalCase, dataset_files, load_dataset
from evals.metrics import METRICS


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
    """Runs the dataset against the agent and saves the results.

    Args:
        cfg: The validated run configuration. Only the metrics of ``eval.metrics`` that
            exist in :data:`evals.metrics.METRICS` are computed.
        run_dir: Folder for ``config.yaml``, ``results.jsonl`` and ``metrics.json``.
        session_factory: ``(AgentConfig, state) -> session`` with ``ask`` and ``close``;
            tests pass a fake that returns canned results.

    Returns:
        ``{"n", "errors", "latency_p50_s", "latency_p95_s", "metrics": {name: mean | None}}``.
        ``n`` counts case runs (cases times repeats); an errored run is scored like any
        other, with its empty answer.

    Side Effects:
        Writes the run folder. The config is written before the first question, so a
        crashed run still records what it was. With the real session factory it calls the model.
    """
    report.write_config(run_dir, cfg)
    ds = load_dataset(cfg.dataset.path)
    table, docs = dataset_files(ds)
    names = [m for m in cfg.eval.metrics if m in METRICS]

    rows: list[dict] = []
    for case in _select_cases(cfg, ds.cases):
        for repeat in range(cfg.eval.repeats):
            result = _ask(session_factory, cfg, files_state(table, docs), case.question)
            rows.append(
                {
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
            )
    metrics = _aggregate(rows, names)
    report.write_run(run_dir, cfg, rows, metrics)
    return metrics
