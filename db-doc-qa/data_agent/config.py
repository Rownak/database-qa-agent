"""Validated configuration for the agent pipeline and its evaluation (Sprint 4, Phase 4.1).

Every tunable setting that changes an answer lives here as a frozen Pydantic model whose
defaults equal the constants the agent used before (``AGENT_MODEL``, ``CHUNK_WORDS``, ...).
One YAML file maps to one :class:`RunConfig`; the ``agent:`` block of it is an
:class:`AgentConfig`, which is what :mod:`data_agent.agent`, :mod:`data_agent.doc_ingest`
and :mod:`data_agent.retrieval` receive. Settings are passed explicitly, never through a
module global, so one process can evaluate several configs.

This module is copied into the UI image, so it imports neither ADK nor ``yaml`` at module
level (``yaml`` is imported inside the loading functions).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_serializer

CONFIG_ENV = "DATA_AGENT_CONFIG"

MetricName = Literal[
    "intent_accuracy",
    "tool_sequence_match",
    "section_recall",
    "mrr",
    "citation_precision",
    "answer_contains",
    "answer_excludes",
    "sources_line",
    "sql_error_rate",
    "tool_calls",
    "answer_f1",
    "object_f1",
    "coverage",
    "exact_match",
    "squad_f1",
]
ALL_METRICS: list[str] = list(get_args(MetricName))
# the short-answer metrics are opt-in, so the default result files keep the keys they had before
DEFAULT_METRICS: list[str] = [m for m in ALL_METRICS if m not in ("exact_match", "squad_f1")]

_FROZEN = ConfigDict(extra="forbid", frozen=True)  # a YAML typo fails fast; a resolved config cannot change


class ModelConfig(BaseModel):
    """Gemini models used by the agent.

    Attributes:
        agent: Model id of the orchestrator agent.
        intent: Model id of the intent classifier; ``None`` means the same as ``agent``.
        temperature: Sampling temperature; ``None`` keeps the model default.
    """

    model_config = _FROZEN

    agent: str = "gemini-3.5-flash-lite"
    intent: str | None = None
    temperature: float | None = Field(None, ge=0, le=2)


class EmbeddingConfig(BaseModel):
    """Embedding model settings for document chunks and queries.

    Attributes:
        provider: ``gemini`` (the default) or ``ollama`` (a local server, for evaluations that
            would hit the Gemini embedding rate limit).
        model: Embedding model id (for example ``nomic-embed-text:latest`` with ``ollama``).
        dim: Output dimensionality; with ``ollama`` it must equal the model's vector size.
        batch: Texts per embedding request (the Gemini API accepts at most 100).
        base_url: Ollama server URL; unused by ``gemini``.
    """

    model_config = _FROZEN

    provider: Literal["gemini", "ollama"] = "gemini"
    model: str = "gemini-embedding-001"
    dim: int = Field(768, gt=0)
    batch: int = Field(100, gt=0, le=100)
    base_url: str = "http://localhost:11434"


class ChunkingConfig(BaseModel):
    """Document chunking settings.

    Attributes:
        chunk_words: Maximum words per chunk.
        fallback_overlap: Overlap ratio between chunks of documents without headings.
    """

    model_config = _FROZEN

    chunk_words: int = Field(160, gt=20)
    fallback_overlap: float = Field(0.15, ge=0, lt=0.5)


class RetrievalConfig(BaseModel):
    """Retrieval settings used by ``retrieval.search`` and ``search_documents``.

    Attributes:
        mode: ``hybrid`` (BM25 + dense, fused), ``bm25`` or ``dense``.
        top_n: Candidates taken from each ranker before fusion.
        rrf_k: Reciprocal Rank Fusion constant.
        min_dense_sim: Dense hits below this cosine similarity are dropped.
        max_top_k: Upper limit for the ``top_k`` the model may request.
        max_passage_chars: Passage text is cut to this length in tool responses.
    """

    model_config = _FROZEN

    mode: Literal["hybrid", "bm25", "dense"] = "hybrid"
    top_n: int = Field(20, gt=0)
    rrf_k: int = Field(60, gt=0)
    min_dense_sim: float = Field(0.62, ge=0, le=1)
    max_top_k: int = Field(8, gt=0, le=20)
    max_passage_chars: int = Field(1200, gt=0)


class AgentConfig(BaseModel):
    """Everything that changes an answer; recorded in the session state as ``agent_config``.

    Attributes:
        profile: Which instruction and description the agent gets: ``shipments`` (the
            default; the app and the deployed agent) or ``lakequest`` (concise answers
            over a multi-table lake, for the LakeQuest evaluation).
        instruction_file: A UTF-8 text file (relative to ``db-doc-qa/`` unless absolute) that
            replaces the instruction of ``profile``; the description stays the profile's. It
            uses the same state placeholders (``{schema_summary}``, ``{docs_summary}``,
            ``{load_error?}``). ``None`` keeps the built-in instruction.
        latency_trace: Diagnostic flag; it never changes an answer. When true, each turn records
            the duration of the stages that ran (session setup, intent, model calls, tools) in
            ``TurnResult.timing``. Off by default, and then nothing is recorded or read.
    """

    model_config = _FROZEN

    profile: Literal["shipments", "lakequest"] = "shipments"
    instruction_file: Path | None = None
    model: ModelConfig = Field(default_factory=ModelConfig)
    intent_enabled: bool = True  # False skips classification (ablation)
    latency_trace: bool = False
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    @field_serializer("instruction_file")
    def _posix(self, value: Path | None) -> str | None:
        return value.as_posix() if value is not None else None  # the saved config.yaml reads the same on every OS


class DatasetConfig(BaseModel):
    """Which evaluation dataset to run.

    Attributes:
        path: Dataset YAML, relative to ``db-doc-qa/``.
        case_ids: Run only these cases; empty means all.
        limit: Run at most this many cases.
    """

    model_config = _FROZEN

    path: Path
    case_ids: list[str] = Field(default_factory=list)
    limit: int | None = Field(None, gt=0)

    @field_serializer("path")
    def _posix(self, value: Path) -> str:
        return value.as_posix()  # the saved config.yaml reads the same on every OS


class EvalConfig(BaseModel):
    """How an evaluation runs and what it must reach.

    Attributes:
        mode: ``agent`` (full agent) or ``retrieval`` (search only, no LLM).
        metrics: Metrics to report.
        repeats: Runs per case.
        output_dir: Folder that holds the ``<timestamp>_<name>`` run folders.
        fail_under: Minimum mean per metric; a missed threshold makes the run exit non-zero.
        predictions: Also write ``predictions.jsonl`` (LakeQuest submission format).
        prediction_answer: Text written as ``answer`` in ``predictions.jsonl``: ``full`` (the
            final answer) or ``first_line`` (its first non-empty line, the text that
            ``exact_match`` scores).
    """

    model_config = _FROZEN

    mode: Literal["agent", "retrieval"] = "agent"
    metrics: list[MetricName] = Field(default_factory=lambda: list(DEFAULT_METRICS))
    repeats: int = Field(1, ge=1, le=5)
    output_dir: Path = Path("runs")
    fail_under: dict[MetricName, float] = Field(default_factory=dict)
    predictions: bool = False
    prediction_answer: Literal["full", "first_line"] = "full"

    @field_serializer("output_dir")
    def _posix(self, value: Path) -> str:
        return value.as_posix()


class RunConfig(BaseModel):
    """One YAML file: an agent configuration plus the evaluation that uses it."""

    model_config = _FROZEN

    name: str
    agent: AgentConfig = Field(default_factory=AgentConfig)
    dataset: DatasetConfig
    eval: EvalConfig = Field(default_factory=EvalConfig)


def _read_yaml(path: str | Path) -> dict[str, Any]:
    """Reads a YAML file that must hold a mapping (an empty file reads as ``{}``)."""
    import yaml  # lazy: a server that never reads YAML does not need it at import time

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the top level must be a mapping")
    return data


def _apply_override(data: dict[str, Any], override: str) -> None:
    """Sets one dotted ``key=value`` override in ``data`` (the value is parsed as YAML)."""
    import yaml

    key, sep, raw = override.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ValueError(f"invalid override {override!r}: expected dotted.key=value")
    node = data
    *parents, leaf = key.split(".")
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = yaml.safe_load(raw)


def load_config(path: str | Path, overrides: list[str] | None = None) -> RunConfig:
    """Loads a run configuration: model defaults < YAML file < dotted overrides.

    Args:
        path: YAML file with ``name``, ``dataset`` and optional ``agent`` / ``eval`` blocks.
        overrides: ``key=value`` strings such as ``agent.retrieval.min_dense_sim=0.55``.
            Each value is parsed with ``yaml.safe_load``, so ``0.55``, ``true`` and
            ``null`` get their types. Applied in order.

    Returns:
        The validated, frozen :class:`RunConfig`.

    Raises:
        pydantic.ValidationError: For an unknown key or an out-of-range value.
        ValueError: For a malformed override or a YAML file that is not a mapping.
    """
    data = _read_yaml(path)
    for override in overrides or []:
        _apply_override(data, override)
    return RunConfig.model_validate(data)


def load_agent_config() -> AgentConfig:
    """Returns the agent configuration of this process.

    Reads the ``agent:`` block of the file named by ``DATA_AGENT_CONFIG``. When the
    variable is unset (the deployed server), returns the defaults.

    Raises:
        pydantic.ValidationError: If the block holds an unknown key or a bad value.
    """
    path = os.environ.get(CONFIG_ENV)
    if not path:
        return AgentConfig()
    return AgentConfig.model_validate(_read_yaml(path).get("agent") or {})
