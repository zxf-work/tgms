"""Operator micro-benchmarks (WP1.4 / `make bench-ops`).

Per operator: p50/p95 wall latency across a few representative arg shapes and
window sizes against a given store. Emits a markdown report. Informal targets
(spec, not gating): O2/O3/O10 < 200 ms p50 at 1M edges; O4 < 1 s p50 for
30-day windows on CollegeMsg-scale.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import tgms
from tgms.temporal.algebra import call_operator, ensure_all_registered

REPEATS = 7


def _percentiles(xs: list[float]) -> tuple[float, float]:
    xs = sorted(xs)
    p50 = statistics.median(xs)
    p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
    return p50, p95


def bench_cases(store: tgms.Store) -> list[tuple[str, str, dict[str, Any]]]:
    stats = store.stats()
    t0, t1 = stats["vt_min"], stats["vt_max"]
    span = max(1, t1 - t0)
    mid = t0 + span // 2
    uid = store.adapter.uids_for([0])[0]
    win = lambda frac: {"t_a": t0, "t_b": t0 + max(1, int(span * frac))}
    return [
        ("entity_history", "base", {"uid": uid, "include_edges": True}),
        ("snapshot_subgraph", "hop2", {"seeds": [uid], "hops": 2, "t_valid": mid}),
        ("diff_snapshots", "global", {"t1": t0 + span // 4, "t2": t0 + 3 * span // 4}),
        ("temporal_reachability", "w10", {"src": uid, "window": win(0.10)}),
        ("temporal_reachability", "w50", {"src": uid, "window": win(0.50)}),
        ("temporal_paths", "w10", {"src": uid, "dst": store.adapter.uids_for([1])[0],
                                   "window": win(0.10), "max_hops": 3}),
        ("count_temporal_motifs", "tri-w10",
         {"motif": "M_triangle_cyclic", "delta": span // 30, "window": win(0.10)}),
        ("graph_metric_timeseries", "events-100b",
         {"metric": "edge_event_count", "window": win(1.0), "stride": span // 100 + 1}),
        ("burst_detection", "zscore",
         {"target": {"kind": "edge_event_rate"}, "window": win(1.0),
          "stride": span // 100 + 1}),
        ("neighborhood_evolution", "base", {"uid": uid, "t1": t0, "t2": t1}),
        ("co_active", "src-narrow",
         {"a_spec": {"src": uid}, "b_spec": {"src": uid},
          "allen_relation": {"relation": "before", "gap": span // 10}}),
        ("resolve_entities", "substr", {"query": uid[:2]}),
    ]


def run_bench(store_path: str) -> str:
    ensure_all_registered()
    store = tgms.open(store_path)
    stats = store.stats()
    lines = [
        "# TGMS operator micro-benchmarks",
        f"\nstore: `{store_path}` — |V|={stats['n_entities']:,}, "
        f"edge versions={stats['n_edge_versions']:,}\n",
        "| operator | case | p50 ms | p95 ms | rows | note |",
        "|---|---|---:|---:|---:|---|",
    ]
    for op, case, args in bench_cases(store):
        try:
            call_operator(store.adapter, op, dict(args))  # warm (tcsr, caches)
            times, rows = [], 0
            for _ in range(REPEATS):
                t = time.perf_counter()
                res = call_operator(store.adapter, op, dict(args))
                times.append((time.perf_counter() - t) * 1000)
                rows = res.get("rows_total", res.get("count", 0))
            p50, p95 = _percentiles(times)
            lines.append(f"| {op} | {case} | {p50:.1f} | {p95:.1f} | {rows} | |")
        except Exception as e:  # report failures, keep benching
            lines.append(f"| {op} | {case} | - | - | - | {type(e).__name__}: {e} |")
    store.close()
    return "\n".join(lines) + "\n"
