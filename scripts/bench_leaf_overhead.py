"""E14 P1 — what the R7 leaf costs (`PAPER_A_EVIDENCE_FREEZE.md` §C1).

**The claim under test.** Routing an existing high-level operator through TGIR
as an opaque leaf does not change its latency: the leaf path is within the
±20% between-day band of the direct path (`TGIR_PLAN_PATH=off`) on all fifteen
operators, on both real stores, native backend.

**Why it needs measuring at all.** E6's architectural claim — the ceiling moved
without one bespoke operator, all fifteen operators became opaque leaves — is
today *semantic* only: 26/26 equivalence, 38 frozen digests unmoved, and
`docs/tgir/equiv/` receipts that compare `canonical_json(payload_of(envelope))`
and carry **no timing**. "Leaf overhead is ~zero" is currently a claim about
equality of answers, not equality of cost. A reviewer reads "we wrapped every
operator in an IR" and asks what the wrapper costs. This answers it or the
claim is dropped.

**One condition per process.** `docs/engine_lessons.md` §9g: operators run in
one process tax each other — `paths.k` builds the TCSR and every later scan
pays for its residency. Each (operator, condition) therefore gets its own
child, and `TGIR_PLAN_PATH` is set in that child's environment rather than
toggled in-process, so neither arm can warm the other.

    uv run python scripts/bench_leaf_overhead.py --store stores/bitcoinotc \
        --out benchmarks/results-v1/e14-p1-leaf-overhead.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: §C4's protocol. Reps depend on how long one call takes: the cheap operators
#: are microseconds and need many, the path family is seconds and needs few.
WARMUPS = 5
REPS_FAST = 30
REPS_SLOW = 10
FAST_THRESHOLD_MS = 1000.0

#: A per-call hard bound. `temporal_paths` and `co_active` exist behind a
#: guardrail precisely because their work is unbounded; a P1 run must not
#: inherit that.
CALL_CEILING_S = 300


def cases(stats: dict[str, Any]) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    """One call per operator, windowed from the store's own extent.

    `check_digest_stability.py`'s case list is the model — one case per
    operator, all fifteen — but its arguments are frozen literals tuned to the
    canonical replay log, because a digest whose arguments moved with the store
    would not be a frozen digest. A *timing* harness has the opposite need: the
    call must be meaningful on whichever real store it is pointed at, so the
    window comes from `vt_min`/`vt_max` here.
    """
    lo, hi = int(stats["vt_min"]), int(stats["vt_max"])
    span = max(hi - lo, 1)
    t1, t2 = lo + span // 4, lo + (3 * span) // 4
    stride = max(span // 64, 1)
    win = {"t_a": lo, "t_b": hi}
    return (
        ("entity_history", "entity_history", {"uid": "n1", "limit": 50}),
        ("entity_history_edges", "entity_history",
         {"uid": "n1", "include_edges": True, "limit": 50}),
        ("version_history", "version_history",
         {"kind": "edge", "window": win, "limit": 50}),
        ("snapshot_subgraph", "snapshot_subgraph",
         {"seeds": ["n1"], "t_valid": t1, "hops": 2, "limit": 50}),
        ("diff_snapshots", "diff_snapshots", {"t1": t1, "t2": t2, "limit": 50}),
        ("neighborhood_evolution", "neighborhood_evolution",
         {"uid": "n1", "t1": t1, "t2": t2, "limit": 50}),
        ("resolve_entities", "resolve_entities", {"query": "n1", "limit": 20}),
        ("aggregate_events", "aggregate_events",
         {"group_by": [{"dim": "time_bucket"}], "aggregates": [{"agg": "count"}],
          "window": win, "stride": stride, "limit": 50}),
        ("graph_metric_timeseries", "graph_metric_timeseries",
         {"metric": "edge_event_count", "window": win, "stride": stride,
          "limit": 50}),
        ("burst_detection", "burst_detection",
         {"target": {"kind": "edge_event_rate"}, "window": win,
          "stride": stride, "limit": 50}),
        ("count_temporal_motifs", "count_temporal_motifs",
         {"motif": "M_2node_pingpong", "delta": 3_600_000_000, "window": win}),
        ("find_temporal_motif_instances", "find_temporal_motif_instances",
         {"motif": "M_2node_pingpong", "delta": 3_600_000_000, "window": win,
          "limit": 20}),
        ("temporal_reachability", "temporal_reachability",
         {"src": "n1", "window": win, "limit": 50}),
        ("temporal_paths", "temporal_paths",
         {"src": "n1", "dst": "n2", "window": win, "k": 3, "max_hops": 2}),
        ("co_active", "co_active",
         {"a_spec": {"src": "n1"}, "b_spec": {"src": "n2"},
          "allen_relation": {"relation": "overlaps"}, "limit": 20}),
        ("compute", "compute",
         {"fn": "count", "input": [{"x": 1}, {"x": 2}, {"x": 3}]}),
    )


def measure(store_path: str, case_id: str) -> dict[str, Any]:
    """Child: time one operator call in this process, at this arm."""
    import tgms
    from tgms.core.errors import TgmsError
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    store = tgms.open(store_path, read_only=True)
    try:
        spec = {c[0]: c for c in cases(store.stats())}[case_id]
        _id, op, args = spec

        def go() -> Any:
            return call_operator(store.adapter, op, dict(args))

        try:
            t = time.time()
            go()
            first_ms = (time.time() - t) * 1000.0
        except TgmsError as e:
            return {"case": case_id, "outcome": "REFUSED_OR_ERRORED",
                    "code": e.to_payload().get("code")}

        reps = REPS_FAST if first_ms < FAST_THRESHOLD_MS else REPS_SLOW
        for _ in range(WARMUPS - 1):
            go()
        times = []
        for _ in range(reps):
            t = time.time()
            go()
            times.append((time.time() - t) * 1000.0)
    finally:
        store.close()

    times.sort()
    return {"case": case_id, "outcome": "OK", "reps": len(times),
            "p50_ms": times[len(times) // 2],
            "p95_ms": times[min(len(times) - 1, int(len(times) * 0.95))],
            "min_ms": times[0], "max_ms": times[-1]}


def run_child(store_path: str, case_id: str, plan_path: str) -> dict[str, Any]:
    env = {**os.environ, "TGIR_PLAN_PATH": plan_path,
           "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()),
           "--single", case_id, "--store", store_path, "--out", os.devnull]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=CALL_CEILING_S, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"case": case_id, "outcome": "TIMEOUT"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"case": case_id, "outcome": "ERRORED",
            "error": (done.stderr or done.stdout)[-300:]}


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--single", default="")
    args = ap.parse_args()

    if args.single:
        print(json.dumps(measure(args.store, args.single), default=str))
        return 0

    import tgms

    sha = _sha()
    store = tgms.open(args.store, read_only=True)
    stats = store.stats()
    digest_ids = [c[0] for c in cases(stats)]
    store.close()

    print(f"RUN_STARTED commit={sha} store={args.store} "
          f"cases={len(digest_ids)} host={platform.node()}", flush=True)
    t0 = time.time()
    rows = []
    for case_id in digest_ids:
        on = run_child(args.store, case_id, "on")
        off = run_child(args.store, case_id, "off")
        ratio = None
        if on.get("outcome") == "OK" and off.get("outcome") == "OK" and off["p50_ms"]:
            ratio = on["p50_ms"] / off["p50_ms"]
        rows.append({"case": case_id, "leaf": on, "direct": off,
                     "leaf_over_direct": ratio})
        band = "" if ratio is None else (
            "  within-band" if 0.8 <= ratio <= 1.2 else "  OUTSIDE ±20%")
        print(f"  {case_id:32s} leaf {on.get('p50_ms', on.get('outcome')):>10} "
              f"direct {off.get('p50_ms', off.get('outcome')):>10}  "
              f"{'—' if ratio is None else f'{ratio:.3f}x'}{band}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "manifest": {
            "commit": sha, "host": platform.node(),
            "platform": platform.platform(), "store": args.store,
            "store_stats": stats, "backend": "native",
            "protocol": (f"warmups {WARMUPS}, reps {REPS_FAST} under "
                         f"{FAST_THRESHOLD_MS:.0f} ms else {REPS_SLOW}; "
                         f"median and p95; one condition per process"),
            "band": "leaf/direct within 0.8-1.2 (the +/-20% between-day band)",
            "call_ceiling_s": CALL_CEILING_S,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_s": round(time.time() - t0, 1),
        },
        "rows": rows,
    }, indent=1, sort_keys=True, default=str))

    ok = [r for r in rows if r["leaf_over_direct"] is not None]
    outside = [r for r in ok if not 0.8 <= r["leaf_over_direct"] <= 1.2]
    print(f"\ncomparable {len(ok)}/{len(rows)}; outside the band: {len(outside)}")
    for r in outside:
        print(f"  {r['case']:32s} {r['leaf_over_direct']:.3f}x")
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
