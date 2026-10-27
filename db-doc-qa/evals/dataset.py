"""Evaluation dataset models and loader (design section 5.1).

A dataset is one YAML file: a table, a document set and a list of questions with what
the answer should contain. A field of :class:`Expected` that is empty means "not checked",
and the metric that needs it returns ``None`` for that case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals import ROOT

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ExpectedSection(BaseModel):
    """A document section the retrieved passages should include.

    Attributes:
        source: Document file name.
        section: Section number; matches a passage's ``section_number`` exactly or as a
            prefix (``3.2`` matches ``3.2(a)-(c)``).
    """

    model_config = _STRICT

    source: str
    section: str


class Expected(BaseModel):
    """What a good answer looks like; every field is optional.

    Attributes:
        tools: Distinct tool names in first-call order; ``[]`` means no tool should be
            called; ``None`` skips the check.
        sections: Sections that should appear among the retrieved passages.
        answer_contains: Strings that should appear in the answer.
        reference_answers: Reference answers for ``answer_f1`` (the best match counts).
        object_ids: Gold object ids (policy ids and table names) for ``object_f1``.
    """

    model_config = _STRICT

    tools: list[Literal["run_sql", "search_documents"]] | None = None
    sections: list[ExpectedSection] = Field(default_factory=list)
    answer_contains: list[str] = Field(default_factory=list)
    reference_answers: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    """One question and its expectations.

    ``state_file`` is this case's own prebuilt session state (JSON, relative to the dataset
    file); it wins over the dataset's ``state_file``, which wins over ``table`` / ``docs``.
    """

    model_config = _STRICT

    id: str
    question: str
    tags: list[str] = Field(default_factory=list)
    state_file: Path | None = None
    expected: Expected = Field(default_factory=Expected)


class EvalDataset(BaseModel):
    """A named, versioned set of cases that share one table and one document set.

    Attributes:
        name: Dataset name, stored with the results.
        version: Dataset version; bump it when the cases change.
        table: Table file, relative to ``db-doc-qa/``; ``None`` is the bundled sample.
        docs: A folder or a list of files; ``None`` is the bundled samples, ``[]`` no documents.
        state_file: A prebuilt session state (JSON) that replaces ``table`` and ``docs``;
            written by ``evals.lakequest``; a case's own ``state_file`` takes precedence.
        cases: The cases; their ids must be unique.
    """

    model_config = _STRICT

    name: str
    version: int
    table: Path | None = None
    docs: Path | list[Path] | None = None
    state_file: Path | None = None
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _unique_ids(self) -> EvalDataset:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
        return self


def resolve(path: str | Path) -> Path:
    """Returns ``path`` as an absolute path; a relative one is taken from ``db-doc-qa/``."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def load_dataset(path: str | Path) -> EvalDataset:
    """Loads and validates a dataset YAML file.

    Args:
        path: File path, relative to ``db-doc-qa/`` unless absolute.

    Returns:
        The validated :class:`EvalDataset`.

    Raises:
        pydantic.ValidationError: For an unknown key, a bad value or a duplicate case id.
    """
    import yaml

    return EvalDataset.model_validate(yaml.safe_load(resolve(path).read_text(encoding="utf-8")))


def dataset_files(ds: EvalDataset) -> tuple[Path | None, list[Path] | None]:
    """Turns the dataset's ``table`` and ``docs`` into the arguments of ``files_state``.

    Args:
        ds: A loaded dataset.

    Returns:
        ``(table, docs)``: absolute paths, with ``None`` kept as "use the samples" and a
        folder expanded to its files (sorted).
    """
    table = resolve(ds.table) if ds.table is not None else None
    if ds.docs is None:
        return table, None
    if isinstance(ds.docs, list):
        return table, [resolve(p) for p in ds.docs]
    folder = resolve(ds.docs)
    return table, sorted(p for p in folder.iterdir() if p.is_file())
