"""Compares two cache evaluation runs pass by pass (Sprint 9, Phase 9.2; reused by 9.4, 9.6 and 9.7).

Usage, from ``db-doc-qa/``::

    uv run python -m dev_test.phase_9_2 runs/<baseline> runs/<arm> [layer]

``layer`` is the cache layer whose hits are counted (default ``response``). Passes are never pooled:
each row of the table is one pass (the ``pass_*`` tags of the derived workload), with latency percentiles,
model usage and the hit rate, then the per-case quality difference against the baseline.
"""

import json
import sys
from pathlib import Path

import numpy as np

PASSES = ("pass_unique", "pass_exact", "pass_paraphrase", "pass_near_miss", "pass_stale")
GUARDED = ("answer_f1", "object_f1", "answer_contains", "coverage", "tool_sequence_match", "section_recall", "citation_precision")
QUALITY_TOLERANCE = 0.2  # one repeat of the model on the same case (design 7.5)


def load_run(folder: str | Path) -> list[dict]:
    """Reads the ``results.jsonl`` rows of a run folder."""
    path = Path(folder) / "results.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def outcome(row: dict, layer: str = "response") -> str | None:
    """The cache outcome of a row for one layer (``hit_exact``, ``miss`` ...), or ``None`` without a cache."""
    value = (row.get("cache") or {}).get(layer)
    return value if isinstance(value, str) else None


def is_hit(row: dict, layer: str = "response") -> bool:
    return (outcome(row, layer) or "").startswith("hit")


def pcts(values: list[float]) -> tuple[float, float, float] | None:
    """``(p50, p95, max)`` of the values, or ``None`` if empty."""
    if not values:
        return None
    return float(np.percentile(values, 50)), float(np.percentile(values, 95)), float(max(values))


def usage(rows: list[dict]) -> dict:
    """Model usage read from the traces: agent calls, classifier calls, input and output tokens, cache lookups."""
    stages = [s for r in rows for s in (r.get("timing") or {}).get("stages", [])]
    llm = [s for s in stages if s["stage"] == "llm"]
    return {
        "llm_calls": len(llm),
        "intent_calls": sum(s["stage"] == "intent" for s in stages),
        "in_tok": sum(s.get("in_tok", 0) for s in llm),
        "out_tok": sum(s.get("out_tok", 0) for s in llm),
        "lookup_s": [s["s"] for s in stages if s["stage"] == "cache_lookup"],
    }


def of_pass(rows: list[dict], tag: str, route: str | None = None) -> list[dict]:
    """The rows of one pass, optionally of one route (``hybrid`` / ``table`` / ``document``)."""
    return [r for r in rows if tag in r["tags"] and (route is None or route in r["tags"])]


def pass_summary(rows: list[dict], layer: str = "response") -> dict:
    """Latency, hit rate, model usage and mean guarded metrics of a set of rows."""
    ok = [r for r in rows if not r["error"]]
    means = {}
    for name in GUARDED:
        values = [r["metrics"][name] for r in rows if r["metrics"].get(name) is not None]
        means[name] = float(np.mean(values)) if values else None
    return {
        "n": len(rows), "errors": len(rows) - len(ok),
        "hits": sum(is_hit(r, layer) for r in rows),
        "latency": pcts([r["latency_s"] for r in ok]),
        "usage": usage(rows), "metrics": means,
    }


def quality_delta(base: list[dict], arm: list[dict], tag: str) -> dict:
    """Per-case difference of the guarded metrics between two runs on the cases of one pass.

    Returns:
        ``{"cases", "worse": ids whose metric dropped by more than the tolerance, "max_drop", "mean_delta"}``,
        where each case is compared with itself (same id) on every metric both runs scored.
    """
    a = {r["id"]: r for r in of_pass(base, tag)}
    worse, deltas = [], []
    for r in of_pass(arm, tag):
        other = a.get(r["id"])
        if other is None:
            continue
        for name in GUARDED:
            x, y = other["metrics"].get(name), r["metrics"].get(name)
            if x is None or y is None:
                continue
            deltas.append(y - x)
            if x - y > QUALITY_TOLERANCE and r["id"] not in worse:
                worse.append(r["id"])
    return {
        "cases": len(a), "worse": worse,
        "max_drop": float(-min(deltas)) if deltas else 0.0,
        "mean_delta": float(np.mean(deltas)) if deltas else 0.0,
    }


def _fmt_latency(p: tuple[float, float, float] | None) -> str:
    return "      -" if p is None else f"{p[0]:5.2f}/{p[1]:5.2f}/{p[2]:5.2f}"


def table(base: list[dict], arm: list[dict], layer: str = "response") -> str:
    """Renders the pass-by-pass comparison as text."""
    lines = [f"{'pass':<17}{'n':>3} {'hits':>5} | {'base p50/p95/max':>20} | {'arm p50/p95/max':>20} | "
             f"{'llm calls b>a':>14} {'in_tok b>a (k)':>16} | {'answer_f1 b>a':>14} | quality"]
    for tag in PASSES:
        b, a = pass_summary(of_pass(base, tag), layer), pass_summary(of_pass(arm, tag), layer)
        if not a["n"]:
            continue
        q = quality_delta(base, arm, tag)
        f1 = lambda s: "-" if s["metrics"]["answer_f1"] is None else f"{s['metrics']['answer_f1']:.2f}"  # noqa: E731
        lines.append(
            f"{tag:<17}{a['n']:>3} {a['hits']:>3}/{a['n']:<2}| {_fmt_latency(b['latency']):>20} | {_fmt_latency(a['latency']):>20} | "
            f"{b['usage']['llm_calls']:>6}>{a['usage']['llm_calls']:<6} "
            f"{b['usage']['in_tok'] / 1000:>7.0f}>{a['usage']['in_tok'] / 1000:<7.0f} | {f1(b):>6}>{f1(a):<6} | "
            f"worse {len(q['worse'])}/{q['cases']}, max drop {q['max_drop']:.2f}"
        )
        for route in ("hybrid", "table", "document"):
            rb, ra = of_pass(base, tag, route), of_pass(arm, tag, route)
            if ra:
                sb, sa = pass_summary(rb, layer), pass_summary(ra, layer)
                lines.append(f"  {route:<15}{sa['n']:>3} {sa['hits']:>3}/{sa['n']:<2}| {_fmt_latency(sb['latency']):>20} | {_fmt_latency(sa['latency']):>20} |")
    lookups = usage(arm)["lookup_s"]
    if lookups:
        lines.append(f"cache_lookup stage: n={len(lookups)} p50 {np.percentile(lookups, 50) * 1000:.1f} ms  max {max(lookups) * 1000:.1f} ms")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Prints the comparison of two run folders."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    print(table(load_run(argv[0]), load_run(argv[1]), argv[2] if len(argv) > 2 else "response"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
