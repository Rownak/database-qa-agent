"""Read-only agent tools over the session's SQLite database.

These four plain functions are registered on the ADK agent (design sections 4 and 6).
Each call opens its own read-only connection to the ``data.db`` path stored in the
session state, so the agent can never name a path or reach another session's data.
Every tool returns ``{"status": "success", ...}`` or
``{"status": "error", "error_message": "..."}`` and never raises to the agent.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from google.adk.tools import ToolContext

MAX_ROWS = 100
MAX_SAMPLE_ROWS = 20
MAX_CELL_CHARS = 200
QUERY_TIMEOUT_S = 5
TABLE_NAME = "data"
TOP_VALUES = 5

_BLOCKED_KEYWORDS = (
    "ATTACH", "PRAGMA", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
)
_BLOCKED_RE = re.compile(r"\b(" + "|".join(_BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)
_START_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_NUMERIC_TYPES = {"INTEGER", "REAL"}


def _error(message: str) -> dict[str, Any]:
    """Builds the standard error payload returned to the agent."""
    return {"status": "error", "error_message": message}


def _connect_ro(tool_context: ToolContext) -> sqlite3.Connection:
    """Opens a read-only connection to this session's database.

    The read-only mode is the real protection against writes; the SQL pre-check in
    :func:`run_sql` only gives friendlier messages. A progress handler aborts any
    query that runs longer than ``QUERY_TIMEOUT_S`` seconds.

    Args:
        tool_context: ADK context whose ``state["db_path"]`` points at ``data.db``.

    Returns:
        An open connection. Callers must wrap it in ``contextlib.closing``.

    Raises:
        RuntimeError: If no file has been loaded for this session.
        sqlite3.Error: If the database cannot be opened.
    """
    db_path = tool_context.state.get("db_path")
    if not db_path:
        raise RuntimeError("No file has been loaded for this session.")
    uri = f"file:{_as_uri_path(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    # A truthy return value from the handler interrupts the running statement.
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    return conn


def _as_uri_path(db_path: Any) -> str:
    """Converts a filesystem path to the path part of a SQLite ``file:`` URI.

    ``as_posix`` and percent-encoding keep the URI valid for Windows drive letters
    and for folder names containing spaces or ``?``/``#``.
    """
    return quote(Path(db_path).resolve().as_posix(), safe="/:")


def _columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Returns ``(name, declared_type)`` for each column of table ``data``."""
    info = conn.execute(f'PRAGMA table_info("{TABLE_NAME}")')
    return [(r[1], (r[2] or "").upper()) for r in info]


def _cell(value: Any) -> Any:
    """Trims long text so tool output stays small for the model."""
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _friendly(exc: Exception) -> str:
    """Maps an exception to a short message for the agent."""
    if isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower():
        return f"Query timed out after {QUERY_TIMEOUT_S} seconds. Simplify the query."
    return str(exc) or exc.__class__.__name__


def get_table_info(tool_context: ToolContext) -> dict:
    """Describes the uploaded table: its size and each column.

    Use this for the exact row count, column count, column names, original column
    names, and storage types.

    Returns:
        A dict with ``status``, ``row_count``, ``column_count`` and ``columns``, a
        list of ``{name, original_name, type}``. On failure, ``status`` is "error"
        with an ``error_message``.
    """
    try:
        column_map = tool_context.state.get("column_map") or {}
        with closing(_connect_ro(tool_context)) as conn:
            cols = _columns(conn)
            row_count = conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}"').fetchone()[0]
        return {
            "status": "success",
            "row_count": row_count,
            "column_count": len(cols),
            "columns": [
                {"name": n, "original_name": column_map.get(n, n), "type": t}
                for n, t in cols
            ],
        }
    except Exception as exc:  # tools must never raise to the agent
        return _error(_friendly(exc))


def get_sample_rows(n: int = 5, *, tool_context: ToolContext) -> dict:
    """Returns the first rows of the table as examples.

    Args:
        n: How many rows to return. Values above 20 are reduced to 20; values
            below 1 are raised to 1.

    Returns:
        A dict with ``status``, ``columns``, ``rows`` (lists of values) and
        ``row_count``. On failure, ``status`` is "error" with an ``error_message``.
    """
    try:
        n = max(1, min(int(n), MAX_SAMPLE_ROWS))
        with closing(_connect_ro(tool_context)) as conn:
            cur = conn.execute(f'SELECT * FROM "{TABLE_NAME}" LIMIT ?', (n,))
            names = [d[0] for d in cur.description]
            rows = [[_cell(v) for v in r] for r in cur.fetchall()]
        return {"status": "success", "columns": names, "rows": rows, "row_count": len(rows)}
    except Exception as exc:
        return _error(_friendly(exc))


def get_column_stats(column: str, *, tool_context: ToolContext) -> dict:
    """Summarizes a single column.

    Numeric columns get count, nulls, min, max, mean and median. Text and date
    columns get count, nulls, distinct count and the 5 most common values.

    Args:
        column: Cleaned column name, exactly as listed by ``get_table_info``.

    Returns:
        A dict with ``status`` and the statistics. For an unknown column,
        ``status`` is "error" and the message lists the valid columns.
    """
    try:
        with closing(_connect_ro(tool_context)) as conn:
            cols = dict(_columns(conn))
            if column not in cols:
                return _error(
                    f"Unknown column '{column}'. Valid columns: {', '.join(cols)}."
                )
            q = '"' + column.replace('"', '""') + '"'
            total, nulls = conn.execute(
                f'SELECT COUNT({q}), SUM({q} IS NULL) FROM "{TABLE_NAME}"'
            ).fetchone()
            nulls = nulls or 0  # SUM over zero rows is NULL
            out: dict[str, Any] = {
                "status": "success",
                "column": column,
                "type": cols[column],
                "count": total,
                "nulls": nulls,
            }
            if cols[column] in _NUMERIC_TYPES:
                mn, mx, mean = conn.execute(
                    f'SELECT MIN({q}), MAX({q}), AVG({q}) FROM "{TABLE_NAME}"'
                ).fetchone()
                out.update(min=mn, max=mx, mean=mean, median=_median(conn, q, total))
            else:
                distinct = conn.execute(
                    f'SELECT COUNT(DISTINCT {q}) FROM "{TABLE_NAME}"'
                ).fetchone()[0]
                top = conn.execute(
                    f'SELECT {q}, COUNT(*) AS n FROM "{TABLE_NAME}" WHERE {q} IS NOT NULL '
                    f"GROUP BY {q} ORDER BY n DESC, {q} LIMIT {TOP_VALUES}"
                ).fetchall()
                out.update(
                    distinct_count=distinct,
                    top_values=[{"value": _cell(v), "count": c} for v, c in top],
                )
            return out
    except Exception as exc:
        return _error(_friendly(exc))


def _median(conn: sqlite3.Connection, quoted_col: str, non_null: int) -> float | None:
    """Computes a column median in SQL (SQLite has no MEDIAN function).

    Args:
        conn: Open read-only connection.
        quoted_col: Column name already double-quoted.
        non_null: Number of non-NULL values in the column.

    Returns:
        The median, or ``None`` if the column has no values.
    """
    if not non_null:
        return None
    mid = conn.execute(
        f'SELECT {quoted_col} FROM "{TABLE_NAME}" WHERE {quoted_col} IS NOT NULL '
        f"ORDER BY {quoted_col} LIMIT ? OFFSET ?",
        (2 - non_null % 2, (non_null - 1) // 2),
    ).fetchall()
    return sum(r[0] for r in mid) / len(mid)


def _precheck(query: str) -> tuple[str | None, str]:
    """Validates a query before it reaches SQLite.

    Args:
        query: Query text from the agent.

    Returns:
        ``(error_message, cleaned_query)``; the message is ``None`` when the query
        passes. One trailing ``;`` is removed from the cleaned query.
    """
    q = query.strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if ";" in q:
        return "Only a single SQL statement is allowed.", q
    if not _START_RE.match(q):
        return "Only SELECT queries (optionally starting with WITH) are allowed.", q
    blocked = _BLOCKED_RE.search(q)
    if blocked:
        return f"The keyword {blocked.group(1).upper()} is not allowed; queries are read-only.", q
    return None, q


def run_sql(query: str, *, tool_context: ToolContext) -> dict:
    """Runs one read-only SQLite SELECT query against table ``data``.

    Use double quotes around cleaned column names. Only a single SELECT (or WITH ...
    SELECT) statement is accepted. At most 100 rows are returned.

    Args:
        query: The SQL query text.

    Returns:
        A dict with ``status``, ``columns``, ``rows``, ``row_count`` and
        ``truncated`` (True if more than 100 rows matched). On a rejected or failed
        query, ``status`` is "error" with an ``error_message``.
    """
    problem, cleaned = _precheck(query)
    if problem:
        return _error(problem)
    try:
        with closing(_connect_ro(tool_context)) as conn:
            cur = conn.execute(cleaned)
            names = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(fetched) > MAX_ROWS
        rows = [[_cell(v) for v in r] for r in fetched[:MAX_ROWS]]
        return {
            "status": "success",
            "columns": names,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except Exception as exc:
        return _error(_friendly(exc))
