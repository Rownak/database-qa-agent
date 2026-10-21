"""Tests for scripts/stage_ui_context.py (Phase 2.5): the staged folder matches what Dockerfile.ui copies."""

from pathlib import Path

from scripts import stage_ui_context

ROOT = Path(__file__).resolve().parent.parent


def _copy_sources() -> list[str]:
    """Source paths of the COPY lines in Dockerfile.ui (all but the last token of each)."""
    sources = []
    for line in (ROOT / "Dockerfile.ui").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            sources += line.split()[1:-1]
    return sources


def test_staged_folder_has_a_dockerfile_and_every_copied_source(tmp_path):
    out = stage_ui_context.stage(tmp_path / "ctx")
    assert (out / "Dockerfile").read_text() == (ROOT / "Dockerfile.ui").read_text()
    for src in _copy_sources():
        assert (out / src).exists(), f"Dockerfile.ui copies {src}, which was not staged"


def test_staged_folder_contains_only_the_ui_files(tmp_path):
    out = stage_ui_context.stage(tmp_path / "ctx")
    staged = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert staged == set(stage_ui_context.FILES) | {"Dockerfile"}
    assert not any(n.startswith(("tests/", ".env")) or n == "data_agent/agent.py" for n in staged)


def test_stage_replaces_previous_content(tmp_path):
    out = tmp_path / "ctx"
    out.mkdir()
    (out / "old.txt").write_text("x")
    stage_ui_context.stage(out)
    assert not (out / "old.txt").exists()


def test_staged_folder_has_the_sample_documents_the_ui_lists_at_start(tmp_path):
    """The UI reads the sample documents at session start; a missing folder crashed the deployed app."""
    from data_agent.ingest import SAMPLE_DOCS_DIR

    out = stage_ui_context.stage(tmp_path / "ctx")
    staged = {p.name for p in (out / "data_agent/sample_data/default/docs").iterdir()}
    assert staged == {p.name for p in SAMPLE_DOCS_DIR.iterdir()} and len(staged) == 3
