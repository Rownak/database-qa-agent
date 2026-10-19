"""File ingestion: validate an uploaded CSV/XLSX and load it into a session SQLite database.

This module is the first stage of the pipeline described in the design (section 5).
The Streamlit app hands it the raw bytes of an upload together with the session
directory. On success a single table named ``data`` exists in ``<session_dir>/data.db``
and the returned :class:`IngestResult` carries everything the UI and the agent need
(counts, schema summary, column-name map, preview). On failure an
:class:`IngestError` is returned and no database file is left behind.
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import warnings
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

TABLE_NAME = "data"
DB_FILENAME = "data.db"
MAX_FILE_BYTES = 10 * 1024 * 1024
PREVIEW_ROWS = 10
SAMPLE_FILE = Path(__file__).resolve().parent.parent / "sample_data" / "sales.csv"
SUPPORTED_EXTENSIONS = (".csv", ".xlsx")

_UNNAMED_RE = re.compile(r"^Unnamed: \d+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a successful load.

    Attributes:
        db_path: Path of the SQLite file holding table ``data``.
        row_count: Number of rows loaded.
        column_count: Number of columns loaded.
        column_map: Cleaned column name -> original header text.
        schema_summary: One line per column: ``name (type) — original name``.
        ignored_sheets: Names of workbook sheets that were not loaded (XLSX only).
        preview: First rows of the loaded table, with cleaned column names.
        sheet_name: Name of the loaded sheet (XLSX only), else ``None``.
    """

    db_path: Path
    row_count: int
    column_count: int
    column_map: dict[str, str]
    schema_summary: str
    ignored_sheets: list[str] = field(default_factory=list)
    preview: pd.DataFrame = field(default_factory=pd.DataFrame)
    sheet_name: str | None = None


@dataclass(frozen=True)
class IngestError:
    """Outcome of a failed load.

    Attributes:
        message: User-facing explanation of the first validation failure.
    """

    message: str


def load_file(
    uploaded_name: str, file_bytes: bytes, session_dir: str | Path
) -> IngestResult | IngestError:
    """Validates an uploaded file and loads it into ``<session_dir>/data.db``.

    Validation runs in a fixed order and stops at the first failure: extension,
    size, readability, non-emptiness, valid header row (present, non-blank,
    text rather than numbers, unique). Nothing is written to disk
    until every check has passed.

    Args:
        uploaded_name: Original file name, used only for its extension.
        file_bytes: Raw content of the upload.
        session_dir: Per-session directory. Created if missing, but only after
            validation succeeds.

    Returns:
        An :class:`IngestResult` on success, otherwise an :class:`IngestError`.

    Side Effects:
        On success, creates ``session_dir`` if needed and replaces ``data.db``
        in it atomically. On failure, the filesystem (including any existing
        ``data.db``) is untouched.
    """
    ext = Path(uploaded_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return IngestError("Unsupported file type. Upload a .csv or .xlsx file.")

    if len(file_bytes) > MAX_FILE_BYTES:
        return IngestError("File is too large. The limit is 10 MB.")

    read = _read_csv(file_bytes) if ext == ".csv" else _read_xlsx(file_bytes)
    if isinstance(read, IngestError):
        return read
    df, sheet_name, ignored_sheets = read

    df = df.dropna(how="all", axis=0).dropna(how="all", axis=1).reset_index(drop=True)
    if df.shape[0] < 1 or df.shape[1] < 1:
        return IngestError("The file has no data rows or no columns.")

    header_error = _check_headers(list(df.columns))
    if header_error:
        return header_error

    original_names = [str(c).strip() for c in df.columns]
    clean_names = [_clean_name(n) for n in original_names]  # unique: checked above
    df.columns = clean_names
    column_map = dict(zip(clean_names, original_names))

    df, labels, sql_types = _infer_types(df)

    db_path = _write_sqlite(df, sql_types, Path(session_dir))
    schema_summary = "\n".join(
        f"{name} ({labels[name]}) — {column_map[name]}" for name in clean_names
    )
    return IngestResult(
        db_path=db_path,
        row_count=len(df),
        column_count=df.shape[1],
        column_map=column_map,
        schema_summary=schema_summary,
        ignored_sheets=ignored_sheets,
        preview=df.head(PREVIEW_ROWS),
        sheet_name=sheet_name,
    )


def load_sample(session_dir: str | Path) -> IngestResult | IngestError:
    """Loads the bundled ``sample_data/sales.csv`` as a session's starting data.

    Every new session (Streamlit or ADK Web) starts from this so questions can be
    asked before any upload (design section 1).

    Args:
        session_dir: Per-session directory that will hold ``data.db``.

    Returns:
        The :func:`load_file` result for the sample file.

    Side Effects:
        Same as :func:`load_file`.
    """
    return load_file(SAMPLE_FILE.name, SAMPLE_FILE.read_bytes(), session_dir)


def _read_csv(file_bytes: bytes) -> tuple[pd.DataFrame, None, list[str]] | IngestError:
    """Parses CSV bytes (``utf-8-sig`` so a BOM is accepted).

    Args:
        file_bytes: Raw CSV content.

    Returns:
        ``(dataframe, None, [])`` or an :class:`IngestError`.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8-sig")
        raw = pd.read_csv(
            io.BytesIO(file_bytes), encoding="utf-8-sig", header=None, nrows=1, dtype=str
        )
        return _with_raw_headers(df, raw), None, []
    except pd.errors.EmptyDataError:
        return IngestError("The file has no data rows or no columns.")
    except Exception as exc:  # decode and parse errors vary widely; all are user-facing
        return IngestError(f"Could not read the file: {exc}.")


def _read_xlsx(
    file_bytes: bytes,
) -> tuple[pd.DataFrame, str, list[str]] | IngestError:
    """Reads the first worksheet of an XLSX workbook.

    Args:
        file_bytes: Raw XLSX content.

    Returns:
        ``(dataframe, loaded_sheet_name, ignored_sheet_names)`` or an
        :class:`IngestError`.
    """
    try:
        with pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl") as xl:
            first, *rest = xl.sheet_names
            df = xl.parse(first)
            raw = xl.parse(first, header=None, nrows=1, dtype=str)
            return _with_raw_headers(df, raw), first, rest
    except Exception as exc:  # corrupt zip, bad XML, etc.
        return IngestError(f"Could not read the file: {exc}.")


def _with_raw_headers(df: pd.DataFrame, raw_header: pd.DataFrame) -> pd.DataFrame:
    """Restores the header texts exactly as written in the file.

    pandas silently renames duplicate headers (``a``, ``a.1``), which would hide the
    real original names and break the ``_2``, ``_3`` suffix rule. Re-reading the
    first row untouched gives the true labels. Blank cells become ``Unnamed: N``.

    Args:
        df: Parsed data with pandas' mangled column labels.
        raw_header: One-row frame read with ``header=None, dtype=str``.

    Returns:
        ``df`` with its original header labels.
    """
    labels = [
        f"Unnamed: {i}" if pd.isna(v) else str(v)
        for i, v in enumerate(raw_header.iloc[0].tolist())
    ]
    out = df.copy()
    out.columns = labels
    return out


def _check_headers(columns: list) -> IngestError | None:
    """Checks that the file has a valid header row.

    Checks run in order and the first failure wins: no header row at all, a blank
    header, a numeric (non-string) header, then duplicate names.

    Args:
        columns: Header labels as restored by :func:`_with_raw_headers`, after
            fully empty columns were dropped.

    Returns:
        An :class:`IngestError` describing the problem, or ``None`` if valid.
    """
    labels = [str(c).strip() for c in columns]
    blank = [_UNNAMED_RE.match(t) is not None or not t for t in labels]
    if all(blank):
        return IngestError("No header row detected.")
    for pos, (text, is_blank) in enumerate(zip(labels, blank), start=1):
        # A header made only of symbols cleans to "" and is just as unusable.
        if is_blank or not _clean_name(text):
            return IngestError(f"Column {pos} has no header.")
        if _NUMBER_RE.match(text):
            return IngestError(
                f"Column {pos} header must be text, not a number: '{text}'."
            )
    # Compare cleaned names: "Order ID" and "order_id" would collide in SQL.
    first_seen: dict[str, int] = {}
    for pos, text in enumerate(labels, start=1):
        key = _clean_name(text)
        if key in first_seen:
            other = first_seen[key]
            return IngestError(
                f"Duplicate column name: '{labels[other - 1]}' (column {other}) "
                f"and '{text}' (column {pos})."
            )
        first_seen[key] = pos
    return None


def _clean_name(raw: str) -> str:
    """Turns one header into a SQL-friendly identifier.

    Lowercases, turns runs of non-alphanumerics into ``_``, strips edge underscores,
    and adds a ``c_`` prefix if the name starts with a digit. Returns ``""`` when
    nothing usable is left.

    Args:
        raw: Original header text.

    Returns:
        The cleaned identifier, or an empty string.
    """
    base = _NON_ALNUM_RE.sub("_", raw.strip().lower()).strip("_")
    return f"c_{base}" if base[:1].isdigit() else base


def _infer_types(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """Picks a type for every column and converts values to match.

    Numeric/bool dtypes are kept. Other columns try ``to_numeric`` first, then
    ``to_datetime`` (stored as ISO-8601 text, labeled ``date``), else stay ``TEXT``.
    The decision is all-or-nothing per column: one unparsable value keeps it text.

    Args:
        df: Dataframe with cleaned column names.

    Returns:
        ``(converted_df, display_label_by_column, sqlite_type_by_column)``.
    """
    df = df.copy()
    labels: dict[str, str] = {}
    sql_types: dict[str, str] = {}
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_bool_dtype(s):
            labels[col] = sql_types[col] = "INTEGER"
        elif pd.api.types.is_integer_dtype(s):
            labels[col] = sql_types[col] = "INTEGER"
        elif pd.api.types.is_float_dtype(s):
            labels[col] = sql_types[col] = "REAL"
        elif pd.api.types.is_datetime64_any_dtype(s):
            df[col] = _to_iso(s)
            labels[col], sql_types[col] = "date", "TEXT"
        else:
            converted, label = _infer_object_column(s)
            df[col] = converted
            labels[col] = label
            sql_types[col] = {"INTEGER": "INTEGER", "REAL": "REAL"}.get(label, "TEXT")
    return df, labels, sql_types


def _infer_object_column(s: pd.Series) -> tuple[pd.Series, str]:
    """Infers the type of a text-like column.

    Args:
        s: Column whose dtype is object/string (or otherwise not numeric/datetime).

    Returns:
        ``(converted_series, label)`` where label is ``INTEGER``, ``REAL``, ``date``
        or ``TEXT``.
    """
    non_null = s.dropna()
    if non_null.empty:
        return s, "TEXT"
    try:
        num = pd.to_numeric(s)
        return num, "INTEGER" if pd.api.types.is_integer_dtype(num) else "REAL"
    except (ValueError, TypeError):
        pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "could not infer format" noise
            dt = pd.to_datetime(s)
        return _to_iso(dt), "date"
    except (ValueError, TypeError, OverflowError):
        return s, "TEXT"


def _to_iso(s: pd.Series) -> pd.Series:
    """Formats datetimes as ISO-8601 text (date only when no value has a time part).

    Args:
        s: Datetime series.

    Returns:
        String series; missing values stay missing.
    """
    has_time = s.dropna().ne(s.dropna().dt.normalize()).any()
    out = s.dt.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")
    return out.astype(object).where(s.notna(), None)


def _write_sqlite(df: pd.DataFrame, sql_types: dict[str, str], session_dir: Path) -> Path:
    """Writes the dataframe to table ``data``, replacing ``data.db`` atomically.

    Args:
        df: Cleaned and type-converted data.
        sql_types: SQLite column type per column.
        session_dir: Directory to hold the database file.

    Returns:
        Path of the created database file.

    Side Effects:
        Creates ``session_dir``. The data goes to a temp file that is moved over
        ``data.db`` only after a successful write, so a failure (exception
        re-raised) leaves any existing ``data.db`` untouched.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = session_dir / DB_FILENAME
    tmp_path = session_dir / (DB_FILENAME + ".tmp")
    try:
        tmp_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(tmp_path)) as conn:
            df.to_sql(TABLE_NAME, conn, index=False, dtype=sql_types)
            conn.commit()
        os.replace(tmp_path, db_path)  # atomic; the connection above is already closed
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return db_path
