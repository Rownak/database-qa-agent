"""Builder: golden YAML -> committed ADK eval sets, with coverage and coupling checks (Sprint 11, Phase 11.1).

Run ``uv run python -m evals.adk.build`` from ``db-doc-qa/``. For every ``evals/golden/<suite>.yaml`` it
validates the cases (:func:`validate`) and writes, under ``evals/adk/<suite>/``:

* ``<suite>.test.json``: the ADK eval set, one invocation per turn. Tool fragments go in
  ``intermediate_data.tool_uses``; answer checks are typed ``rubrics`` (``ANSWER_CONTAINS``,
  ``ANSWER_EXCLUDES``, ``EXPECTED_SECTION``); the session state comes from the case's preset.
* ``test_config.json``: the criteria for the folder's match type.
* ``manifest.json``: per case the layer, rules, critical flag and variant group, which ADK's schema
  cannot hold.

A suite that needs both match types (``adversarial``) is split into ``exact/`` and ``in_order/`` folders.
The generated JSON is committed so ``adk eval`` and ``adk web`` work without this module; a test compares
:func:`render` with the committed files. The builder never calls a model. It also prints the rule coverage
matrix (design section 4.4).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from google.adk.evaluation.eval_set import EvalSet

from data_agent.agent import INSTRUCTION
from data_agent.ingest import SAMPLE_DOCS_DIR, IngestError, load_sample
from data_agent.tools import run_sql, search_documents
from evals.adk.metrics import ANSWER_CONTAINS, ANSWER_EXCLUDES, EXPECTED_SECTION
from evals.adk.golden import (
    GOLDEN_DIR,
    RULE_RANGE,
    STATES_DIR,
    Case,
    GoldenSuite,
    Layer,
    load_all,
    load_preset,
)

ROOT = Path(__file__).resolve().parent.parent.parent  # db-doc-qa/
ADK_DIR = ROOT / "evals" / "adk"
APP_NAME = "data_agent"
USER_ID = "evaluator"
KNOWN_TOOLS = {"run_sql": run_sql, "search_documents": search_documents}
SHA_LENGTH = 12

# Rules whose behaviour is a refusal, a not-found or a warning: each needs a negative or edge case.
FAILURE_MODE_RULES = (4, 5, 6, 7, 8, 10, 12, 13, 14)
NEGATIVE_LAYERS = (Layer.EDGE, Layer.ADVERSARIAL)
KNOWN_GAPS = {11: "60 rows never truncate and a SQL error cannot be forced without changing the agent"}
RULE_NAMES = {
    1: "route by intent", 2: "record -> policy", 3: "policy -> filter", 4: "no documents loaded",
    5: "record not found", 6: "no section for type", 7: "conflicting documents", 8: "user contradicts data",
    9: "ground every fact", 10: "unknown table/column", 11: "SQL error / truncation", 12: "read-only",
    13: "injected text", 14: "load notices",
}


class BuildError(ValueError):
    """Raised when the golden files are invalid; the message lists every problem found."""


def current_instruction_sha() -> str:
    """Hashes what the golden files are coupled to: ``INSTRUCTION`` and the tool signatures and docstrings.

    The golden set is the specification of the agent, so a change to any of these must be followed by a
    review of the golden files (design section 4.3). The same inputs are fingerprinted by the response cache.

    Returns:
        The first ``SHA_LENGTH`` hex digits of a sha256.
    """
    tools = "\n".join(f"{t.__name__}{inspect.signature(t)}\n{inspect.getdoc(t)}" for t in KNOWN_TOOLS.values())
    return hashlib.sha256(f"{INSTRUCTION}\n{tools}".encode("utf-8")).hexdigest()[:SHA_LENGTH]


def _b64_file(path: Path) -> dict:
    """Returns ``{name, b64}`` for a file, the form the UI stores in ``state["docs"]`` and ``state["uploads"]``."""
    return {"name": path.name, "b64": base64.b64encode(path.read_bytes()).decode()}


def resolve_state(preset_name: str, states_dir: Path = STATES_DIR) -> dict:
    """Builds the ADK ``session_input.state`` of a preset, inlining its files as base64.

    Args:
        preset_name: A preset in ``evals/golden/states/``.
        states_dir: Folder of the presets.

    Returns:
        The state dict. The ``default`` preset gives ``{}`` so ``ensure_data`` loads the bundled sample.

    Raises:
        ValueError: If the preset or one of its files is missing.
    """
    preset = load_preset(preset_name, states_dir)
    state = dict(preset.state)
    if preset.docs_files:
        docs: list[dict] = []
        for entry in preset.docs_files:
            if entry == "@sample_docs":
                docs += [_b64_file(p) for p in sorted(SAMPLE_DOCS_DIR.iterdir()) if p.is_file()]
            else:
                docs.append(_b64_file(_existing(ROOT / entry)))
        state["docs"] = docs
    if preset.upload_files:
        state["uploads"] = [_b64_file(_existing(ROOT / entry)) for entry in preset.upload_files]
    return state


def _existing(path: Path) -> Path:
    """Returns ``path`` or raises ``ValueError`` when the file is missing."""
    if not path.is_file():
        raise ValueError(f"state file not found: {path}")
    return path


def _format_value(value) -> str:
    """Formats one SQL cell as the text an answer would contain (``9``, not ``9.0``)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sql_facts(query: str) -> list[str]:
    """Runs a SELECT on the bundled data and returns the first column of every row as a fragment.

    Args:
        query: A SELECT (or WITH) statement over the table ``data``.

    Returns:
        One string per row, in row order.

    Raises:
        ValueError: If the statement is not a read, fails, or returns no value.
    """
    if query.lstrip().split(None, 1)[0].upper() not in ("SELECT", "WITH"):
        raise ValueError(f"contains_sql must be a SELECT: {query!r}")
    with tempfile.TemporaryDirectory() as tmp:
        loaded = load_sample(tmp)
        if isinstance(loaded, IngestError):
            raise ValueError(f"the bundled data did not load: {loaded.message}")
        # Read-only like the agent's own tool, so a bad statement cannot touch the file.
        uri = f"file:{Path(loaded.db_path).as_posix()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                rows = conn.execute(query).fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"contains_sql failed ({exc}): {query!r}") from exc
    facts = [_format_value(row[0]) for row in rows if row and row[0] is not None]
    if not facts:
        raise ValueError(f"contains_sql returned no value: {query!r}")
    return facts


def effective_match(suite: GoldenSuite, case: Case) -> str:
    """Returns the match type a case is scored with: its own ``match``, else the suite's."""
    return case.match or suite.match


def _tool_signature(case: Case) -> tuple:
    """The golden tools of every turn, including argument fragments, for the redundancy check."""
    return tuple(
        tuple((c.name, tuple(sorted(c.args.items()))) for c in turn.tools) for turn in case.turns
    )


def validate(suites: list[GoldenSuite], states_dir: Path = STATES_DIR) -> tuple[list[str], list[str]]:
    """Checks the golden suites beyond what the schema can express.

    Errors (the build refuses): an empty golden tool list without ``trajectory: any``, ``trajectory: any``
    where it cannot apply (an EXACT folder, or no empty turn), an unknown tool or tool argument, duplicate
    ids (also across suites and variant ids), an unknown state preset, a
    fragment both required and forbidden, and an unusable ``contains_sql``. Warnings (the build goes on):
    redundant cases (same layer, rules, match type and golden tools) and an ``instruction_sha`` that no
    longer matches the agent.

    Args:
        suites: The loaded golden suites.
        states_dir: Folder of the state presets.

    Returns:
        ``(errors, warnings)`` as lists of messages.
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    redundant: dict[tuple, str] = {}
    sha = current_instruction_sha()
    for suite in suites:
        if not sha.startswith(suite.instruction_sha):
            warnings.append(
                f"{suite.suite}: instruction_sha {suite.instruction_sha} is not the current {sha}; "
                "review the golden cases against INSTRUCTION and the tool docs, then update it"
            )
        for case in suite.cases:
            where = f"{suite.suite}/{case.id}"
            for cid in [case.id, *(f"{case.id}__v{i}" for i in range(1, len(case.variants) + 1))]:
                if cid in seen:
                    errors.append(f"{where}: duplicate id {cid!r} (also in {seen[cid]})")
                seen[cid] = suite.suite
            errors += _validate_case(suite, case, where, states_dir)
            key = (case.layer, tuple(sorted(case.rules)), effective_match(suite, case), _tool_signature(case))
            if key in redundant:
                warnings.append(f"{where}: redundant with {redundant[key]} (same layer, rules and golden tools)")
            else:
                redundant[key] = where
    return errors, warnings


def _validate_case(suite: GoldenSuite, case: Case, where: str, states_dir: Path) -> list[str]:
    """Returns the errors of one case (see :func:`validate`)."""
    errors: list[str] = []
    match = effective_match(suite, case)
    has_empty_turn = any(not t.tools for t in case.turns)
    if case.trajectory == "any" and match == "EXACT":
        errors.append(f"{where}: trajectory: any cannot be scored under EXACT (an empty list there means no tool call)")
    elif case.trajectory == "any" and not has_empty_turn:
        errors.append(f"{where}: trajectory: any, but no turn has an empty tool list")
    if match == "IN_ORDER" and has_empty_turn and case.trajectory != "any":
        errors.append(f"{where}: empty golden tool list under IN_ORDER matches every trajectory; state 'trajectory: any'")
    try:
        resolve_state(case.state, states_dir)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
    for n, turn in enumerate(case.turns, 1):
        for call in turn.tools:
            tool = KNOWN_TOOLS.get(call.name)
            if tool is None:
                errors.append(f"{where} turn {n}: unknown tool {call.name!r} (known: {sorted(KNOWN_TOOLS)})")
                continue
            params = set(inspect.signature(tool).parameters) - {"tool_context"}
            for arg in call.args.keys() - params:
                errors.append(f"{where} turn {n}: {call.name} has no argument {arg!r} (has: {sorted(params)})")
        for query in turn.contains_sql:
            try:
                sql_facts(query)
            except ValueError as exc:
                errors.append(f"{where} turn {n}: {exc}")
        clash = {f.lower() for f in turn.contains} & {f.lower() for f in turn.excludes}
        if clash:
            errors.append(f"{where} turn {n}: required and forbidden at once: {sorted(clash)}")
    return errors


def _rubric(kind: str, index: int, text: str) -> dict:
    """One ADK rubric. ``type`` selects the metric; the value sits in ``rubric_content.text_property``."""
    return {
        "rubric_id": f"{kind.lower()}_{index}",
        "rubric_content": {"text_property": text},
        "type": kind,
    }


def _turn_rubrics(turn) -> list[dict]:
    """Builds the typed rubrics of a turn: required fragments (plus ``contains_sql`` values), forbidden ones, sections."""
    contains = list(dict.fromkeys([*turn.contains, *(f for q in turn.contains_sql for f in sql_facts(q))]))
    rubrics = [_rubric(ANSWER_CONTAINS, i, text) for i, text in enumerate(contains, 1)]
    rubrics += [_rubric(ANSWER_EXCLUDES, i, text) for i, text in enumerate(turn.excludes, 1)]
    rubrics += [_rubric(EXPECTED_SECTION, i, f"{s.source}|{s.section}") for i, s in enumerate(turn.sections, 1)]
    return rubrics


def _eval_case(case: Case, eval_id: str, last_question: str, state: dict) -> dict:
    """Builds one ADK eval case; ``last_question`` is the text of the last turn (a variant rewords it)."""
    conversation = []
    for n, turn in enumerate(case.turns, 1):
        invocation = {
            "invocation_id": f"inv_{n:03d}",
            "user_content": {"role": "user", "parts": [{"text": last_question if n == len(case.turns) else turn.q}]},
            "final_response": {"role": "model", "parts": [{"text": ""}]},  # the answer is judged by the rubrics
            "intermediate_data": {
                "intermediate_responses": [],
                "tool_uses": [{"name": c.name, "args": dict(c.args)} for c in turn.tools],
            },
        }
        rubrics = _turn_rubrics(turn)
        if rubrics:
            invocation["rubrics"] = rubrics
        conversation.append(invocation)
    return {
        "eval_id": eval_id,
        "conversation": conversation,
        "session_input": {"app_name": APP_NAME, "user_id": USER_ID, "state": state},
    }


GATING_METRICS = {  # custom metric (also the function name in evals.adk.metrics) -> description
    "tool_args_contain": "Each golden tool call was made with arguments containing the golden fragments.",
    "answer_contains": "Every required fragment (ANSWER_CONTAINS rubric) is in the final answer.",
    "answer_excludes": "No forbidden fragment (ANSWER_EXCLUDES rubric) is in the final answer.",
    "section_retrieved": "Each expected section (EXPECTED_SECTION rubric) was among the passages returned this turn.",
    "citation_grounded": "Every citation in the answer matches a passage returned this turn.",
}


def config_for(match: str) -> dict:
    """Returns the ``test_config.json`` content for a folder with this trajectory match type.

    It holds the built-in trajectory score and the five custom gating metrics, each with threshold 1.0.
    """
    return {
        "criteria": {
            "tool_trajectory_avg_score": {"threshold": 1.0, "match_type": match, "ignore_args": True},
            **{name: 1.0 for name in GATING_METRICS},
        },
        "custom_metrics": {
            name: {"code_config": {"name": f"evals.adk.metrics.{name}"}, "description": desc}
            for name, desc in GATING_METRICS.items()
        },
    }


def _dump(data: dict) -> str:
    """Serialises JSON the same way every time (2-space indent, trailing newline), for the stale check."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def render(suites: list[GoldenSuite], adk_dir: Path = ADK_DIR, states_dir: Path = STATES_DIR) -> dict[Path, str]:
    """Generates every output file without writing anything.

    Args:
        suites: Validated golden suites (see :func:`validate`).
        adk_dir: Output root, ``evals/adk/``.
        states_dir: Folder of the state presets.

    Returns:
        ``{path: file text}`` for each eval set, ``test_config.json`` and ``manifest.json``.

    Raises:
        ValueError: If a ``contains_sql`` or a state preset cannot be resolved.
    """
    files: dict[Path, str] = {}
    for suite in suites:
        matches = sorted({effective_match(suite, c) for c in suite.cases})
        eval_cases: dict[str, list[dict]] = defaultdict(list)
        manifest: list[dict] = []
        for case in suite.cases:
            match = effective_match(suite, case)
            folder = adk_dir / suite.suite if len(matches) == 1 else adk_dir / suite.suite / match.lower()
            state = resolve_state(case.state, states_dir)
            questions = [(case.id, case.turns[-1].q), *((f"{case.id}__v{i}", q) for i, q in enumerate(case.variants, 1))]
            for eval_id, question in questions:
                eval_cases[match].append(_eval_case(case, eval_id, question, state))
                manifest.append({
                    "id": eval_id,
                    "group": case.id,
                    "layer": case.layer.value,
                    "rules": case.rules,
                    "critical": case.critical,
                    "match": match,
                    "turns": len(case.turns),
                    "state": case.state,
                    "folder": folder.relative_to(adk_dir).as_posix(),
                })
        for match in matches:
            folder = adk_dir / suite.suite if len(matches) == 1 else adk_dir / suite.suite / match.lower()
            stem = suite.suite if len(matches) == 1 else f"{suite.suite}_{match.lower()}"
            eval_set = {
                "eval_set_id": stem,
                "name": stem,
                "description": suite.description,
                "eval_cases": eval_cases[match],
            }
            EvalSet.model_validate(eval_set)  # ADK's own model must accept what is written
            files[folder / f"{stem}.test.json"] = _dump(eval_set)
            files[folder / "test_config.json"] = _dump(config_for(match))
        files[adk_dir / suite.suite / "manifest.json"] = _dump(
            {"suite": suite.suite, "instruction_sha": suite.instruction_sha, "cases": manifest}
        )
    return files


def write(files: dict[Path, str]) -> None:
    """Writes the generated files, creating folders, with ``\\n`` line ends on every platform."""
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def stale_files(files: dict[Path, str]) -> list[Path]:
    """Returns the generated files whose committed content differs or that do not exist."""
    return [
        p for p, text in files.items()
        if not p.is_file() or p.read_bytes().decode("utf-8") != text
    ]


def coverage(suites: list[GoldenSuite]) -> dict[int, list[Case]]:
    """Maps each rule 1-14 to the (base) cases tagged with it; variants are not counted."""
    by_rule: dict[int, list[Case]] = {r: [] for r in RULE_RANGE}
    for suite in suites:
        for case in suite.cases:
            for rule in case.rules:
                by_rule[rule].append(case)
    return by_rule


def coverage_gaps(suites: list[GoldenSuite]) -> tuple[list[int], list[int]]:
    """Finds the rules the golden set does not cover.

    Returns:
        ``(uncovered, no_negative)``: rules with no case at all, and failure-mode rules
        (``FAILURE_MODE_RULES``) with cases but none in an edge or adversarial layer. Known gaps
        (``KNOWN_GAPS``) are left out of both.
    """
    by_rule = coverage(suites)
    uncovered = [r for r, cases in by_rule.items() if not cases and r not in KNOWN_GAPS]
    no_negative = [
        r for r in FAILURE_MODE_RULES
        if by_rule[r] and r not in KNOWN_GAPS and not any(c.layer in NEGATIVE_LAYERS for c in by_rule[r])
    ]
    return uncovered, no_negative


def format_matrix(suites: list[GoldenSuite]) -> str:
    """Formats the rule x case coverage matrix: counts per layer, the case ids, and the gaps."""
    by_rule = coverage(suites)
    layers = [la for la in Layer]
    head = f"{'rule':<5}{'meaning':<25}{'cases':>6}  " + " ".join(f"{la.value[:4]:>4}" for la in layers) + "  note"
    lines = [head, "-" * len(head)]
    for rule, cases in by_rule.items():
        counts = " ".join(f"{sum(c.layer is la for c in cases):>4}" for la in layers)
        note = f"KNOWN GAP: {KNOWN_GAPS[rule]}" if rule in KNOWN_GAPS else ("GAP: no case" if not cases else "")
        if rule in FAILURE_MODE_RULES and cases and not any(c.layer in NEGATIVE_LAYERS for c in cases):
            note = "GAP: no negative/edge case"
        lines.append(f"{rule:<5}{RULE_NAMES[rule]:<25}{len(cases):>6}  {counts}  {note}")
    return "\n".join(lines)


@dataclass
class BuildResult:
    """Outcome of :func:`build`: the generated files and the messages to show."""

    files: dict[Path, str]
    warnings: list[str]
    suites: list[GoldenSuite]


def build(golden_dir: Path = GOLDEN_DIR, adk_dir: Path = ADK_DIR, states_dir: Path = STATES_DIR) -> BuildResult:
    """Loads, validates and renders all golden suites (nothing is written).

    Raises:
        BuildError: If validation finds errors; the message lists them all.
    """
    suites = load_all(golden_dir)
    errors, warnings = validate(suites, states_dir)
    if errors:
        raise BuildError("\n".join(errors))
    return BuildResult(render(suites, adk_dir, states_dir), warnings, suites)


def main(argv: list[str] | None = None) -> int:
    """Command line entry: build and write the eval sets, print warnings and the coverage matrix.

    With ``--check`` nothing is written and the exit code is 1 when a committed file is stale.

    Returns:
        0 on success, 1 when the golden files are invalid or (``--check``) stale.
    """
    parser = argparse.ArgumentParser(description="Generate the ADK eval sets from evals/golden/*.yaml")
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 if a committed file is stale")
    args = parser.parse_args(argv)
    try:
        result = build()
    except BuildError as exc:
        print(f"Golden files are invalid:\n{exc}", file=sys.stderr)
        return 1
    stale = stale_files(result.files)
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}")
        return 1 if stale else 0
    write(result.files)
    for suite in result.suites:
        variants = sum(len(c.variants) for c in suite.cases)
        print(f"{suite.suite}: {len(suite.cases)} cases + {variants} variants ({suite.match})")
    print(f"wrote {len(stale)} changed file(s) of {len(result.files)}")
    for message in result.warnings:
        print(f"warning: {message}")
    print()
    print(format_matrix(result.suites))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
