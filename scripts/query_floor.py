#!/usr/bin/env python3
"""Query-ready floor (SCALE_BUILD_FORECAST_2026-09-15 Addendum 5, Stage 1
row "query-ready floor"): a FRESH process cold-opens a finished store and
runs eval_harness's 13-operator registry ONCE each (not the warmup/rep
protocol -- this measures memory, not latency), then reports this process's
own VmHWM read from /proc/self/status right before exit. Distinct from
build_synth_store.py's build-time peak RSS (a different process, a
different phase of the store's life) -- this is what a query-serving
process actually costs to stand up cold.

Reuses scripts/eval_harness.py's own dataset_from_log()/registry()/
call_operator() so the query set and its arguments are byte-identical to
the scale-curve run, not a re-implementation.

Opens `read_only=True` -- this is a cold-open READER measurement, and the
first cut of this script omitted that flag, taking the writer lock like an
ingest process would. Running alongside two other jobs that also opened the
same store right after the build job released it, that cost the first
30M attempt (job 213068) a `WriterLockedError` (writer.lock held by another
pid) 33s in -- a harness bug, not a store or engine finding. Fixed here;
the failed run and its exception are named in the record's `reruns` field
by the job script, not silently dropped.
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
    ap.add_argument("--worktree", required=True,
                     help="cluster worktree root, e.g. /project/xzhang12/tgms-b7")
    ap.add_argument("--store", required=True)
    ap.add_argument("--log", required=True,
                     help="the store's own eventlog.jsonl, to derive registry params from")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.worktree) / "scripts"))
    sys.path.insert(0, args.worktree)

    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered
    import eval_harness

    ensure_all_registered()

    t0 = time.perf_counter()
    data = eval_harness.dataset_from_log(Path(args.log))
    queries = eval_harness.registry(data.t0, data.t1, data.tt_epoch1, data.filter_uids)

    store = tgms.open(args.store, backend="native", read_only=True)
    adapter = store.adapter
    open_ms = round((time.perf_counter() - t0) * 1000, 3)

    results = []
    for q in queries:
        qt0 = time.perf_counter()
        try:
            payload = call_operator(adapter, q.op, dict(q.args))
            ms = round((time.perf_counter() - qt0) * 1000, 3)
            results.append({
                "id": q.id, "op": q.op, "ok": True, "ms": ms,
                "rows": eval_harness._answer_size(payload),
            })
        except Exception as e:  # a refusal/failure is data, same convention as eval_harness
            ms = round((time.perf_counter() - qt0) * 1000, 3)
            details = getattr(e, "details", None)
            results.append({
                "id": q.id, "op": q.op, "ok": False, "ms": ms,
                "error": f"{type(e).__name__}: {e}"[:200],
                "details": details,
            })
    store.close()

    vm = vm_status()
    record = {
        "store": str(args.store),
        "log": str(args.log),
        "open_ms": open_ms,
        "n_queries": len(results),
        "n_ok": sum(1 for r in results if r["ok"]),
        "queries": results,
        "vmhwm_kb": vm.get("VmHWM"),
        "vmrss_kb": vm.get("VmRSS"),
    }
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "vmhwm_kb": vm.get("VmHWM"), "vmrss_kb": vm.get("VmRSS"),
        "n_queries": len(results), "n_ok": record["n_ok"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
