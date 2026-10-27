"""File ingestion: validate uploaded CSV/XLSX files and load them into a session SQLite database.

This module is the first stage of the pipeline described in the design (section 5).
The Streamlit app hands it the raw bytes of up to :data:`MAX_TABLES` uploads together
with the session directory. On success every file is a separate table in
``<session_dir>/data.db`` (named after the file, see :func:`table_name_for`) and the
returned :class:`LoadResult` carries everything the UI and the agent need (per-table
counts and preview, schema summary, column-name map). On failure an :class:`IngestError`
is returned and no database file is left behind. The bundled sample is one table, ``data``.
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

SAMPLE_TABLE = "data"  # generic on purpose: uploads can be any kind of table (finance, students, ...)
DB_FILENAME = "data.db"
MAX_FILE_BYTES = 500 * 1024
MAX_TABLES = 3
PREVIEW_ROWS = 10
# Inside the package so ``adk deploy`` (which uploads only this folder) ships it.
SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data" / "default"
SAMPLE_FILE = SAMPLE_DIR / "shipments.csv"
SAMPLE_DOCS_DIR = SAMPLE_DIR / "docs"  # default documents; loaded by the document pipeline
SUPPORTED_EXTENSIONS = (".csv", ".xlsx")

_UNNAMED_RE = re.compile(r"^Unnamed: \d+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class ParsedTable:
    """One validated file, ready to be written as a table.

    Attributes:
        file_name: Original file name.
        df: Cleaned, type-converted data (cleaned column names).
        labels: Display type per column (``INTEGER``, ``REAL``, ``date``, ``TEXT``).
        sql_types: SQLite type per column.
        column_map: Cleaned column name -> original header text.
        ignored_sheets: Workbook sheets that were not loaded (XLSX only).
        sheet_name: Name of the loaded sheet (XLSX only), else ``None``.
    """

    file_name: str
    df: pd.DataFrame
    labels: dict[str, str]
    sql_types: dict[str, str]
    column_map: dict[str, str]
    ignored_sheets: list[str] = field(default_factory=list)
    sheet_name: str | None = None


@dataclass(frozen=True)
class TableInfo:
    """What the UI shows about one loaded table.

    Attributes:
        file_name: File the table came from.
        table: Table name in ``data.db``.
        row_count: Number of rows loaded.
        column_count: Number of columns loaded.
        preview: First rows, with cleaned column names.
        ignored_sheets: Workbook sheets that were not loaded (XLSX only).
        sheet_name: Name of the loaded sheet (XLSX only), else ``None``.
    """

    file_name: str
    table: str
    row_count: int
    column_count: int
    preview: pd.DataFrame
    ignored_sheets: list[str] = field(default_factory=list)
    sheet_name: str | None = None


@dataclass(frozen=True)
class LoadResult:
    """Outcome of a successful load.

    Attributes:
        db_path: Path of the SQLite file holding the tables.
        tables: One :class:`TableInfo` per table, in upload order.
        column_map: Table name -> (cleaned column name -> original header text).
        schema_summary: One block per table (see :func:`schema_block`), blank-line separated.
    """

    db_path: Path
    tables: list[TableInfo]
    column_map: dict[str, dict[str, str]]
    schema_summary: str


@dataclass(frozen=True)
class IngestError:
    """Outcome of a failed load.

    Attributes:
        message: User-facing explanation of the first validation failure.
    """

    message: str


def read_table(uploaded_name: str, file_bytes: bytes) -> ParsedTable | IngestError:
    """Validates one uploaded file without writing anything.

    Validation runs in a fixed order and stops at the first failure: extension,
    size, readability, non-emptiness, valid header row (present, non-blank,
    text rather than numbers, unique).

    Args:
        uploaded_name: Original file name, used for its extension and as the source name.
        file_bytes: Raw content of the upload.

    Returns:
        A :class:`ParsedTable` on success, otherwise an :class:`IngestError`.
    """
    ext = Path(uploaded_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return IngestError("Unsupported file type. Upload a .csv or .xlsx file.")

    if len(file_bytes) > MAX_FILE_BYTES:
        return IngestError("File is too large. The limit is 500 KB.")

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
    return ParsedTable(
        file_name=Path(uploaded_name).name,
        df=df,
        labels=labels,
        sql_types=sql_types,
        column_map=column_map,
        ignored_sheets=ignored_sheets,
        sheet_name=sheet_name,
    )


def table_name_for(file_name: str, taken: set[str]) -> str:
    """Picks the table name for an uploaded file.

    The file stem is lower-cased with runs of non-alphanumerics turned into ``_``.
    A stem that is empty, starts with a digit or starts with ``sqlite_`` (reserved by
    SQLite) is prefixed with ``t_`` (an empty one becomes ``table``). A name already in
    ``taken`` gets ``_2``, ``_3``, ... so two files never share a table.

    Args:
        file_name: Original file name.
        taken: Table names already in use (lower-case).

    Returns:
        A unique, SQL-friendly table name.
    """
    base = _NON_ALNUM_RE.sub("_", Path(file_name).stem.lower()).strip("_") or "table"
    if base[0].isdigit() or base.startswith("sqlite_"):
        base = f"t_{base}"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def schema_block(table: str, parsed: ParsedTable) -> str:
    """Describes one table for the agent's instruction.

    Args:
        table: Table name in ``data.db``.
        parsed: The file loaded into it.

    Returns:
        A header line (``"table" — N rows, from file``) followed by one
        ``name (type) — original name`` line per column.
    """
    lines = [f'"{table}" — {len(parsed.df)} rows, from {parsed.file_name}']
    lines += [f"{c} ({parsed.labels[c]}) — {parsed.column_map[c]}" for c in parsed.df.columns]
    return "\n".join(lines)


def load_files(files: list[tuple[str, bytes]], session_dir: str | Path) -> LoadResult | IngestError:
    """Validates uploaded files and loads each into its own table of ``<session_dir>/data.db``.

    Args:
        files: ``(file_name, bytes)`` pairs, one to :data:`MAX_TABLES`.
        session_dir: Per-session directory. Created if missing, but only after
            validation succeeds.

    Returns:
        A :class:`LoadResult`, or an :class:`IngestError` for the first invalid file
        (prefixed with its name when there are several).

    Side Effects:
        See :func:`load_parsed`.
    """
    parsed = []
    for name, data in files:
        one = read_table(name, data)
        if isinstance(one, IngestError):
            return IngestError(f"{name}: {one.message}") if len(files) > 1 else one
        parsed.append(one)
    return load_parsed(parsed, session_dir)


def load_parsed(parsed: list[ParsedTable], session_dir: str | Path) -> LoadResult | IngestError:
    """Writes validated files as tables and replaces ``data.db`` atomically.

    Args:
        parsed: Results of :func:`read_table`, one to :data:`MAX_TABLES`.
        session_dir: Per-session directory.

    Returns:
        A :class:`LoadResult`, or an :class:`IngestError` when the count is out of range.

    Side Effects:
        On success, creates ``session_dir`` if needed and replaces ``data.db`` in it.
        On failure, the filesystem (including any existing ``data.db``) is untouched.
    """
    if not 1 <= len(parsed) <= MAX_TABLES:
        return IngestError(f"Load between 1 and {MAX_TABLES} data files.")
    taken: set[str] = set()
    names = []
    for p in parsed:
        names.append(table_name_for(p.file_name, taken))
        taken.add(names[-1])
    return _build_result(list(zip(names, parsed)), Path(session_dir))


def load_sample(session_dir: str | Path) -> LoadResult | IngestError:
    """Loads the bundled ``data_agent/sample_data/default/shipments.csv`` as table ``data``.

    Every new session (Streamlit or ADK Web) starts from this so questions can be
    asked before any upload (design section 1).

    Args:
        session_dir: Per-session directory that will hold ``data.db``.

    Returns:
        The load result for the sample file.

    Side Effects:
        Same as :func:`load_parsed`.
    """
    parsed = read_table(SAMPLE_FILE.name, SAMPLE_FILE.read_bytes())
    if isinstance(parsed, IngestError):
        return parsed
    return _build_result([(SAMPLE_TABLE, parsed)], Path(session_dir))


def _build_result(named: list[tuple[str, ParsedTable]], session_dir: Path) -> LoadResult:
    """Writes the named tables and assembles the :class:`LoadResult`."""
    db_path = _write_sqlite(named, session_dir)
    tables = [
        TableInfo(
            file_name=p.file_name,
            table=name,
            row_count=len(p.df),
            column_count=p.df.shape[1],
            preview=p.df.head(PREVIEW_ROWS),
            ignored_sheets=p.ignored_sheets,
            sheet_name=p.sheet_name,
        )
        for name, p in named
    ]
    return LoadResult(
        db_path=db_path,
        tables=tables,
        column_map={name: p.column_map for name, p in named},
        schema_summary="\n\n".join(schema_block(name, p) for name, p in named),
    )


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


def _write_sqlite(named: list[tuple[str, ParsedTable]], session_dir: Path) -> Path:
    """Writes the tables to a new database, replacing ``data.db`` atomically.

    Args:
        named: ``(table_name, parsed_file)`` pairs.
        session_dir: Directory to hold the database file.

    Returns:
        Path of the created database file.

    Side Effects:
        Creates ``session_dir``. The data goes to a temp file that is moved over
        ``data.db`` only after every table was written, so a failure (exception
        re-raised) leaves any existing ``data.db`` untouched.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = session_dir / DB_FILENAME
    tmp_path = session_dir / (DB_FILENAME + ".tmp")
    try:
        tmp_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(tmp_path)) as conn:
            for name, p in named:
                p.df.to_sql(name, conn, index=False, dtype=p.sql_types)
            conn.commit()
        os.replace(tmp_path, db_path)  # atomic; the connection above is already closed
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return db_path
