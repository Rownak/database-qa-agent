"""Command line: ``uv run python -m evals <config.yaml> [key=value ...]`` (from ``db-doc-qa/``).

Example: ``uv run python -m evals configs/eval/smoke.yaml agent.retrieval.mode=bm25 name=smoke_bm25``
"""

from __future__ import annotations

import sys
from datetime import datetime

from data_agent.config import load_config
from evals.dataset import resolve
from evals.run import run_eval


def format_summary(metrics: dict) -> str:
    """Renders the aggregates as a short text table."""
    lines = [f"cases: {metrics['n']}  errors: {metrics['errors']}"]
    for name, value in metrics["metrics"].items():
        shown = "-" if value is None else f"{value:.2f}"
        lines.append(f"  {name:<22}{shown}")
    p50, p95 = metrics["latency_p50_s"], metrics["latency_p95_s"]
    if p50 is not None:
        lines.append(f"  latency p50 {p50:.1f}s  p95 {p95:.1f}s")
    for tag, part in metrics.get("by_tag", {}).items():
        values = "  ".join(f"{n}={v:.2f}" for n, v in part["metrics"].items() if v is not None)
        lines.append(f"  [{tag}] n={part['n']}  {values}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Loads the config, runs the evaluation into ``runs/<timestamp>_<name>/`` and prints the summary.

    Args:
        argv: Arguments after the program name; ``sys.argv[1:]`` when ``None``.

    Returns:
        The process exit code (2 for missing arguments, else 0).
    """
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m evals <config.yaml> [key=value ...]", file=sys.stderr)
        return 2
    cfg = load_config(args[0], args[1:])
    run_dir = resolve(cfg.eval.output_dir) / f"{datetime.now():%Y%m%d-%H%M%S}_{cfg.name}"
    metrics = run_eval(cfg, run_dir)
    print(format_summary(metrics))
    print(f"results: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
