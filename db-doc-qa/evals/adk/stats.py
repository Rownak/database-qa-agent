"""Case-run extraction, pass@k statistics and verdicts for the ADK suites (Sprint 11, Phase 11.4; design §7).

Pure functions over ADK ``*.evalset_result.json`` files (read with ADK's own models) and over plain numbers,
so everything here is tested offline on hand-built result files. The gate is built on per-case success counts
and pass^k; pass@k is a diagnostic only (design §7.2).

Vocabulary: a *case-run* is one run of one eval case (a variant is a separate case). It is *valid* or
*invalid* (no verdict possible), and a valid one *succeeds* when every gating metric of every turn is passed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from google.adk.evaluation.eval_case import get_all_tool_calls, get_all_tool_responses
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult
from google.adk.evaluation.evaluator import EvalStatus

GATING = (
    "tool_trajectory_avg_score", "tool_args_contain", "answer_contains", "answer_excludes",
    "section_retrieved", "citation_grounded",
)
DEGRADED_NOTE = "Semantic search was unavailable"  # tools.search_documents: the embedding call failed, BM25 answered
MAX_INVALID_SHARE = 0.10
PASS_POW_K = 3
CLASS_ORDER = {"STABLE": 0, "FLAKY": 1, "BROKEN": 2}  # worse is larger; a variant group takes its worst member


# --- formulas ---

def pass_at_1(c: int, n: int) -> float:
    """Share of valid runs that succeeded: ``c / n`` (0.0 when there is no valid run)."""
    return c / n if n else 0.0


def pass_pow_k(c: int, n: int, k: int = PASS_POW_K) -> float:
    """Chance that ``k`` runs drawn without replacement all succeed: ``C(c, k) / C(n, k)``.

    With fewer than ``k`` valid runs the formula is taken at ``k = n`` (all runs succeeded or not).
    """
    if n == 0:
        return 0.0
    k = min(k, n)
    return math.comb(c, k) / math.comb(n, k)


def pass_at_k(c: int, n: int, k: int | None = None) -> float:
    """Chance that at least one of ``k`` drawn runs succeeds: ``1 - C(n - c, k) / C(n, k)`` (diagnostic only).

    ``k`` defaults to ``n``, the capability reading of design §7.2 (``c = 1, n = 5`` gives 1.0).
    """
    if n == 0:
        return 0.0
    k = min(k or n, n)
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def classify(c: int, n: int) -> str:
    """``STABLE`` (c = n), ``BROKEN`` (c = 0) or ``FLAKY``; ``NO_RESULT`` when there is no valid run."""
    if n == 0:
        return "NO_RESULT"
    return "STABLE" if c == n else "BROKEN" if c == 0 else "FLAKY"


# --- case-runs ---

@dataclass
class CaseRun:
    """The outcome of one run of one eval case.

    Attributes:
        case_id: The eval id (a variant has its own, ``<id>__vN``).
        valid: False when no verdict is possible (see ``invalid_reason``).
        success: Valid and every gating metric passed.
        invalid_reason: ``""`` when valid; else ``no metric results`` or ``degraded retrieval``.
        failing: Names of the gating metrics that failed.
        tools: The tools the agent called, per turn joined with `` | `` (turn: names joined with `` > ``).
        latency_s: Summed invocation duration reported by ADK, or ``None``.
        tokens: Token usage reported by ADK's informational metric, or ``None``.
    """

    case_id: str
    valid: bool
    success: bool
    invalid_reason: str = ""
    failing: list[str] = field(default_factory=list)
    tools: str = ""
    latency_s: float | None = None
    tokens: float | None = None


def _informational(run: EvalCaseResult, name: str) -> float | None:
    """Returns the score of one of ADK's informational metrics for a run, if reported."""
    return next((r.score for r in run.overall_eval_metric_results if r.metric_name == name and r.score is not None), None)


def _tools(run: EvalCaseResult) -> str:
    turns = []
    for per in run.eval_metric_result_per_invocation:
        calls = get_all_tool_calls(per.actual_invocation.intermediate_data)
        turns.append(" > ".join(c.name for c in calls) or "(none)")
    return " | ".join(turns)


def _degraded(run: EvalCaseResult) -> bool:
    """True when a ``search_documents`` response carries the BM25-fallback note (embeddings failed)."""
    for per in run.eval_metric_result_per_invocation:
        for response in get_all_tool_responses(per.actual_invocation.intermediate_data):
            if DEGRADED_NOTE in str(response.response):
                return True
    return False


def to_case_run(run: EvalCaseResult) -> CaseRun:
    """Classifies one ADK case result (design §7.1).

    Invalid: no gating metric produced a score (ADK records a model or API error, a timeout or a rate limit
    that way), no invocation was recorded, or a search fell back to BM25. Otherwise the run succeeds when no
    gating metric failed and ADK's final status is ``PASSED``.
    """
    gating = [r for r in run.overall_eval_metric_results if r.metric_name in GATING]
    common = dict(
        case_id=run.eval_id,
        tools=_tools(run),
        latency_s=_informational(run, "invocation_duration_v1"),
        tokens=_informational(run, "token_usage_v1"),
    )
    if not run.eval_metric_result_per_invocation or not any(r.score is not None for r in gating):
        return CaseRun(valid=False, success=False, invalid_reason="no metric results", **common)
    if _degraded(run):
        return CaseRun(valid=False, success=False, invalid_reason="degraded retrieval", **common)
    failing = sorted({r.metric_name for r in gating if r.eval_status == EvalStatus.FAILED})
    success = not failing and run.final_eval_status == EvalStatus.PASSED
    return CaseRun(valid=True, success=success, failing=failing, **common)


def collect_runs(history_dir: Path) -> dict[str, list[CaseRun]]:
    """Reads every ``*.evalset_result.json`` in a folder and groups the case-runs by eval id.

    A suite split into several eval sets (``adversarial``) writes one file per set; all are read.
    """
    runs: dict[str, list[CaseRun]] = {}
    for path in sorted(Path(history_dir).glob("*.evalset_result.json")):
        result = EvalSetResult.model_validate_json(path.read_text(encoding="utf-8"))
        for case_result in result.eval_case_results:
            run = to_case_run(case_result)
            runs.setdefault(run.case_id, []).append(run)
    return runs


# --- per case and per suite ---

@dataclass
class CaseStats:
    """The statistics of one eval case, or of a variant group (the worst member, design §3.2).

    Attributes:
        id: Case id, or the group id for a group.
        members: Eval ids it covers (more than one for a variant group).
        layer, rules, critical: From the manifest.
        n: Valid runs used (at most the requested ``n``). ``c``: successes among them.
        invalid: Case-runs that stayed invalid after the rerun (``requested n`` minus ``n``).
        invalid_first: Invalid case-runs seen before the rerun.
        pass1, pass3, passk: Statistics of design §7.2 (``passk`` uses ``k = n``).
        klass: ``STABLE``, ``FLAKY``, ``BROKEN`` or ``NO_RESULT``.
        failing: Gating metrics that failed in any valid run.
        tools: Actual tools of the first failed run (the first run when none failed).
    """

    id: str
    members: list[str]
    layer: str
    rules: list[int]
    critical: bool
    n: int
    c: int
    invalid: int
    invalid_first: int
    pass1: float
    pass3: float
    passk: float
    klass: str
    failing: list[str]
    tools: str
    worst_member: str


def case_stats(members: list[tuple[str, list[CaseRun], dict]], n_requested: int, group: str) -> CaseStats:
    """Builds the statistics of a case or variant group; the group is as bad as its worst member.

    Args:
        members: ``(eval_id, case_runs, manifest entry)`` for each member (a plain case has one).
        n_requested: The ``n`` of the tier. Valid runs beyond it are ignored.
        group: The group id.

    Returns:
        The group's :class:`CaseStats`, taken from the member with the worst class, then the lowest pass@1.
    """
    per_member = []
    for eval_id, runs, meta in members:
        valid = [r for r in runs if r.valid][:n_requested]
        c = sum(r.success for r in valid)
        n = len(valid)
        failed = next((r for r in valid if not r.success), None)
        shown = failed or (valid[0] if valid else (runs[0] if runs else None))
        per_member.append(
            CaseStats(
                id=group, members=[eval_id], layer=meta["layer"], rules=meta["rules"], critical=meta["critical"],
                n=n, c=c, invalid=max(0, n_requested - n), invalid_first=sum(not r.valid for r in runs),
                pass1=pass_at_1(c, n), pass3=pass_pow_k(c, n), passk=pass_at_k(c, n), klass=classify(c, n),
                failing=sorted({m for r in valid for m in r.failing}), tools=shown.tools if shown else "(no run)",
                worst_member=eval_id,
            )
        )
    worst = max(per_member, key=lambda s: (CLASS_ORDER.get(s.klass, 3), -s.pass1))
    worst.members = [m.members[0] for m in per_member]
    worst.invalid = sum(m.invalid for m in per_member)
    worst.invalid_first = sum(m.invalid_first for m in per_member)
    worst.failing = sorted({f for m in per_member for f in m.failing})
    return worst


@dataclass
class Tier:
    """Gate settings of a tier (``suites.yaml``)."""

    name: str
    n: int
    min_success: int
    suite_mean_pass1: float | None = None
    judges: bool = False
    calibration: bool = False
    fail_on_regression: bool = False


@dataclass
class SuiteResult:
    """A suite's cases, counts and verdict.

    Attributes:
        status: ``PASS``, ``FAIL`` or ``INVALID`` (design §7.1 and §7.3).
        reasons: Why it is not a plain PASS, one line each.
        regressions: Group ids STABLE in the baseline and not stable now. ``fixed``: the reverse.
    """

    name: str
    cases: list[CaseStats]
    total_runs: int
    invalid_runs: int
    mean_pass1: float
    mean_pass3: float
    status: str
    reasons: list[str]
    latencies: list[float] = field(default_factory=list)
    tokens: float = 0.0
    regressions: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def judge_suite(
    name: str,
    cases: list[CaseStats],
    tier: Tier,
    total_runs: int,
    latencies: list[float] | None = None,
    tokens: float = 0.0,
    baseline: dict[str, str] | None = None,
) -> SuiteResult:
    """Applies the validity rules (§7.1) and the pass criteria (§7.3) to a suite.

    Args:
        name: Suite name.
        cases: Group-level statistics.
        tier: Thresholds.
        total_runs: Case-runs requested (eval cases times ``n``); the base of the invalid share.
        latencies: Per-run latencies for the report.
        tokens: Total tokens for the report.
        baseline: ``{group id: class}`` from the baseline report, or ``None``.

    Returns:
        A :class:`SuiteResult`. ``INVALID`` when more than 10 % of the runs stayed invalid or a case has fewer
        valid runs than ``min_success``: no pass/fail claim. Otherwise ``FAIL`` when a critical case is not
        STABLE, another case has ``c < min_success``, the mean pass@1 is under ``suite_mean_pass1``, or (tiers
        with ``fail_on_regression``) a case regressed against the baseline.
    """
    invalid = sum(c.invalid for c in cases)
    reasons: list[str] = []
    status = "PASS"
    if total_runs and invalid / total_runs > MAX_INVALID_SHARE:
        reasons.append(f"{invalid} of {total_runs} runs invalid after rerun (more than {MAX_INVALID_SHARE:.0%})")
        status = "INVALID"
    short = [c.id for c in cases if c.n < tier.min_success]
    if short:
        reasons.append(f"fewer than {tier.min_success} valid runs: {', '.join(short)}")
        status = "INVALID"
    mean1 = statistics.fmean(c.pass1 for c in cases) if cases else 0.0
    mean3 = statistics.fmean(c.pass3 for c in cases) if cases else 0.0
    regressions = [c.id for c in cases if baseline and baseline.get(c.id) == "STABLE" and c.klass in ("FLAKY", "BROKEN")]
    fixed = [c.id for c in cases if baseline and baseline.get(c.id) in ("FLAKY", "BROKEN") and c.klass == "STABLE"]
    if status == "PASS":
        for c in cases:
            if c.critical and c.klass != "STABLE":
                reasons.append(f"critical case {c.id} is {c.klass} ({c.c}/{c.n})")
            elif not c.critical and c.c < tier.min_success:
                reasons.append(f"{c.id} succeeded in {c.c}/{c.n} runs (needs {tier.min_success})")
        if tier.suite_mean_pass1 is not None and mean1 < tier.suite_mean_pass1:
            reasons.append(f"mean pass@1 {mean1:.2f} is below {tier.suite_mean_pass1:.2f}")
        if tier.fail_on_regression and regressions:
            reasons.append(f"regressed against the baseline: {', '.join(regressions)}")
        if reasons:
            status = "FAIL"
    return SuiteResult(
        name=name, cases=cases, total_runs=total_runs, invalid_runs=invalid, mean_pass1=mean1, mean_pass3=mean3,
        status=status, reasons=reasons, latencies=latencies or [], tokens=tokens, regressions=regressions, fixed=fixed,
    )


def tier_verdict(results: list[SuiteResult]) -> tuple[str, int]:
    """Combines suite statuses into ``(status, exit code)``.

    ``FAIL`` (exit 1) when any suite failed, even if another is INVALID: an observed regression is not hidden
    by missing evidence elsewhere. Else ``INVALID`` (exit 2) when any suite is INVALID, else ``PASS`` (exit 0).
    """
    statuses = {r.status for r in results}
    if "FAIL" in statuses:
        return "FAIL", 1
    if "INVALID" in statuses or not results:
        return "INVALID", 2
    return "PASS", 0


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0-100), or ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]
