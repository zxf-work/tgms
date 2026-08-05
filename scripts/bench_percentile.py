#!/usr/bin/env python3
"""What a percentile slice costs against the ranking it is a sibling of
(D-060).

`topk pct` is `topk k` with the count derived from the row count, so the
sort is identical and the only new work is one exact-rational multiply and
one slice. The risk is therefore not the slope — it is whether the exact
arithmetic, or `side: bottom`'s complement, quietly costs something per row.
The control is the operator it extends, at the same cardinality, which is
the only way this attributes anything.

Four conditions, all over the same grouped result so the input is fixed:

    group      the `aggregate_events` call that produces the rows — the
               work neither ranking is responsible for, priced so the two
               below can be read as increments over it
    topk       `k` = the count `pct` works out to: the existing operator,
               same rows out
    pct        `pct` = the fraction: the same sort, the count derived
    bottom     `pct` with `side: bottom`: the complement, which slices the
               far end of the same order

    python3 scripts/bench_percentile.py --store stores/collegemsg \\
        --condition pct --pct 10 --reps 5

One condition per process (engine_lessons §9g). xzgpu is the measurement
host; a run anywhere else is a sanity check, not a number for the record.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time

from tgms.store import Store
from tgms.temporal.algebra import call_operator, ensure_all_registered


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--condition", required=True,
                   choices=("group", "topk", "pct", "bottom"))
    p.add_argument("--pct", type=float, default=10.0)
    p.add_argument("--reps", type=int, default=5)
    args = p.parse_args()

    ensure_all_registered()
    adapter = Store(args.store).adapter
    stats = adapter.stats()
    window = {"t_a": stats["vt_min"], "t_b": stats["vt_max"] + 1}
    grouped = {"window": window, "aggregates": [{"agg": "count"}],
               "group_by": [{"dim": "endpoint", "role": "src"}],
               "limit": 10_000}

    if args.condition == "group":
        times = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            out = call_operator(adapter, "aggregate_events", dict(grouped))
            times.append((time.perf_counter() - t0) * 1000)
        n, taken = out["rows_total"], 0
    else:
        rows = call_operator(adapter, "aggregate_events", dict(grouped))["rows"]
        n = len(rows)
        # the same cardinality on every condition: k is exactly what pct
        # works out to, so `topk` is a control and not a different question
        k = math.ceil(n * args.pct / 100)
        call = {"fn": "topk", "input": rows, "field": "count", "limit": 10_000}
        call |= ({"k": k} if args.condition == "topk"
                 else {"pct": args.pct} if args.condition == "pct"
                 else {"pct": args.pct, "side": "bottom"})
        times = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            out = call_operator(adapter, "compute", dict(call))
            times.append((time.perf_counter() - t0) * 1000)
        taken = out["rows_total"]

    print(json.dumps({
        "store": args.store, "condition": args.condition, "pct": args.pct,
        "rows_in": n, "rows_out": taken, "reps": args.reps,
        "median_ms": round(statistics.median(times), 3),
        "min_ms": round(min(times), 3), "max_ms": round(max(times), 3),
    }))


if __name__ == "__main__":
    main()
