"""YAML-driven evaluation of the agent pipeline (Sprint 4, Phase 4.3).

Run with ``uv run python -m evals <config.yaml> [key=value ...]`` from ``db-doc-qa/``.
This package only calls the public functions of :mod:`data_agent`; it is not deployed
and not part of the UI image.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # db-doc-qa/: dataset and output paths are relative to it
