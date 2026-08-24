"""E14 P3 — the cost guard scored as a classifier at plan scope (§C3).

Derived from `benchmarks/results-v1/ldbc-sf1-campaign.json`, not measured
separately: the campaign already recorded, per plan, the estimate the frozen
policy produced, the admission decision that followed, and the wall time the
plan actually took with the guard bypassed. That is exactly D-086's
instrument — "a grid of calls with the guardrail *disabled but recording*,
every call also executed to ground truth, then the frontier computed by
sweeping the ceiling over the recorded estimates" — applied to core plans
instead of operator calls.

**Definitions, D-086's, unchanged.** For a wall-time budget `T`:

* **false admission** — the estimate passed the ceiling but the actual runtime
  exceeded `T`;
* **false rejection** — refused, but the actual runtime was within `T`.

`T` is the policy's declared budget (10 s) and stays fixed. The *ceiling* is
what the sweep moves. Holding one and moving the other is the whole point:
"what multiplier would this policy have needed?" is a different question from
"was this call fast?".

**The unit backstops are held fixed** while the time ceiling sweeps. They are
memory backstops (`rows_scanned_est`, `expansions_est`), not the primary axis,
and D-087 already set them 256x above the D-030-era values; sweeping them
together would conflate two policies.

**The two arms never merge.** Only the BI arm carries a third-party-parameter
claim; the Interactive arm's anchors are ours (§E addendum 4) and it is
reported beside, never inside.

    uv run python scripts/derive_p3_frontier.py \
        --record benchmarks/results-v1/ldbc-sf1-campaign.json \
        --out benchmarks/results-v1/e14-p3-frontier.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: Multipliers swept over the policy's time ceiling. Spans four orders either
#: side of the default because D-086's operator-scope optimum was 256x *above*
#: it and this run's is expected below — a sweep that could only find one of
#: those would be assuming the answer.
MULTIPLIERS = (0.01, 0.05, 0.1, 0.25, 0.5, 0.59, 0.75, 1.0, 2.0, 4.0, 8.0,
               16.0, 64.0, 256.0, 1024.0)


def scoreable(rec: dict[str, Any]) -> bool:
    """A row scores only if it ran to completion and has both numbers."""
    return (rec.get("outcome") == "COMPLETED"
            and rec.get("ms") is not None
            and rec.get("estimate", {}).get("time_est_ms") is not None)


def classify(est_ms: float, actual_ms: float, ceiling: float,
             budget: float) -> str:
    admitted = est_ms <= ceiling
    within = actual_ms <= budget
    if admitted:
        return "true-admission" if within else "false-admission"
    return "false-rejection" if within else "true-rejection"


def sweep(rows: list[dict[str, Any]], budget: float, base_ceiling: float,
          units: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for m in MULTIPLIERS:
        ceiling = base_ceiling * m
        counts = {"true-admission": 0, "false-admission": 0,
                  "false-rejection": 0, "true-rejection": 0}
        for r in rows:
            est = r["estimate"]["time_est_ms"]
            # a unit-axis breach refuses regardless of the time ceiling
            breached = any(r["estimate"].get(k, 0) > v for k, v in units.items())
            eff = float("inf") if breached else est
            counts[classify(eff, r["ms"], ceiling, budget)] += 1
        out.append({"multiplier": m, "ceiling_ms": ceiling, **counts})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", default=str(
        ROOT / "benchmarks/results-v1/ldbc-sf1-campaign.json"))
    ap.add_argument("--out", default=str(
        ROOT / "benchmarks/results-v1/e14-p3-frontier.json"))
    args = ap.parse_args()

    doc = json.loads(Path(args.record).read_text())
    man = doc["manifest"]
    budget = float(man["ceilings"]["time_est_ms"])
    units = {k: float(v) for k, v in man["ceilings"].items()
             if k != "time_est_ms"}

    print(f"source {Path(args.record).name}  commit {man['commit']}  "
          f"host {man['host']}  policy {man['policy_version']}")
    print(f"budget T = {budget:.0f} ms; unit backstops held fixed at {units}\n")

    report: dict[str, Any] = {"manifest": {
        "derived_from": str(Path(args.record)), "source_commit": man["commit"],
        "host": man["host"], "policy_version": man["policy_version"],
        "budget_ms": budget, "unit_backstops": units,
        "definitions": ("D-086's: false admission = estimate passed the "
                        "ceiling but actual exceeded T; false rejection = "
                        "refused but actual within T. T fixed, ceiling swept."),
        "arms_never_merged": True,
    }, "arms": {}}

    for arm in ("scored-bi", "characterization-interactive"):
        rows = [r for r in doc["records"] if r.get("arm") == arm]
        good = [r for r in rows if scoreable(r)]
        dropped = [r["plan_id"] for r in rows if not scoreable(r)]
        title = ("SCORED — BI, LDBC parameters" if arm == "scored-bi"
                 else "CHARACTERIZATION — Interactive, sampled anchors")
        print(f"=== {title} ===")
        print(f"scoreable {len(good)}/{len(rows)}"
              + (f"; excluded by name: {dropped}" if dropped else ""))

        ratios = [r["estimate"]["time_est_ms"] / r["ms"] for r in good
                  if r["ms"] > 0]
        under = [r["plan_id"] for r in good
                 if r["estimate"]["time_est_ms"] < r["ms"]]
        # `time_est_ms` is an int, so anything under a millisecond estimates to
        # literally 0 — which is not a small estimate, it is no estimate. Those
        # rows make the spread infinite, so it is reported as unbounded rather
        # than as a number that happens to be large.
        zeros = [r["plan_id"] for r in good
                 if r["estimate"]["time_est_ms"] == 0]
        lo, hi = min(ratios), max(ratios)
        spread = "unbounded (estimates of 0)" if lo == 0 else f"{hi/lo:,.0f}x"
        print(f"estimate/actual: min {lo:.3f}x  max {hi:,.0f}x"
              f"  median {statistics.median(ratios):.2f}x  spread {spread}")
        if zeros:
            print(f"estimated at 0 ms ({len(zeros)}): {zeros}")
        print(f"UNDER-estimates ({len(under)}/{len(good)}): {under}")

        table = sweep(good, budget, budget, units)
        print(f"\n{'mult':>7} {'ceiling ms':>12} {'TA':>4} {'FA':>4} "
              f"{'FR':>4} {'TR':>4}")
        for row in table:
            print(f"{row['multiplier']:>7g} {row['ceiling_ms']:>12,.0f} "
                  f"{row['true-admission']:>4} {row['false-admission']:>4} "
                  f"{row['false-rejection']:>4} {row['true-rejection']:>4}")
        clean = [r for r in table
                 if r["false-admission"] == 0 and r["false-rejection"] == 0]
        best = (max(clean, key=lambda r: r["multiplier"]) if clean else
                min(table, key=lambda r: (r["false-admission"],
                                          r["false-rejection"])))
        print(f"\nbest multiplier: {best['multiplier']:g}x "
              f"(FA {best['false-admission']}, FR {best['false-rejection']})"
              + ("" if clean else "  — no multiplier achieves FA=0 and FR=0"))
        print()

        report["arms"][arm] = {
            "scoreable": len(good), "of": len(rows), "excluded": dropped,
            "estimate_over_actual": {
                "min": min(ratios), "max": max(ratios),
                "median": statistics.median(ratios),
                "spread": (None if lo == 0 else hi / lo),
                "spread_note": (None if lo != 0 else
                                "unbounded: some estimates are 0 ms"),
                "under_estimates": under,
                "zero_estimates": zeros,
            },
            "sweep": table, "best": best,
            "per_plan": [{"plan_id": r["plan_id"],
                          "derived_admission": r["derived_admission"],
                          "est_ms": r["estimate"]["time_est_ms"],
                          "actual_ms": r["ms"], "rows": r.get("rows"),
                          "classifier": classify(
                              r["estimate"]["time_est_ms"], r["ms"],
                              budget, budget)} for r in good],
        }

    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True,
                                         default=str))
    print(f"record: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
