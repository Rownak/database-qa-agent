"""Runs an evaluation: ask every case, score the answers, aggregate and save (design section 5.2).

:func:`run_eval` takes an already validated :class:`~data_agent.config.RunConfig` and a
folder, so it does not depend on how the config was assembled (a Hydra wrapper could call
it unchanged). ``eval.mode`` selects the agent mode (the full agent, one session per case)
or the retrieval mode (search only, no LLM, for tuning chunking and retrieval).
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

from data_agent import doc_ingest, retrieval
from data_agent.config import RunConfig
from data_agent.events import TurnResult, summarize_stages
from data_agent.ingest import SAMPLE_DOCS_DIR
from data_agent.runner import AgentSession, files_state
from evals import report
from evals.dataset import EvalCase, dataset_files, load_dataset, resolve
from evals.metrics import AGENT_METRICS, METRICS, RETRIEVAL_METRICS, predicted_object_ids


def _select_cases(cfg: RunConfig, cases: list[EvalCase]) -> list[EvalCase]:
    """Applies ``dataset.case_ids`` and ``dataset.limit``; an unknown id is an error."""
    ids = cfg.dataset.case_ids
    if ids:
        unknown = set(ids) - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in ids]
    return cases[: cfg.dataset.limit] if cfg.dataset.limit else cases


def _load_prebuilt(
    state_path: Path, cfg: RunConfig, fallback_dir: Path | None = None, checked: dict | None = None
) -> tuple[dict, dict | None]:
    """Loads a prebuilt bundle state (``evals.lakequest prepare``) and checks that it matches the run config.

    Args:
        state_path: The bundle's ``state.json``; its paths are relative to its folder.
        cfg: The run configuration.
        fallback_dir: Where to look for ``prepare.json`` when it is not next to ``state.json``
            (the dataset folder, for a bundle with one ``state.json`` per case).
        checked: Optional cache ``{prepare.json path: info}`` shared between calls, so the guard
            runs once per distinct ``prepare.json``.

    Returns:
        ``(state, prepare_info)``: the state with absolute ``db_path`` / ``docs_db_path``, and the
        content of ``prepare.json`` (``None`` when absent, which skips the guard).

    Raises:
        ValueError: If a database file is missing, or the chunking or embedding settings used to
            build ``docs.db`` differ from ``cfg.agent`` (a stale index would be scored silently).
    """
    base = state_path.parent
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for key in ("db_path", "docs_db_path"):
        if state.get(key):
            state[key] = str((base / state[key]).resolve())
            if not Path(state[key]).is_file():
                raise ValueError(f"{state_path}: {key} does not exist: {state[key]}")
    info_path = base / "prepare.json"
    if not info_path.is_file() and fallback_dir is not None:
        info_path = fallback_dir / "prepare.json"
    if not info_path.is_file():
        return state, None
    if checked is not None and info_path in checked:
        return state, checked[info_path]
    info = json.loads(info_path.read_text(encoding="utf-8"))
    for key in ("chunking", "embedding"):
        # Missing keys read as their defaults, so a bundle written before a setting existed (for
        # example the embedding provider) still matches; the server URL does not change the index.
        wanted = getattr(cfg.agent, key).model_dump(mode="json", exclude={"base_url"})
        try:
            stored = type(getattr(cfg.agent, key)).model_validate(info.get(key) or {}).model_dump(mode="json", exclude={"base_url"})
        except ValueError:
            stored = info.get(key)
        if stored != wanted:
            raise ValueError(
                f"{key} settings differ from the prepared bundle ({info.get(key)} vs {wanted}); "
                f"run `prepare` again with this config"
            )
    if checked is not None:
        checked[info_path] = info
    return state, info


def _ask(session_factory: Callable, cfg: RunConfig, state: dict, question: str, keep_files: bool = False) -> TurnResult:
    """Asks one question in a fresh session and closes it; any failure becomes ``TurnResult.error``.

    ``keep_files`` leaves the session's database folder alone, for a shared prebuilt bundle.
    """
    session = None
    try:
        session = session_factory(cfg.agent, state)
        return session.ask(question)
    except Exception as exc:
        return TurnResult("", [], None, 0.0, f"{type(exc).__name__}: {exc}")
    finally:
        if session is not None:
            try:
                session.close(delete_files=False) if keep_files else session.close()
            except Exception:
                pass  # cleanup must not hide the result


def _row(case: EvalCase, repeat: int, result: TurnResult, names: list[str]) -> dict:
    """One line of ``results.jsonl``: the turn and its scores."""
    row = {
        "id": case.id,
        "repeat": repeat,
        "tags": case.tags,
        "question": case.question,
        "answer": result.answer,
        "intent": result.intent,
        "calls": report.short_calls(result.calls),
        "object_ids": predicted_object_ids(result),
        "metrics": {n: METRICS[n](case, result) for n in names},
        "latency_s": round(result.latency_s, 3),
        "error": result.error,
    }
    if result.timing is not None:  # only with agent.latency_trace; otherwise the row keeps its old keys
        row["timing"] = result.timing
    return row


def _retrieve(db_path: Path, question: str, cfg: RunConfig) -> TurnResult:
    """Runs one retrieval (no LLM) and wraps the hits as a ``search_documents`` call.

    The hits are shaped like the tool's passages, so the same metrics apply in both
    modes. A failure becomes ``TurnResult.error``.
    """
    started = time.monotonic()
    rcfg = cfg.agent.retrieval
    stages: list[dict] = []  # with latency_trace: the steps that ran (no embedding with bm25)
    try:
        vec = None
        if rcfg.mode != "bm25":  # BM25-only retrieval needs no query embedding
            vec = doc_ingest.embed([question], "RETRIEVAL_QUERY", **doc_ingest.embed_options(cfg.agent.embedding))
            stages.append({"stage": "query_embedding", "s": time.monotonic() - started})
        mark = time.monotonic()
        found = retrieval.search(
            db_path, question, top_k=rcfg.max_top_k, query_vec=None if vec is None else vec[0], cfg=rcfg
        )
        stages.append({"stage": "search", "s": time.monotonic() - mark})
        passages = [
            {"source": h.source, "section_number": h.section_number, "section_path": h.section_path, "matched_by": h.matched_by}
            for h in found.hits
        ]
        calls = [{"name": "search_documents", "args": {"query": question}, "response": {"passages": passages}}]
        latency = time.monotonic() - started
        return TurnResult("", calls, None, latency, None, _trace(cfg, stages, latency))
    except Exception as exc:
        latency = time.monotonic() - started
        return TurnResult("", [], None, latency, f"{type(exc).__name__}: {exc}", _trace(cfg, stages, latency))


def _trace(cfg: RunConfig, stages: list[dict], latency_s: float) -> dict | None:
    """Closes the retrieval stages into a trace, or returns ``None`` when ``agent.latency_trace`` is off."""
    return summarize_stages(stages, latency_s) if cfg.agent.latency_trace else None


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


def _pcts(values: list[float]) -> dict:
    """``p50_s`` / ``p95_s`` / ``max_s`` of a non-empty list."""
    return {
        "p50_s": float(np.percentile(values, 50)),
        "p95_s": float(np.percentile(values, 95)),
        "max_s": float(max(values)),
    }


def _latency_stages(rows: list[dict]) -> dict | None:
    """Per-stage latency over the rows that carry a usable trace, or ``None`` when there are none.

    A row counts when it has a ``timing`` with ``ok`` true and no error (as for ``latency_p50_s``).
    A stage that ran several times in one turn is summed for that turn, and the percentiles are
    taken over the turns that had the stage, so a stage that rarely runs is not diluted by zeros.

    Returns:
        ``{"n", "query_p50_s", "query_p95_s", "in_tok_mean", "out_tok_mean", "stages": {name:
        {"turns", "calls_per_turn", "p50_s", "p95_s", "max_s"}}}``. ``other`` is the residual.
        The token means appear only when the events carried token counts.
    """
    traced = [r["timing"] for r in rows if r.get("timing") and r["timing"]["ok"] and not r["error"]]
    if not traced:
        return None
    per_stage: dict[str, list[tuple[float, int]]] = {}  # name -> [(seconds summed in a turn, entries in it)]
    for t in traced:
        sums: dict[str, list] = {}
        for st in t["stages"]:
            acc = sums.setdefault(st["stage"], [0.0, 0])
            acc[0] += st["s"]
            acc[1] += 1
        sums["other"] = [t["other_s"], 1]
        for name, (seconds, count) in sums.items():
            per_stage.setdefault(name, []).append((seconds, count))
    out: dict = {"n": len(traced)}
    queries = [t["query_s"] for t in traced]
    out["query_p50_s"], out["query_p95_s"] = float(np.percentile(queries, 50)), float(np.percentile(queries, 95))
    for key in ("in_tok", "out_tok"):
        totals = [sum(st.get(key, 0) for st in t["stages"]) for t in traced if any(key in st for st in t["stages"])]
        if totals:
            out[f"{key}_mean"] = sum(totals) / len(totals)
    out["stages"] = {
        name: {"turns": len(v), "calls_per_turn": sum(c for _, c in v) / len(v), **_pcts([x for x, _ in v])}
        for name, v in per_stage.items()
    }
    return out


def _aggregate(rows: list[dict], names: list[str]) -> dict:
    """Mean per metric over the rows where it applies, plus counts and latency percentiles.

    ``latency_stages`` (see :func:`_latency_stages`) is added only when the rows carry traces.
    """
    metrics: dict[str, float | None] = {}
    for name in names:
        values = [r["metrics"][name] for r in rows if r["metrics"].get(name) is not None]
        metrics[name] = sum(values) / len(values) if values else None
    latencies = [r["latency_s"] for r in rows if not r["error"]]
    agg = {
        "n": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "latency_p50_s": float(np.percentile(latencies, 50)) if latencies else None,
        "latency_p95_s": float(np.percentile(latencies, 95)) if latencies else None,
        "metrics": metrics,
    }
    stages = _latency_stages(rows)
    if stages is not None:
        agg["latency_stages"] = stages
    return agg


def _by_tag(rows: list[dict], names: list[str]) -> dict[str, dict]:
    """Means per case tag (for example ``hybrid`` / ``table`` / ``document``): ``{tag: {"n", "metrics"}}``.

    With traces in the rows, each tag also gets ``latency_p50_s`` / ``latency_p95_s``.
    """
    traced = any("timing" in r for r in rows)
    out = {}
    for tag in sorted({t for r in rows for t in r["tags"]}):
        agg = _aggregate([r for r in rows if tag in r["tags"]], names)
        out[tag] = {"n": agg["n"], "metrics": agg["metrics"]}
        if traced:
            out[tag]["latency_p50_s"], out[tag]["latency_p95_s"] = agg["latency_p50_s"], agg["latency_p95_s"]
    return out


def run_eval(cfg: RunConfig, run_dir: Path, session_factory: Callable = AgentSession) -> dict:
    """Runs the dataset and saves the results.

    Args:
        cfg: The validated run configuration. ``eval.mode`` is ``agent`` (full agent; the
            session factory is used) or ``retrieval`` (search only, no LLM, only the cases
            with ``expected.sections``). Only the metrics of ``eval.metrics`` that suit the
            mode are computed (:data:`evals.metrics.AGENT_METRICS` or ``RETRIEVAL_METRICS``).
        run_dir: Folder for ``config.yaml``, ``results.jsonl`` and ``metrics.json`` (and
            ``predictions.jsonl`` when ``eval.predictions`` is true).
        session_factory: ``(AgentConfig, state) -> session`` with ``ask`` and ``close``;
            tests pass a fake that returns canned results. Unused in retrieval mode.

    Returns:
        ``{"n", "errors", "latency_p50_s", "latency_p95_s", "metrics": {name: mean | None},
        "by_tag": {tag: {"n", "metrics"}}}``, plus ``"data"`` (the bundle's ``prepare.json``)
        for a prebuilt dataset (the first ``prepare.json`` found). ``n`` counts case runs (cases times repeats); an errored run
        is scored like any other, with its empty answer.

    Raises:
        ValueError: For a prebuilt dataset (``state_file`` on the dataset or on a case) in retrieval
            mode, with a missing database file, or whose ``prepare.json`` chunking or embedding
            differs from the config. This is raised before the first question.

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

    # precedence: the case's state_file, then the dataset's, then table/docs
    ds_dir = resolve(cfg.dataset.path).parent
    wanted = {c.id: c.state_file or ds.state_file for c in cases}
    prebuilt: dict[Path, dict] = {}
    data = None
    if any(wanted.values()):
        if cfg.eval.mode == "retrieval":
            raise ValueError("a prebuilt dataset (state_file, dataset or case level) can only be run in agent mode")
        checked: dict[Path, dict] = {}
        for rel in dict.fromkeys(p for p in wanted.values() if p is not None):
            path = rel if rel.is_absolute() else ds_dir / rel
            prebuilt[rel], info = _load_prebuilt(path, cfg, fallback_dir=ds_dir, checked=checked)
            data = data or info

    if cfg.eval.mode == "retrieval":
        rows = _run_retrieval(cfg, cases, docs, names)
    else:
        rows = []
        for case in cases:
            for repeat in range(cfg.eval.repeats):
                rel = wanted[case.id]
                state = dict(prebuilt[rel]) if rel is not None else files_state([table] if table is not None else None, docs)
                result = _ask(session_factory, cfg, state, case.question, keep_files=rel is not None)
                rows.append(_row(case, repeat, result, names))
    metrics = _aggregate(rows, names)
    metrics["by_tag"] = _by_tag(rows, names)
    if data is not None:
        metrics["data"] = data
    report.write_run(run_dir, cfg, rows, metrics)
    return metrics
