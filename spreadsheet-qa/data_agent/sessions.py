"""Per-session temp folders for the Streamlit app (design section 7).

Each browser session owns one folder under ``<tmp>/adk_sheet_qa/<session_id>/`` that
holds its ``data.db``. Streamlit cannot tell when a tab closes, so cleanup is
opportunistic: everything is purged when the server process starts, and every rerun
sweeps folders that have not been touched for 30 minutes. There is no background
thread. The module has no Streamlit dependency, so it can be tested directly.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(tempfile.gettempdir()) / "adk_sheet_qa"
MAX_AGE_SECONDS = 30 * 60


def purge_all(root: Path = ROOT) -> None:
    """Deletes every session folder.

    The app runs this once per server process (via ``st.cache_resource``), so
    folders left behind by a previous run never accumulate.

    Args:
        root: Folder holding all session folders.

    Side Effects:
        Removes ``root`` and everything in it; failures are ignored.
    """
    shutil.rmtree(root, ignore_errors=True)


def new_session_dir(session_id: str, root: Path = ROOT) -> Path:
    """Creates the folder for a new session.

    Args:
        session_id: Unique id of the browser session.
        root: Folder holding all session folders.

    Returns:
        The created (empty) session folder.
    """
    path = root / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def touch(session_dir: Path) -> None:
    """Marks a session folder as recently used.

    Args:
        session_dir: The session folder; a missing folder is ignored.
    """
    try:
        session_dir.touch()
    except OSError:
        pass


def sweep_stale(
    max_age: float = MAX_AGE_SECONDS, root: Path = ROOT, now: float | None = None
) -> list[Path]:
    """Deletes session folders not touched for longer than ``max_age``.

    Catches sessions whose tab was closed. The caller touches its own folder first,
    so the active session is never removed.

    Args:
        max_age: Maximum age in seconds, measured from the folder's modified time.
        root: Folder holding all session folders.
        now: Current time in seconds; defaults to ``time.time()`` (for tests).

    Returns:
        The folders that were removed.
    """
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    for child in root.iterdir():
        try:
            stale = child.is_dir() and now - child.stat().st_mtime > max_age
        except OSError:
            continue
        if stale:
            end_session(child)
            removed.append(child)
    return removed


def end_session(session_dir: Path) -> None:
    """Deletes one session folder.

    Args:
        session_dir: The session folder to remove.

    Side Effects:
        Recursively deletes the folder; errors are ignored so a locked file never
        blocks the user.
    """
    shutil.rmtree(session_dir, ignore_errors=True)
