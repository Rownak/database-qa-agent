"""Prints a case x metric scorecard from the ADK eval history (Sprint 10, Phase 10.3).

Usage, from ``db-doc-qa/``::

    uv run python -m dev_test.phase_10_3 [history_dir]

``history_dir`` defaults to ``data_agent/.adk/eval_history``, where ``adk eval`` and the pytest gate write
one ``*.evalset_result.json`` per run. For each eval set the newest file is used. A case run several times
(``num_runs``) is one row showing its *worst* score per metric, so one bad run is not hidden by good ones.
"""

import sys
from pathlib import Path

from google.adk.evaluation.eval_case import get_all_tool_calls
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult
from google.adk.evaluation.evaluator import EvalStatus

HISTORY = Path(__file__).resolve().parent.parent / "data_agent" / ".adk" / "eval_history"
METRICS = ("tool_trajectory_avg_score", "tool_args_contain")  # ADK also reports informational metrics; not shown


def load_latest(history_dir: str | Path = HISTORY) -> list[EvalSetResult]:
    """Reads the newest result file of each eval set, ordered by eval set id."""
    newest: dict[str, EvalSetResult] = {}
    for path in sorted(Path(history_dir).glob("*.evalset_result.json")):
        result = EvalSetResult.model_validate_json(path.read_text(encoding="utf-8"))
        best = newest.get(result.eval_set_id)
        if best is None or result.creation_timestamp >= best.creation_timestamp:
            newest[result.eval_set_id] = result
    return [newest[k] for k in sorted(newest)]


def _tools(run: EvalCaseResult) -> str:
    """The tool names the agent called in the first invocation of one run, e.g. ``run_sql > search_documents``."""
    if not run.eval_metric_result_per_invocation:
        return "(no invocation)"
    calls = get_all_tool_calls(run.eval_metric_result_per_invocation[0].actual_invocation.intermediate_data)
    return " > ".join(c.name for c in calls) or "(none)"


def rows(results: list[EvalSetResult], metrics: tuple[str, ...] = METRICS) -> list[dict]:
    """Builds one row per eval case: runs, worst score per metric, pass/fail, actual tools and a note.

    The tools shown are those of the first failed run (the first run when all passed), so a failure shows
    what the agent really did. The note marks a case whose tools were right but whose arguments were not.
    """
    out = []
    for result in results:
        by_case: dict[str, list[EvalCaseResult]] = {}
        for run in result.eval_case_results:
            by_case.setdefault(run.eval_id, []).append(run)
        for eval_id, runs in by_case.items():
            scores = {
                m: min(
                    (r.score for run in runs for r in run.overall_eval_metric_results if r.metric_name == m and r.score is not None),
                    default=None,
                )
                for m in metrics
            }
            failed = next((r for r in runs if r.final_eval_status != EvalStatus.PASSED), None)
            passed = failed is None
            arg_scores = [scores[m] for m in metrics[1:] if scores[m] is not None]
            note = ""
            if all(v is None for v in scores.values()):
                note = "no scores (inference failed?)"
            elif scores[metrics[0]] == 1.0 and any(v < 1.0 for v in arg_scores):
                note = "tools right, arguments wrong"
            out.append(
                {"set": result.eval_set_id, "case": eval_id, "runs": len(runs), "scores": scores, "pass": passed,
                 "tools": _tools(failed or runs[0]), "note": note}
            )
    return out


def table(data: list[dict], metrics: tuple[str, ...] = METRICS) -> str:
    """Formats the rows as a fixed-width table."""
    head = ["set", "case", "runs", *[m.replace("tool_", "").replace("_avg_score", "") for m in metrics], "result", "actual tools", "note"]
    lines = [head]
    for r in data:
        lines.append(
            [r["set"], r["case"], str(r["runs"]), *["-" if r["scores"][m] is None else f"{r['scores'][m]:.2f}" for m in metrics],
             "PASS" if r["pass"] else "FAIL", r["tools"], r["note"]]
        )
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines)


def main(argv: list[str]) -> int:
    folder = Path(argv[0]) if argv else HISTORY
    results = load_latest(folder) if folder.is_dir() else []
    if not results:
        print(f"No eval results in {folder}. Run `adk eval` or the pytest gate first (see README).")
        return 1
    data = rows(results)
    print(table(data))
    print(f"\n{sum(r['pass'] for r in data)} of {len(data)} cases passed in every run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
