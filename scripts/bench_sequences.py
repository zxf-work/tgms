#!/usr/bin/env python3
"""What the sequence aggregates cost on top of the path they share (D-056).

The three new aggregates add a lexsort by (group, vt_s) and then array
arithmetic on adjacent pairs. What is at risk is the **slope**: a sort per
group rather than one sort, or a per-event search that walks a group instead
of bisecting it, would both show as super-linear growth in the event count
while looking fine at 100k.

Three conditions and not two, because a two-condition comparison attributes
nothing (D-054's lesson, paid for). `count` here is run through the *same*
`_portable` entry point as the sequence aggregates rather than through the
operator, which would route it to the native kernel: the scan, the mask, the
dimension coding and the grouping are then bit-identical between conditions
and the only difference left is the sequence walk itself.

    python3 scripts/bench_sequences.py --store stores/synth-10m \\
        --condition max_gap --frac 0.5 --reps 5

One condition per process, deliberately (engine_lessons §9g). The driver
loop is in the decision record.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from tgms.store import Store
from tgms.temporal.algebra import ensure_all_registered, validate_args
from tgms.temporal.ops_aggregate import _portable

DAY = 86_400_000_000
HOUR = 3_600_000_000

AGGREGATES = {
    "count": {"agg": "count"},
    "max_gap": {"agg": "max_gap"},
    "max_in_window": {"agg": "max_in_window", "span": DAY},
    "max_session_span": {"agg": "max_session_span", "gap": HOUR},
}


def _group_by(args: argparse.Namespace, t_a: int, t_b: int) -> list:
    if args.group == "src":
        return [{"dim": "endpoint", "role": "src"}]
    if args.group == "none":
        return []
    if args.group == "bucket":
        # 24 buckets, so the group count matches hour_of_day's and only the
        # code derivation differs between the two conditions
        return [{"dim": "time_bucket"}]
    return [{"dim": "calendar_unit", "unit": args.group}]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--condition", required=True, choices=sorted(AGGREGATES))
    p.add_argument("--frac", type=float, default=1.0,
                   help="fraction of the valid-time extent to scan")
    p.add_argument("--group", default="src",
                   choices=("src", "none", "bucket", "hour_of_day",
                            "day_of_week", "month_of_year"),
                   help="src = many small runs (one per sender); "
                        "none = one run holding every event, which is where "
                        "a sort that is really per-group would show; "
                        "bucket = a time_bucket stride, which is the honest "
                        "control for the calendar units (same event count, "
                        "same portable path, a different code per event)")
    p.add_argument("--reps", type=int, default=5)
    args = p.parse_args()

    ensure_all_registered()
    t0 = time.perf_counter()
    adapter = Store(args.store).adapter
    stats = adapter.stats()
    open_s = time.perf_counter() - t0

    lo, hi = stats["vt_min"], stats["vt_max"] + 1
    t_b = lo + int((hi - lo) * args.frac)
    call = validate_args("aggregate_events", {
        "window": {"t_a": lo, "t_b": max(t_b, lo + 1)},
        "group_by": _group_by(args, lo, t_b),
        "aggregates": [AGGREGATES[args.condition]],
        "limit": 10_000,
        **({"stride": max((t_b - lo) // 24, 1)}
           if args.group == "bucket" else {}),
    })

    times, rows_total, events = [], None, None
    for _ in range(args.reps):
        t0 = time.perf_counter()
        out = _portable(adapter, call)
        times.append((time.perf_counter() - t0) * 1000)
        rows_total = out["rows_total"]
    # the event count the condition actually walked, so the slope is against
    # a measured n rather than against the fraction we asked for
    probe = dict(call)
    probe["aggregates"] = [{"agg": "count", "of": None, "prop": None,
                            "span": None, "gap": None}]
    probe["group_by"] = []
    events = _portable(adapter, probe)["rows"][0]["count"]

    print(json.dumps({
        "store": args.store, "condition": args.condition, "frac": args.frac,
        "group": args.group,
        "events": events, "groups": rows_total, "reps": args.reps,
        "open_ms": round(open_s * 1000, 1),
        "median_ms": round(statistics.median(times), 1),
        "min_ms": round(min(times), 1), "max_ms": round(max(times), 1),
    }))


if __name__ == "__main__":
    main()
