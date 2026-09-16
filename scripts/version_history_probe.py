#!/usr/bin/env python3
"""`version_history` wall + RSS probe (SCALE_BUILD_FORECAST_2026-09-15 Addendum
5, Stage 1 row "version_history, manifest bytes, segment bytes" / §2c of the
main doc): measures the wall time and peak RSS of one
`version_history(kind="edge", belief="all")` call on a finished store, same
call shape as scripts/bench_versions.py's "all" condition and the same
methodology B2 used (benchmarks/results-v1/b2-version-history-ab-2026-09*):
`call_operator(..., skip_cost_check=True)` (the plain path raises CostError
-- version_history is refused by the cost guardrail past a few million
versions by design, per its own docstring), one call this process, wall
time and this process's own VmHWM from /proc/self/status read right before
exit. Run this three times (three separate processes) for the B2-style
median-of-3, per the calling job script -- this script itself does one call.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def vm_status() -> dict:
    out = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(("VmRSS:", "VmHWM:")):
                    k, v = line.split(":", 1)
                    out[k.strip()] = int(v.strip().split()[0])
    except FileNotFoundError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--store", required=True)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.worktree) / "scripts"))
    sys.path.insert(0, args.worktree)

    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()

    t_open0 = time.perf_counter()
    store = tgms.open(args.store, backend="native")
    adapter = store.adapter
    open_ms = round((time.perf_counter() - t_open0) * 1000, 3)

    stats = adapter.stats()
    lo, hi = stats["vt_min"], stats["vt_max"] + 1
    window = {"t_a": lo, "t_b": hi}
    call = {"kind": "edge", "window": window, "belief": "all", "limit": 10}

    t0 = time.perf_counter()
    out = call_operator(adapter, "version_history", dict(call), skip_cost_check=True)
    wall_ms = round((time.perf_counter() - t0) * 1000, 3)
    store.close()

    vm = vm_status()
    record = {
        "store": str(args.store), "op": "version_history", "call": call,
        "open_ms": open_ms, "wall_ms": wall_ms,
        "rows_total": out.get("rows_total"),
        "vmhwm_kb": vm.get("VmHWM"), "vmrss_kb": vm.get("VmRSS"),
    }
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
