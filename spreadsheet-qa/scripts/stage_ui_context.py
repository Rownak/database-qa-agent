"""Stages the build context for the UI image (Sprint 2, phase 2.5).

``gcloud run deploy --source`` only picks up a file named ``Dockerfile`` and has no flag
for another name, but this project's UI Dockerfile is ``Dockerfile.ui`` (the repo root
is the ADK agent's project). This script copies exactly the files ``Dockerfile.ui``
needs into ``.ui_build/`` and names the Dockerfile accordingly, so the upload contains
no tests, ``.env`` or backend code.

Run from ``spreadsheet-qa/``::

    uv run python scripts/stage_ui_context.py
    gcloud run deploy data-qa-ui --source .ui_build ...
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".ui_build"
# Mirrors the COPY lines in Dockerfile.ui; the test in tests/test_stage_ui_context.py keeps them in sync.
FILES = [
    "requirements-ui.txt",
    "app.py",
    "ui_client.py",
    ".streamlit/config.toml",
    "data_agent/ingest.py",
    "data_agent/sessions.py",
    "data_agent/sample_data/sales.csv",
]


def stage(out: Path = OUT) -> Path:
    """Copies the UI image's files into a clean folder.

    Args:
        out: Folder to create; any existing content is removed first.

    Returns:
        ``out``, which now holds ``Dockerfile`` plus the files in :data:`FILES`.

    Side Effects:
        Deletes and recreates ``out``.
    """
    shutil.rmtree(out, ignore_errors=True)
    for rel in FILES:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dest)
    shutil.copyfile(ROOT / "Dockerfile.ui", out / "Dockerfile")
    return out


if __name__ == "__main__":
    print(f"Staged UI build context in {stage()}")
