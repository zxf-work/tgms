#!/usr/bin/env python3
"""What reading the belief log costs (D-058).

`version_history` is the first operator that goes through
`all_{node,edge}_versions()` — a full materialization of version rows as
Python objects — rather than through a columnar scan. Every other operator
reads `edges_columnar`, which returns struct-of-arrays and never builds a
row object. So what is at risk here is not the slope but the **constant**:
this operator may simply be in a different cost class from the rest of the
algebra, and if it is, that belongs in the record rather than in a surprise.

Three conditions, so the comparison attributes something:

    columnar   `aggregate_events` count over the same window — the scan
               every other operator pays, with no row objects at all
    current    `version_history` belief=current — the same population,
               materialized
    all        `version_history` belief=all — every version ever written

    python3 scripts/bench_versions.py --store stores/synth-1m \\
        --condition current --reps 5

One condition per process (engine_lessons §9g).
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from tgms.store import Store
from tgms.temporal.algebra import call_operator, ensure_all_registered


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--condition", required=True,
                   choices=("columnar", "current", "all"))
    p.add_argument("--frac", type=float, default=1.0)
    p.add_argument("--reps", type=int, default=5)
    args = p.parse_args()

    ensure_all_registered()
    t0 = time.perf_counter()
    adapter = Store(args.store).adapter
    stats = adapter.stats()
    open_s = time.perf_counter() - t0

    lo, hi = stats["vt_min"], stats["vt_max"] + 1
    window = {"t_a": lo, "t_b": max(lo + int((hi - lo) * args.frac), lo + 1)}
    if args.condition == "columnar":
        name, call = "aggregate_events", {
            "window": window, "group_by": [],
            "aggregates": [{"agg": "count"}], "limit": 10}
    else:
        name, call = "version_history", {
            "kind": "edge", "window": window, "belief": args.condition,
            "limit": 10}

    times, total = [], None
    for _ in range(args.reps):
        t0 = time.perf_counter()
        out = call_operator(adapter, name, dict(call))
        times.append((time.perf_counter() - t0) * 1000)
        total = out["rows_total"] if name == "version_history" \
            else out["rows"][0]["count"]

    print(json.dumps({
        "store": args.store, "condition": args.condition, "frac": args.frac,
        "rows_total": total, "reps": args.reps,
        "open_ms": round(open_s * 1000, 1),
        "median_ms": round(statistics.median(times), 1),
        "min_ms": round(min(times), 1), "max_ms": round(max(times), 1),
    }))


if __name__ == "__main__":
    main()
