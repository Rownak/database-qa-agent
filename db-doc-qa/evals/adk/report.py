"""Report files and the dashboard of an ADK suite run (Sprint 11, Phase 11.4; design §8).

A run writes ``runs/adk/<timestamp>_<tier>/`` with ``report.json`` (the data), ``report.md`` (the dashboard
below, readable in a PR or an execution log) and ``eval_history/`` (copies of ADK's raw result files).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from evals.adk.stats import SuiteResult, percentile


def suite_json(result: SuiteResult) -> dict:
    """Turns a :class:`SuiteResult` into the ``report.json`` entry of a suite."""
    counts = {k: sum(c.klass == k for c in result.cases) for k in ("STABLE", "FLAKY", "BROKEN", "NO_RESULT")}
    critical = [c for c in result.cases if c.critical]
    return {
        "name": result.name,
        "status": result.status,
        "reasons": result.reasons,
        "cases": [asdict(c) for c in result.cases],
        "counts": counts,
        "critical": {"stable": sum(c.klass == "STABLE" for c in critical), "total": len(critical)},
        "critical_failures": [c.id for c in critical if c.klass != "STABLE"],
        "mean_pass1": round(result.mean_pass1, 4),
        "mean_pass3": round(result.mean_pass3, 4),
        "runs": result.total_runs,
        "invalid_runs": result.invalid_runs,
        "latency_p50_s": percentile(result.latencies, 50),
        "latency_p95_s": percentile(result.latencies, 95),
        "tokens": result.tokens,
        "regressions": result.regressions,
        "fixed": result.fixed,
    }


def build_report(meta: dict, results: list[SuiteResult], status: str, exit_code: int, coverage_gaps: list[str]) -> dict:
    """Assembles ``report.json``: run metadata, one entry per suite, the tier verdict and the coverage gaps."""
    return {
        "meta": meta,
        "suites": [suite_json(r) for r in results],
        "status": status,
        "exit_code": exit_code,
        "coverage_gaps": coverage_gaps,
    }


def _fmt(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def dashboard(report: dict) -> str:
    """Formats the dashboard: header, one row per suite, the unstable cases, regressions, gaps and cost."""
    meta = report["meta"]
    models = ", ".join(sorted(set(meta.get("models", []))))
    lines = [
        f"Tier {meta['tier']} · n={meta['n']} · commit {meta.get('commit', '?')} · instruction {meta.get('instruction_sha', '?')}"
        f" (golden {', '.join(sorted(set(meta.get('golden_sha', {}).values()))) or '?'}) · {models}",
        "",
    ]
    head = ["Suite", "Cases", "Stable", "Flaky", "Broken", "Critical", "pass@1", "pass^3", "Invalid", "Status"]
    rows = [head]
    for s in report["suites"]:
        c = s["counts"]
        rows.append([
            s["name"], str(sum(c.values())), str(c["STABLE"]), str(c["FLAKY"]), str(c["BROKEN"] + c["NO_RESULT"]),
            f"{s['critical']['stable']}/{s['critical']['total']}", f"{s['mean_pass1']:.2f}", f"{s['mean_pass3']:.2f}",
            str(s["invalid_runs"]), s["status"],
        ])
    rows.append(["TOTAL", "", "", "", "", "", "", "", "", f"{report['status']} (exit {report['exit_code']})"])
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    lines += ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]

    unstable = [(s["name"], c) for s in report["suites"] for c in s["cases"] if c["klass"] != "STABLE"]
    if unstable:
        lines += ["", "Not stable", f"{'case':<32}{'c/n':<7}{'failing metric':<30}actual tools (first failed run)"]
        for _, c in unstable:
            lines.append(f"{c['id']:<32}{str(c['c']) + '/' + str(c['n']):<7}{', '.join(c['failing']) or '-':<30}{c['tools']}")
    reasons = [(s["name"], r) for s in report["suites"] for r in s["reasons"]]
    if reasons:
        lines += ["", "Verdict reasons"] + [f"{name}: {reason}" for name, reason in reasons]
    baseline = meta.get("baseline")
    if baseline:
        regressed = [f"{c} (STABLE -> {next(x['klass'] for s in report['suites'] for x in s['cases'] if x['id'] == c)})"
                     for s in report["suites"] for c in s["regressions"]]
        fixed = [c for s in report["suites"] for c in s["fixed"]]
        lines += ["", f"Regressions vs baseline {baseline}: {', '.join(regressed) or 'none'}"]
        if fixed:
            lines.append(f"Fixed since baseline: {', '.join(fixed)}")
    gaps = report.get("coverage_gaps") or []
    lines.append(f"Coverage gaps: {', '.join(gaps) or 'none'}")
    runs = sum(s["runs"] for s in report["suites"])
    tokens = sum(s["tokens"] for s in report["suites"])
    p50 = [s["latency_p50_s"] for s in report["suites"] if s["latency_p50_s"] is not None]
    p95 = [s["latency_p95_s"] for s in report["suites"] if s["latency_p95_s"] is not None]
    lines.append(
        f"Cost: {runs} runs · {tokens / 1e6:.2f} M tokens · p50 {_fmt(max(p50) if p50 else None)} s · p95 {_fmt(max(p95) if p95 else None)} s"
    )
    return "\n".join(lines)


def write_report(run_dir: Path, report: dict, history_dirs: dict[str, Path]) -> None:
    """Writes ``report.json``, ``report.md`` and a copy of the raw ADK result files into ``run_dir``.

    Args:
        run_dir: The run folder (created).
        report: The dict from :func:`build_report`.
        history_dirs: ``{suite: folder of its *.evalset_result.json files}``; the files are copied to
            ``eval_history/<suite>__<file name>``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "report.md").write_text("```\n" + dashboard(report) + "\n```\n", encoding="utf-8")
    target = run_dir / "eval_history"
    target.mkdir(exist_ok=True)
    for suite, folder in history_dirs.items():
        for path in sorted(Path(folder).glob("*.evalset_result.json")):
            shutil.copy2(path, target / f"{suite}__{path.name}")
