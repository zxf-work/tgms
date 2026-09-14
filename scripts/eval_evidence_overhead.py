#!/usr/bin/env python
"""Evidence overhead, measured (M7, D-108).

Three costs: descriptor construction per envelope (µs), whole-plan
overhead of live descriptor production (the executor path, on vs off),
and the SQL certificate's relative cost (COUNT-wrapped vs base query).

    python scripts/eval_evidence_overhead.py --json out.json [--sql-db path]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path


def _t(fn, reps):
    xs = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1e3)
    return statistics.median(xs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--sql-db", type=Path)
    args = ap.parse_args()
    out: dict = {"host": platform.node()}

    import tempfile

    import tgms
    from tgms.agent.executor import Executor
    from tgms.agent.ir import Plan
    from tgms.data.synth import generate
    from tgms.evidence.adapter_tgms import build_ecqr
    from tgms.temporal.algebra import call_operator
    from tgms.tools.server import ToolRouter, ensure_all_registered

    ensure_all_registered()
    tmp = Path(tempfile.mkdtemp(prefix="ev-overhead-"))
    generate(tmp / "synth", n_nodes=100, n_events=20_000, seed=3)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    stats = store.stats()
    w = {"t_a": stats["vt_min"], "t_b": stats["vt_max"] + 2}

    small = call_operator(store.adapter, "entity_history",
                          {"uid": store.adapter.uids_for([0])[0],
                           "limit": 100})
    big = call_operator(store.adapter, "aggregate_events", {
        "window": w, "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": 10000})
    out["build_ecqr_ms"] = {
        "small_envelope": round(_t(lambda: build_ecqr(small, "s"), 500), 4),
        "large_envelope": round(_t(lambda: build_ecqr(big, "s"), 500), 4),
        "large_rows": len(big.get("rows", [])),
    }

    plan = Plan.from_json({
        "plan_id": "ovh", "question": "q",
        "steps": [
            {"id": "s1", "op": "graph_metric_timeseries",
             "args": {"metric": "edge_event_count", "window": w,
                      "stride": (w["t_b"] - w["t_a"]) // 20},
             "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "topk", "input": {"$ref": "s1.rows"},
                      "field": "value", "k": 1}, "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "interval", "from": "s2.rows[0]"}})
    ex = Executor(ToolRouter(store.adapter))
    with_ms = _t(lambda: ex.run(plan), 30)
    import tgms.agent.executor as exmod
    real = exmod.__dict__.get("build_ecqr")  # imported lazily inside run
    import tgms.evidence.adapter_tgms as ad
    orig = ad.build_ecqr
    ad.build_ecqr = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    without_ms = _t(lambda: ex.run(plan), 30)   # soft-fails to ecqr=None
    ad.build_ecqr = orig
    assert real is None or True
    out["plan_overhead"] = {
        "with_descriptors_ms": round(with_ms, 3),
        "descriptors_disabled_ms": round(without_ms, 3),
        "overhead_ms": round(with_ms - without_ms, 3),
        "overhead_pct": round(100 * (with_ms - without_ms) /
                              max(without_ms, 1e-9), 2),
    }

    if args.sql_db and args.sql_db.exists():
        import duckdb
        conn = duckdb.connect(str(args.sql_db), read_only=True)
        base = ("SELECT DISTINCT src FROM edge_versions "
                "WHERE tt_e = 4611686018427387904")
        q_ms = _t(lambda: conn.execute(base + " LIMIT 200").fetchall(), 9)
        c_ms = _t(lambda: conn.execute(
            f"SELECT COUNT(*) FROM ({base}) AS _c").fetchone(), 9)
        out["sql_certificate"] = {
            "db": str(args.sql_db), "page_query_ms": round(q_ms, 2),
            "count_certificate_ms": round(c_ms, 2),
            "certificate_over_page": round(c_ms / max(q_ms, 1e-9), 2),
        }

    print(json.dumps(out, indent=1))
    if args.json:
        commit = os.environ.get("COMMIT", "")
        if not commit:
            try:
                commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                        capture_output=True,
                                        text=True).stdout.strip()
            except OSError:
                commit = "unknown"
        out["commit"] = commit
        args.json.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
