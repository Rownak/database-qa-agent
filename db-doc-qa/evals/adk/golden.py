"""Schema and loader of the golden YAML files (Sprint 11, Phase 11.1).

Golden cases are authored in ``evals/golden/<suite>.yaml`` because ADK's eval-set JSON is verbose and
carries no case metadata. This module is the strict, typed view of those files and of the state presets in
``evals/golden/states/``. :mod:`evals.adk.build` turns the validated models into ADK eval sets, and the
offline tests use them for the coverage checks. Nothing here calls a model or reads the agent's answers: the
golden data is the specification, so it is written from the data and the documents (design section 4.3).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"
STATES_DIR = GOLDEN_DIR / "states"
RULE_RANGE = range(1, 15)  # the numbered rules of INSTRUCTION

Match = Literal["EXACT", "IN_ORDER"]


class Layer(str, Enum):
    """The behaviour layer of a case; it fixes the prefix of the case id."""

    UNIT = "unit"
    EDGE = "edge"
    MEMORY = "memory"
    ADVERSARIAL = "adversarial"
    REGRESSION = "regression"


ID_PREFIX = {
    Layer.UNIT: "unit_",
    Layer.EDGE: "edge_",
    Layer.MEMORY: "mem_",
    Layer.ADVERSARIAL: "adv_",
    Layer.REGRESSION: "reg_",
}


class _Strict(BaseModel):
    """Base of every golden model: unknown keys are errors, so a typo in a YAML file is caught."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Section(_Strict):
    """A policy section the turn's search must return (``EXPECTED_SECTION`` rubric)."""

    source: str = Field(min_length=1)
    section: str = Field(min_length=1)


class ToolCall(_Strict):
    """One golden tool call: the tool name and the argument fragments that must appear in its arguments."""

    name: str
    args: dict[str, str] = Field(default_factory=dict)


class Turn(_Strict):
    """One user message and what the agent must do and say in reply.

    Attributes:
        q: The user message.
        tools: Golden tool calls in order, written in YAML as ``- run_sql: {query: "12345"}``. An empty
            list means "no tool call" under EXACT and "anything" under IN_ORDER (see ``Case.trajectory``).
        contains: Fragments the final answer must contain.
        contains_sql: SELECT statements run on the bundled data by the builder; the first column of every
            returned row becomes a ``contains`` fragment.
        excludes: Fragments the final answer must not contain.
        sections: Policy sections that must be among the passages returned in this turn.
    """

    q: str = Field(min_length=1)
    tools: list[ToolCall] = Field(default_factory=list)
    contains: list[str] = Field(default_factory=list)
    contains_sql: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)

    @field_validator("tools", mode="before")
    @classmethod
    def _parse_tools(cls, value):
        """Accepts ``[{tool_name: {arg: fragment}}]`` and turns each entry into a :class:`ToolCall`."""
        if not isinstance(value, list):
            return value
        calls = []
        for entry in value:
            if isinstance(entry, dict) and set(entry) != {"name", "args"}:
                if len(entry) != 1:
                    raise ValueError(f"a tool entry must have exactly one key (the tool name), got {sorted(entry)}")
                (name, args), = entry.items()
                entry = {"name": name, "args": args or {}}
            calls.append(entry)
        return calls


class Case(_Strict):
    """One golden case: metadata for the coverage checks plus one or more turns.

    Attributes:
        id: Unique across all suites; starts with the prefix of ``layer`` (``unit_``, ``edge_``, ...).
        layer: Behaviour layer.
        rules: Numbers of the ``INSTRUCTION`` rules the case exercises (1-14).
        critical: A critical case must succeed in every run (design section 7.3).
        state: Name of a preset in ``evals/golden/states/``.
        variants: Paraphrases of the last turn (the only one of a single-turn case); each becomes a case
            ``<id>__vN`` with the same checks.
        trajectory: ``"any"`` states on purpose that a turn with an empty ``tools`` list accepts any tool
            use. Without it an empty list under IN_ORDER is rejected, because ADK scores it as a match for
            every trajectory.
        match: Overrides the suite's match type, for a suite that needs both (``adversarial``).
        turns: At least one.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    layer: Layer
    rules: list[int] = Field(min_length=1)
    critical: bool = False
    state: str = "default"
    variants: list[str] = Field(default_factory=list)
    trajectory: Literal["any"] | None = None
    match: Match | None = None
    turns: list[Turn] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _rules_in_range(cls, rules: list[int]) -> list[int]:
        """Rejects a rule number outside 1-14 and a rule listed twice."""
        bad = [r for r in rules if r not in RULE_RANGE]
        if bad or len(set(rules)) != len(rules):
            raise ValueError(f"rules must be distinct numbers from 1 to 14, got {rules}")
        return rules

    @model_validator(mode="after")
    def _id_matches_layer(self) -> Case:
        """Requires the layer prefix on the id (``unit_``, ``edge_``, ``mem_``, ``adv_``, ``reg_``)."""
        if not self.id.startswith(ID_PREFIX[self.layer]):
            raise ValueError(f"id {self.id!r} of a {self.layer.value} case must start with {ID_PREFIX[self.layer]!r}")
        return self


class GoldenSuite(_Strict):
    """The content of one ``evals/golden/<suite>.yaml`` file.

    Attributes:
        suite: Suite name; it names the output folder under ``evals/adk/``.
        instruction_sha: Prefix of the hash of ``INSTRUCTION`` and the tool docstrings when the file was
            last reviewed (see :func:`evals.adk.build.current_instruction_sha`).
        match: ADK trajectory match type of the suite's cases: ``EXACT`` or ``IN_ORDER``.
        description: Free text for the eval set.
        cases: The cases.
    """

    suite: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    instruction_sha: str = Field(pattern=r"^[0-9a-f]{6,64}$")
    match: Match
    description: str = ""
    cases: list[Case] = Field(min_length=1)


class StatePreset(_Strict):
    """A session-state preset (design section 4.2).

    Attributes:
        description: What the preset sets up.
        state: Literal state entries.
        docs_files: Files for ``state["docs"]``; the token ``@sample_docs`` stands for the bundled documents.
            Paths are relative to ``db-doc-qa/``. The builder inlines each file as ``{name, b64}``.
        upload_files: Files for ``state["uploads"]``, inlined the same way.
    """

    description: str = ""
    state: dict = Field(default_factory=dict)
    docs_files: list[str] = Field(default_factory=list)
    upload_files: list[str] = Field(default_factory=list)


def load_suite(path: Path) -> GoldenSuite:
    """Reads and validates one golden suite file.

    Args:
        path: A ``evals/golden/<suite>.yaml`` file.

    Returns:
        The validated suite.

    Raises:
        pydantic.ValidationError: If the file does not match the schema.
        ValueError: If the file name differs from the ``suite`` it declares.
    """
    suite = GoldenSuite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if suite.suite != path.stem:
        raise ValueError(f"{path.name}: declares suite {suite.suite!r}; the file must be named {suite.suite}.yaml")
    return suite


def load_preset(name: str, states_dir: Path = STATES_DIR) -> StatePreset:
    """Reads one state preset.

    Args:
        name: Preset name, the file name without ``.yaml``.
        states_dir: Folder of the presets.

    Raises:
        ValueError: If no such preset exists.
    """
    path = states_dir / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown state preset {name!r} (expected {path})")
    return StatePreset.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_all(golden_dir: Path = GOLDEN_DIR) -> list[GoldenSuite]:
    """Loads every ``*.yaml`` suite directly in ``golden_dir`` (not the presets), sorted by file name."""
    return [load_suite(p) for p in sorted(golden_dir.glob("*.yaml"))]
