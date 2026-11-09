"""Headless runner of the ADK eval suites: one command, one verdict, an exit code (Sprint 11, Phase 11.4; design §5, §7, §8).

Run from ``db-doc-qa/``::

    uv run python -m evals.adk.suite --tier pr
    uv run python -m evals.adk.suite --tier nightly --baseline runs/adk/<ts>_nightly
    uv run python -m evals.adk.suite --suite memory --n 5
    uv run python -m evals.adk.suite --capture adv_urgent_write

For each selected suite (in registry order) it builds the agent from the suite's ``agent_config``, runs ADK's
``AgentEvaluator.evaluate(num_runs=n)`` with a results manager, catches the assertion a failing case raises,
and reads the result files that run wrote. Invalid case-runs (API errors, degraded retrieval) are rerun once.
:mod:`evals.adk.stats` turns the runs into per-case and per-suite verdicts and :mod:`evals.adk.report` writes
``runs/adk/<timestamp>_<tier>/``. Exit code: 0 all suites PASS, 1 a suite FAILED, 2 INVALID (no verdict).
Everything except :func:`adk_evaluator` is offline; tests pass a fake evaluator that writes synthetic results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from evals.adk import build, report
from evals.adk.stats import (
    CaseRun,
    SuiteResult,
    Tier,
    case_stats,
    collect_runs,
    judge_suite,
    tier_verdict,
)

ROOT = Path(__file__).resolve().parent.parent.parent  # db-doc-qa/
REGISTRY = ROOT / "evals" / "adk" / "suites.yaml"
RUNS_DIR = ROOT / "runs" / "adk"
HISTORY = Path("data_agent") / ".adk" / "eval_history"  # below the agents dir given to the results manager
MAX_RERUN = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuiteEntry(_Strict):
    """One suite of the registry: where its eval sets are, which agent config it runs with and in which tiers."""

    name: str
    path: str
    agent_config: str
    tiers: list[str]


class TierEntry(_Strict):
    """A tier of the registry: ``n`` runs per case and the gate thresholds (see :class:`evals.adk.stats.Tier`)."""

    n: int
    min_success: int
    suite_mean_pass1: float | None = None
    judges: bool = False
    calibration: bool = False
    fail_on_regression: bool = False


class Registry(_Strict):
    """``suites.yaml``: the app, its suites and its tiers."""

    agent: str
    suites: list[SuiteEntry]
    tiers: dict[str, TierEntry]


def load_registry(path: Path = REGISTRY) -> Registry:
    """Reads and validates the suite registry.

    Raises:
        ValueError: If a suite names a tier that is not defined, or a name or path is duplicated.
    """
    registry = Registry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    names = [s.name for s in registry.suites]
    if len(set(names)) != len(names):
        raise ValueError("duplicate suite name in the registry")
    for s in registry.suites:
        unknown = set(s.tiers) - set(registry.tiers)
        if unknown:
            raise ValueError(f"suite {s.name}: unknown tier(s) {sorted(unknown)}")
    return registry


def select(registry: Registry, tier: str, suites: list[str] | None = None) -> list[SuiteEntry]:
    """Returns the suites to run, in registry order: the named ones, else those registered for ``tier``.

    Raises:
        ValueError: If a name is not a suite, or the tier does not exist.
    """
    if tier not in registry.tiers:
        raise ValueError(f"unknown tier {tier!r} (known: {sorted(registry.tiers)})")
    if suites:
        unknown = set(suites) - {s.name for s in registry.suites}
        if unknown:
            raise ValueError(f"unknown suite(s) {sorted(unknown)}")
        return [s for s in registry.suites if s.name in suites]
    return [s for s in registry.suites if tier in s.tiers]


# --- running ADK ---

Evaluator = Callable[[SuiteEntry, Path, int, Path], None]
"""``evaluator(suite, eval_folder, num_runs, agents_dir)``: runs the eval sets below ``eval_folder`` and leaves
``*.evalset_result.json`` files in ``agents_dir / HISTORY``. The default is :func:`adk_evaluator`."""


@contextmanager
def agent_from_config(config_path: Path) -> Iterator[None]:
    """Makes ``data_agent.agent.root_agent`` the agent of ``config_path`` while the block runs.

    ``AgentEvaluator`` reads ``root_agent`` from the already imported module, so setting the environment
    variable alone is not enough (the Sprint 10 fixture pattern). The previous agent and variable are restored.
    """
    import data_agent.agent as agent_module
    from data_agent.config import CONFIG_ENV, load_agent_config

    previous_env, previous_agent = os.environ.get(CONFIG_ENV), agent_module.root_agent
    os.environ[CONFIG_ENV] = str(config_path)
    try:
        agent_module.root_agent = agent_module.build_agent(load_agent_config())
        yield
    finally:
        agent_module.root_agent = previous_agent
        if previous_env is None:
            os.environ.pop(CONFIG_ENV, None)
        else:
            os.environ[CONFIG_ENV] = previous_env


def adk_evaluator(suite: SuiteEntry, folder: Path, num_runs: int, agents_dir: Path) -> None:
    """The live evaluator: ADK ``AgentEvaluator.evaluate`` with a results manager rooted at ``agents_dir``.

    A failing case makes ADK raise ``AssertionError`` after the results are saved; that is the normal "some
    case failed" signal and is caught here, because the verdict is computed from the result files.
    """
    from google.adk.evaluation.agent_evaluator import AgentEvaluator
    from google.adk.evaluation.local_eval_set_results_manager import LocalEvalSetResultsManager

    with agent_from_config(ROOT / suite.agent_config):
        try:
            asyncio.run(
                AgentEvaluator.evaluate(
                    "data_agent",
                    str(folder),
                    num_runs=num_runs,
                    print_detailed_results=False,
                    app_name="data_agent",
                    eval_set_results_manager=LocalEvalSetResultsManager(str(agents_dir)),
                )
            )
        except AssertionError:
            pass


def filtered_copy(source: Path, ids: set[str], target: Path) -> int:
    """Copies the eval sets below ``source`` to ``target`` keeping only the cases in ``ids``.

    Each folder with a ``test_config.json`` and at least one kept case is copied with its config. Used to
    rerun invalid cases and to capture one case.

    Returns:
        The number of cases kept.
    """
    kept = 0
    for config in sorted(source.rglob("test_config.json")):
        folder = config.parent
        for test_file in folder.glob("*.test.json"):
            data = json.loads(test_file.read_text(encoding="utf-8"))
            cases = [c for c in data["eval_cases"] if c["eval_id"] in ids]
            if not cases:
                continue
            out = target / folder.relative_to(source)
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config, out / "test_config.json")
            (out / test_file.name).write_text(json.dumps({**data, "eval_cases": cases}, indent=2), encoding="utf-8")
            kept += len(cases)
    return kept


# --- one suite ---

def load_manifest(suite: SuiteEntry) -> dict:
    """Reads ``manifest.json`` of the suite (written by the builder) from its folder."""
    return json.loads((ROOT / suite.path / "manifest.json").read_text(encoding="utf-8"))


def run_suite(
    suite: SuiteEntry,
    tier: Tier,
    work_dir: Path,
    evaluator: Evaluator = adk_evaluator,
    baseline: dict[str, str] | None = None,
    folder: Path | None = None,
    manifest: dict | None = None,
) -> tuple[SuiteResult, Path]:
    """Runs one suite, reruns its invalid case-runs once and judges the result.

    Args:
        suite: The registry entry.
        tier: Gate settings; ``tier.n`` is the number of runs per case.
        work_dir: Scratch folder of the run; the suite uses ``work_dir / suite.name``.
        evaluator: Runs ADK (replaced by a fake in tests).
        baseline: ``{group id: class}`` of the baseline report, for regressions.
        folder: Override for the eval folder (default ``ROOT / suite.path``).
        manifest: Override for the manifest (default read from the suite folder).

    Returns:
        ``(result, history_dir)``: the verdict and the folder of the first pass's result files.
        The evaluator raising is recorded as a reason and leaves the runs missing, which makes the suite
        INVALID rather than crashing the run.
    """
    folder = folder or ROOT / suite.path
    manifest = manifest or load_manifest(suite)
    meta = {c["id"]: c for c in manifest["cases"]}
    base = Path(work_dir) / suite.name
    error = ""
    try:
        evaluator(suite, folder, tier.n, base)
    except Exception as exc:  # an infrastructure failure must end as INVALID, not as a crash
        error = f"evaluator failed: {type(exc).__name__}: {exc}"
    runs = collect_runs(base / HISTORY)

    for attempt in range(MAX_RERUN):
        short = {i: tier.n - sum(r.valid for r in runs.get(i, [])) for i in meta}
        short = {i: k for i, k in short.items() if k > 0}
        if not short:
            break
        rerun_in = base / f"rerun{attempt}_in"
        if filtered_copy(folder, set(short), rerun_in) == 0:
            break
        try:
            evaluator(suite, rerun_in, max(short.values()), base / f"rerun{attempt}")
        except Exception as exc:
            error = error or f"rerun failed: {type(exc).__name__}: {exc}"
        for case_id, case_runs in collect_runs(base / f"rerun{attempt}" / HISTORY).items():
            runs.setdefault(case_id, []).extend(case_runs)

    groups: dict[str, list[tuple[str, list[CaseRun], dict]]] = {}
    for eval_id, entry in meta.items():
        groups.setdefault(entry["group"], []).append((eval_id, runs.get(eval_id, []), entry))
    cases = [case_stats(members, tier.n, group) for group, members in groups.items()]
    valid_runs = [r for rs in runs.values() for r in rs if r.valid]
    result = judge_suite(
        suite.name, cases, tier, total_runs=len(meta) * tier.n,
        latencies=[r.latency_s for r in valid_runs if r.latency_s is not None],
        tokens=sum(r.tokens or 0.0 for r in valid_runs), baseline=baseline,
    )
    if error:
        result.reasons.append(error)
    return result, base / HISTORY


# --- a whole run ---

def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _model_of(config_path: Path) -> str:
    """The agent model named by an eval config (the default model when the file does not set one)."""
    from data_agent.config import ModelConfig

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return ((data.get("agent") or {}).get("model") or {}).get("agent") or ModelConfig().agent


def load_baseline(folder: Path) -> dict[str, str]:
    """Reads a baseline run folder and returns ``{group id: class}`` over all its suites."""
    data = json.loads((Path(folder) / "report.json").read_text(encoding="utf-8"))
    return {c["id"]: c["klass"] for s in data["suites"] for c in s["cases"]}


def coverage_gap_notes() -> list[str]:
    """Lists the coverage gaps of the golden set: known gaps, uncovered rules and failure modes without a negative case."""
    suites = build.load_all()
    uncovered, no_negative = build.coverage_gaps(suites)
    notes = [f"rule {r} (known)" for r in sorted(build.KNOWN_GAPS)]
    notes += [f"rule {r} (no case)" for r in uncovered] + [f"rule {r} (no negative case)" for r in no_negative]
    return notes


def run_tier(
    registry: Registry,
    tier_name: str,
    suites: list[SuiteEntry],
    n: int | None = None,
    baseline_dir: Path | None = None,
    out_dir: Path = RUNS_DIR,
    evaluator: Evaluator = adk_evaluator,
) -> tuple[dict, Path]:
    """Runs the selected suites in order, writes the report folder and returns ``(report, run_dir)``.

    Args:
        registry: The loaded registry.
        tier_name: Tier whose thresholds apply.
        suites: Suites to run, in order, one after another (to stay under the Gemini rate limits).
        n: Runs per case; default the tier's. ``min_success`` is capped at ``n``.
        baseline_dir: A previous run folder, for the regression list.
        out_dir: Parent of the new run folder ``<timestamp>_<tier>``.
        evaluator: Runs ADK (replaced by a fake in tests).
    """
    entry = registry.tiers[tier_name]
    runs_per_case = n or entry.n
    tier = Tier(
        name=tier_name, n=runs_per_case, min_success=min(entry.min_success, runs_per_case),
        suite_mean_pass1=entry.suite_mean_pass1, judges=entry.judges, calibration=entry.calibration,
        fail_on_regression=entry.fail_on_regression,
    )
    baseline = load_baseline(baseline_dir) if baseline_dir else None
    run_dir = Path(out_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}_{tier_name}"
    work = run_dir / "_work"
    results: list[SuiteResult] = []
    histories: dict[str, Path] = {}
    for suite in suites:
        result, history = run_suite(suite, tier, work, evaluator, baseline)
        results.append(result)
        histories[suite.name] = history
    status, code = tier_verdict(results)
    meta = {
        "tier": tier_name, "n": runs_per_case, "min_success": tier.min_success, "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "commit": _git_commit(), "instruction_sha": build.current_instruction_sha(),
        "golden_sha": {s.name: load_manifest(s).get("instruction_sha", "?") for s in suites},
        "models": [_model_of(ROOT / s.agent_config) for s in suites],
        "suites": [s.name for s in suites], "baseline": str(baseline_dir) if baseline_dir else None,
        "judges": tier.judges,
    }
    full = report.build_report(meta, results, status, code, coverage_gap_notes())
    report.write_report(run_dir, full, histories)
    shutil.rmtree(work, ignore_errors=True)  # the raw result files were copied to eval_history/
    return full, run_dir


def capture(registry: Registry, case_id: str, evaluator: Evaluator = adk_evaluator) -> str:
    """Runs one golden case once and describes what the agent did, to compare with the YAML (design §4.3 step 3).

    Returns:
        Text with, per turn, the user message, the tool calls with arguments and the final answer. The
        golden YAML is not changed: a difference is resolved by reading the instruction rule.

    Raises:
        ValueError: If no suite has that case.
    """
    suite = next((s for s in registry.suites if case_id in {c["id"] for c in load_manifest(s)["cases"]}), None)
    if suite is None:
        raise ValueError(f"no suite has a case {case_id!r}")
    from google.adk.evaluation.eval_case import get_all_tool_calls
    from google.adk.evaluation.eval_result import EvalSetResult

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "set"
        filtered_copy(ROOT / suite.path, {case_id}, folder)
        evaluator(suite, folder, 1, Path(tmp) / "out")
        lines = [f"capture of {case_id} (suite {suite.name}, one run)"]
        for path in sorted((Path(tmp) / "out" / HISTORY).glob("*.evalset_result.json")):
            for run in EvalSetResult.model_validate_json(path.read_text(encoding="utf-8")).eval_case_results:
                for n, per in enumerate(run.eval_metric_result_per_invocation, 1):
                    inv = per.actual_invocation
                    calls = "; ".join(f"{c.name}({json.dumps(dict(c.args or {}), ensure_ascii=False)})" for c in get_all_tool_calls(inv.intermediate_data))
                    answer = "".join(p.text or "" for p in (inv.final_response.parts if inv.final_response else []) if p.text)
                    lines += [f"turn {n} q: {inv.user_content.parts[0].text}", f"  tools: {calls or '(none)'}", f"  answer: {answer}"]
                lines.append(f"status: {run.final_eval_status.name}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, evaluator: Evaluator = adk_evaluator) -> int:
    """Command line entry point; returns the exit code (0 PASS, 1 FAIL, 2 INVALID or a usage error)."""
    parser = argparse.ArgumentParser(description="Run the ADK eval suites and print one verdict.")
    parser.add_argument("--tier", default="pr", help="pr | nightly | release (default pr)")
    parser.add_argument("--suite", action="append", help="run only this suite (repeatable); overrides the tier's list")
    parser.add_argument("--n", type=int, help="runs per case (default: the tier's)")
    parser.add_argument("--baseline", type=Path, help="a previous run folder to compare with")
    parser.add_argument("--capture", metavar="ID", help="run one golden case once and print what the agent did")
    parser.add_argument("--registry", type=Path, default=REGISTRY, help=argparse.SUPPRESS)
    parser.add_argument("--out-dir", type=Path, default=RUNS_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        if args.capture:
            print(capture(registry, args.capture, evaluator))
            return 0
        suites = select(registry, args.tier, args.suite)
        if not suites:
            raise ValueError(f"no suite is registered for tier {args.tier!r}")
        if args.n is not None and args.n < 1:
            raise ValueError("--n must be at least 1")
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    full, run_dir = run_tier(registry, args.tier, suites, args.n, args.baseline, args.out_dir, evaluator)
    print(report.dashboard(full))
    print(f"\nreport: {run_dir}")
    return full["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
