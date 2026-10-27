"""Writes the files of one evaluation run (design section 5.4).

:func:`write_run` is the only place where results leave the runner, so a later tracker
(for example MLflow) can be called next to it without touching the run logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from data_agent.config import RunConfig
from evals.metrics import first_line, strip_sources

CALL_RESPONSE_CHARS = 300  # tool responses are cut in results.jsonl to keep the file small


def write_config(run_dir: Path, cfg: RunConfig) -> None:
    """Writes the resolved config (after overrides) to ``config.yaml``; rerunning it reproduces the run."""
    import yaml

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def short_calls(calls: list[dict]) -> list[dict]:
    """Copies tool calls with each response turned into text of at most 300 characters."""
    out = []
    for call in calls:
        text = json.dumps(call.get("response"), ensure_ascii=False, default=str)
        if len(text) > CALL_RESPONSE_CHARS:
            text = text[:CALL_RESPONSE_CHARS] + "…"
        out.append({"name": call["name"], "args": call["args"], "response": text})
    return out


def write_predictions(run_dir: Path, rows: list[dict], answer: str = "full") -> None:
    """Writes ``predictions.jsonl`` in the LakeQuest submission format.

    One line per case (the first repeat only, since the scorer keeps one prediction per
    ``qa_id``): ``{"qa_id", "answer", "object_ids"}``, where the answer has no ``Sources:``
    line, exactly as ``answer_f1`` scores it.

    Args:
        run_dir: The run folder.
        rows: The ``results.jsonl`` rows (``id``, ``repeat``, ``answer``, ``object_ids``).
        answer: ``full`` (the answer without its ``Sources:`` line) or ``first_line`` (the first
            non-empty line, as ``exact_match`` scores it).
    """
    text = (lambda a: first_line(strip_sources(a))) if answer == "first_line" else strip_sources
    with (run_dir / "predictions.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            if row.get("repeat", 0) == 0:
                line = {"qa_id": row["id"], "answer": text(row["answer"]), "object_ids": row.get("object_ids", [])}
                f.write(json.dumps(line, ensure_ascii=False) + "\n")


def write_run(run_dir: Path, cfg: RunConfig, rows: list[dict], metrics: dict) -> None:
    """Writes ``config.yaml``, ``results.jsonl`` and ``metrics.json`` into ``run_dir``.

    ``predictions.jsonl`` is added when ``cfg.eval.predictions`` is true.

    Args:
        run_dir: The run folder; created if missing.
        cfg: The resolved run configuration.
        rows: One dict per case and repeat (id, question, answer, intent, calls, metrics,
            latency_s, error ...).
        metrics: The aggregates returned by ``run_eval``.
    """
    write_config(run_dir, cfg)
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    if cfg.eval.predictions:
        write_predictions(run_dir, rows, cfg.eval.prediction_answer)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
