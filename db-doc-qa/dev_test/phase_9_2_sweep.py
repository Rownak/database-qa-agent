"""Threshold sweep for the semantic response cache from a shadow run (Sprint 9, Phase 9.2).

Usage, from ``db-doc-qa/``::

    uv run python -m dev_test.phase_9_2_sweep runs/<shadow run> [runs/<baseline run>]

A shadow run (``response.semantic=true response.shadow=true``) records, for every miss, the nearest stored
question's similarity and entry id in ``results.jsonl``. This script needs no model call: for each threshold
from 0.90 to 0.99 it counts the paraphrase hits (pass P) and the false hits (pass N, and the unique pass),
with and without the entity guard. The entry id is the hash prefix of the stored question, so the nearest
entry is labelled with its case here, in the analysis, and never inside the cache.

Guard variants, recomputed from the question texts so they can be compared without a new run:
``off`` (similarity only), ``guard`` (the shipped entity guard), ``guard+ids`` (the same plus hyphenated
words such as policy ids).
"""

import re
import sys
from pathlib import Path

import numpy as np
import yaml

from data_agent import cache
from dev_test.phase_9_2 import load_run, of_pass, pcts

DATASET = Path(__file__).resolve().parent.parent.parent / "benchmark" / "lakequest_prepared" / "bank_sample_cache" / "dataset.yaml"
THRESHOLDS = [round(0.90 + i * 0.01, 2) for i in range(10)]
_HYPHENATED = re.compile(r"\b[a-z]+(?:-[a-z]+)+\b")


def _ids(text: str) -> frozenset[str]:
    return frozenset(_HYPHENATED.findall(text))


def canon(case_id: str) -> str:
    """The original case behind a derived case id (``<original>-para`` and so on)."""
    return re.sub(r"-(exact|para|near|stale)$", "", case_id)


def hits_at(rows: list[dict], questions: dict[str, str], by_entry: dict[str, str], tau: float, expected_of) -> dict:
    """Counts the rows a semantic tier would serve at ``tau`` under each guard variant.

    Args:
        rows: The rows of one pass.
        questions: Case id to question text (analysis only).
        by_entry: Entry id (question hash prefix) to the id of the case it was stored for.
        tau: Similarity threshold.
        expected_of: Maps a row id to the case id whose answer is the right one to serve, or ``None``.

    Returns:
        ``{variant: {"served", "right"}}``: how many rows are served and how many of those from the right case.
    """
    out = {v: {"served": 0, "right": 0} for v in ("off", "guard", "guard+ids")}
    for r in rows:
        shadow = (r.get("cache") or {}).get("shadow")
        if not shadow or shadow["similarity"] < tau:
            continue
        near = by_entry.get(shadow["nearest"])
        a, b = questions[r["id"]], questions.get(near, "")
        same = cache.entity_tokens(a) == cache.entity_tokens(b)
        variants = {"off": True, "guard": same, "guard+ids": same and _ids(a) == _ids(b)}
        for name, served in variants.items():
            if served:
                out[name]["served"] += 1
                out[name]["right"] += near is not None and canon(near) == expected_of(r["id"])
    return out


def main(argv: list[str]) -> int:
    """Prints the sweep table and the break-even check."""
    if not argv:
        print(__doc__)
        return 2
    rows = load_run(argv[0])
    spec = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    questions = {c["id"]: c["question"] for c in spec["cases"]}
    # every question can have been stored (an earlier paraphrase too), so label all of them
    by_entry = {cache.text_hash(q)[:12]: cid for cid, q in questions.items()}

    passes = {t: of_pass(rows, t) for t in ("pass_paraphrase", "pass_near_miss", "pass_unique")}
    n_p, n_n = len(passes["pass_paraphrase"]), len(passes["pass_near_miss"])
    with_shadow = {t: sum(bool((r.get("cache") or {}).get("shadow")) for r in rs) for t, rs in passes.items()}
    print(f"rows with a shadow neighbour: {with_shadow}")
    sims = sorted((r["cache"]["shadow"]["similarity"], r["id"]) for r in passes["pass_near_miss"] if (r.get("cache") or {}).get("shadow"))
    print("near-miss similarities:", [f"{s:.3f}" for s, _ in sims])
    psims = [r["cache"]["shadow"]["similarity"] for r in passes["pass_paraphrase"] if (r.get("cache") or {}).get("shadow")]
    print("paraphrase similarities (min/median/max):", f"{min(psims):.3f}/{np.median(psims):.3f}/{max(psims):.3f}" if psims else "-")
    print()
    print(f"{'tau':>5} | P served right/{n_p} (off  guard  g+ids) | N false/{n_n} (off  guard  g+ids) | U false (off  guard)")
    for tau in THRESHOLDS:
        p = hits_at(passes["pass_paraphrase"], questions, by_entry, tau, canon)
        n = hits_at(passes["pass_near_miss"], questions, by_entry, tau, lambda rid: None)
        u = hits_at(passes["pass_unique"], questions, by_entry, tau, lambda rid: None)
        print(
            f"{tau:5.2f} | {p['off']['right']:>10} {p['guard']['right']:>6} {p['guard+ids']['right']:>6}"
            f"{'':>10}| {n['off']['served']:>10} {n['guard']['served']:>6} {n['guard+ids']['served']:>6}"
            f"{'':>7}| {u['off']['served']:>8} {u['guard']['served']:>6}"
        )

    # break-even: semantic mode costs one embedding on every miss and gains T per hit
    lookups = [s["s"] for r in passes["pass_unique"] for s in (r.get("timing") or {}).get("stages", []) if s["stage"] == "cache_lookup"]
    if lookups and len(argv) > 1:
        t_lookup = float(np.median(lookups))
        base_u = pcts([r["latency_s"] for r in of_pass(load_run(argv[1]), "pass_unique") if not r["error"]])
        print(f"\nt_lookup (median cache_lookup on pass U) = {t_lookup:.3f} s; baseline pass U p50 T = {base_u[0]:.2f} s")
        print(f"break-even hit rate of the semantic tier = t_lookup / T = {t_lookup / base_u[0]:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
