"""Writes the files of one evaluation run (design section 5.4).

:func:`write_run` is the only place where results leave the runner, so a later tracker
(for example MLflow) can be called next to it without touching the run logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from data_agent.config import RunConfig

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


def write_run(run_dir: Path, cfg: RunConfig, rows: list[dict], metrics: dict) -> None:
    """Writes ``config.yaml``, ``results.jsonl`` and ``metrics.json`` into ``run_dir``.

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
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
