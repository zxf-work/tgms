#!/usr/bin/env python3
"""Cold/warm manifest-chain open time, component-scored (B1(c)).

`benchmarks/results-v1/b1-manifest-v2-ab-2026-09.README.md` §B1(c) measured
this by timing `NativeAdapter(store/"native")` construction directly (the
Rust-level `NativeStore::open`), 3 reps per arm, back-to-back in one process.
That lane could not score the "manifest-chain open <= 70 ms" threshold by
component — `NativeAdapter()` was a single opaque constructor call with no
internal phase timer exposed to Python, so a checkpoint-load vs delta-replay
split had to be flagged as unmeasured rather than fabricated
(`benchmarks/results-v1/b1-manifest-v2-ab-2026-09-raw.json`'s
`component_breakdown_note`).

`NativeAdapter._store.open_phase_us()` now exists (mirrors the commit path's
`phase_p50_us` / `last_commit_phases`), so this script reports it alongside
the wall-clock open time on every rep: the next chain-open A/B is
component-scored without touching this file again.

    python3 scripts/bench_manifest_chain_open.py --store stores/collegemsg --reps 3
    python3 scripts/bench_manifest_chain_open.py --store stores/collegemsg --reps 3 \\
        --out benchmarks/results-v1/b1v3-chain-open.json

One condition (one store, one engine build) per process invocation, same as
the other `bench_*.py` scripts here (engine_lessons §9g) — the A vs. B split
in an A/B is which process ran which engine `.so`, not a flag to this script.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from tgms.storage.native import NativeAdapter


def open_once(native_dir: Path) -> tuple[float, dict[str, int]]:
    """One cold/warm open: wall-clock ms plus the engine's own phase split.

    The two are independent measurements of overlapping work — `total_ms` is
    this process's `time.perf_counter()` around the constructor, while
    `open_phase_us` is the engine's own `Instant`-based accounting inside
    `NativeStore::open` — so `total_ms * 1000` need not equal
    `open_phase_us["total_us"]` exactly; the gap is pyo3/constructor overhead
    outside the timed Rust call.
    """
    t0 = time.perf_counter()
    adapter = NativeAdapter(native_dir)
    total_ms = (time.perf_counter() - t0) * 1000
    try:
        phases = {k: int(v) for k, v in adapter._store.open_phase_us().items()}
    finally:
        adapter.close()
    return total_ms, phases


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True,
                   help="store root (this script opens <store>/native directly, "
                        "matching B1(c)'s NativeAdapter(store/\"native\") methodology)")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--out", help="write the JSON record here too, in addition to stdout")
    args = p.parse_args()

    native_dir = Path(args.store) / "native"
    total_ms: list[float] = []
    open_phase_us: list[dict[str, int]] = []
    for _ in range(args.reps):
        ms, phases = open_once(native_dir)
        total_ms.append(round(ms, 3))
        open_phase_us.append(phases)

    named_keys = (
        "checkpoint_read_parse_us", "merkle_verify_us", "state_build_us",
        "delta_replay_us", "dictionary_open_us",
    )
    record = {
        "store": args.store,
        "reps": args.reps,
        "open_ms": total_ms,
        "open_ms_median": round(statistics.median(total_ms), 3),
        "open_phase_us": open_phase_us,
        # median-of-each-key, so a component-scored gate does not have to
        # pick one rep out of several — same shape as the commit path's
        # phase_p50_us (scripts/eval_concurrency.py)
        "open_phase_p50_us": {
            key: int(statistics.median(rep[key] for rep in open_phase_us))
            for key in open_phase_us[0]
        } if open_phase_us else {},
        "named_phase_us_share_of_total_median": (
            round(
                statistics.median(
                    sum(rep[k] for k in named_keys) / rep["total_us"]
                    for rep in open_phase_us if rep["total_us"] > 0
                ),
                4,
            )
            if any(rep["total_us"] > 0 for rep in open_phase_us) else None
        ),
    }
    text = json.dumps(record, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
