"""Frozen-digest receipt: every operator's `result_digest`, before and after.

M2's operational reading of "no semantic change" (M2_IMPLEMENTATION_PLAN §8.4,
and its definition-of-done item 4) is stronger than the suites: *a suite proves
the payload matches the oracle; a frozen digest proves it matches yesterday.*
This script recomputes `result_digest` for a fixed case list over the canonical
CollegeMsg store on **both** backends and compares against the values checked in
beside it, captured before M2.1's first envelope change.

Its specific job is to catch the failure mode M2 is most exposed to: a metadata
field returned by a *kernel* rather than added to the *envelope* silently
rewrites every digest in the tree, breaking replay, the frozen benchmarks and
`docs/STABILITY.md` §3's promise in one commit. Digest exclusion in M2 is
structural — `digest()` is applied to `payload` before the envelope literal is
assembled, so any key added to the envelope and not to the payload is excluded by
construction — and this script is the guard that says so out loud.

    uv run python scripts/check_digest_stability.py            # check (CI)
    uv run python scripts/check_digest_stability.py --capture  # re-freeze
    uv run python scripts/check_digest_stability.py --backend duckdb

The store is rebuilt by **replay**, never by ingest: a fresh ingest stamps
transaction times from the wall clock, so two independent builds of the same data
legitimately differ (D-023).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tgms.core.errors import TgmsError
from tgms.core.model import OPEN_END
from tgms.storage.eventlog import replay
from tgms.temporal.algebra import call_operator, ensure_all_registered

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "benchmarks/frozen-v1/collegemsg.eventlog.jsonl"
FROZEN = Path(__file__).with_name("frozen_digests_v1.json")

BACKENDS = ("native", "duckdb")

# CollegeMsg's valid-time extent, from its own stats: vt_min 1082040961000000,
# vt_max 1098777142000001. The window below is a fixed sub-interval of it —
# spelled as literals so the case list is a constant, not a store query.
VT_MIN = 1_082_040_961_000_000
W_A = 1_082_000_000_000_000
W_B = 1_086_000_000_000_000
T1 = 1_083_000_000_000_000
T2 = 1_084_000_000_000_000

#: One case per operator — all fifteen (`scripts/check_tgir_leaf_totality.py`
#: will assert the same totality against the plan path in M2.2). Args are fixed
#: literals: a case whose arguments moved with the store would not be a frozen
#: digest at all.
CASES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("entity_history", "entity_history", {"uid": "n1", "limit": 50}),
    ("entity_history_edges", "entity_history",
     {"uid": "n1", "include_edges": True, "limit": 50}),
    ("version_history", "version_history",
     {"kind": "edge", "window": {"t_a": W_A, "t_b": W_B}, "limit": 50}),
    ("snapshot_subgraph", "snapshot_subgraph",
     {"seeds": ["n1"], "t_valid": T1, "hops": 2, "limit": 50}),
    ("diff_snapshots", "diff_snapshots", {"t1": T1, "t2": T2, "limit": 50}),
    ("neighborhood_evolution", "neighborhood_evolution",
     {"uid": "n1", "t1": T1, "t2": T2, "limit": 50}),
    ("resolve_entities", "resolve_entities", {"query": "n1", "limit": 20}),
    ("aggregate_events", "aggregate_events",
     {"group_by": [{"dim": "time_bucket"}], "aggregates": [{"agg": "count"}],
      "window": {"t_a": W_A, "t_b": W_B}, "stride": 86_400_000_000, "limit": 50}),
    ("aggregate_events_endpoint", "aggregate_events",
     {"group_by": [{"dim": "endpoint", "role": "src"}],
      "aggregates": [{"agg": "count"}, {"agg": "mean", "of": "vt_s"}],
      "window": {"t_a": W_A, "t_b": W_B}, "limit": 50}),
    ("graph_metric_timeseries", "graph_metric_timeseries",
     {"metric": "edge_event_count", "window": {"t_a": W_A, "t_b": W_B},
      "stride": 86_400_000_000, "limit": 50}),
    ("burst_detection", "burst_detection",
     {"target": {"kind": "edge_event_rate"}, "window": {"t_a": W_A, "t_b": W_B},
      "stride": 86_400_000_000, "limit": 50}),
    ("count_temporal_motifs", "count_temporal_motifs",
     {"motif": "M_2node_pingpong", "delta": 3_600_000_000,
      "window": {"t_a": W_A, "t_b": W_B}}),
    ("find_temporal_motif_instances", "find_temporal_motif_instances",
     {"motif": "M_2node_pingpong", "delta": 3_600_000_000,
      "window": {"t_a": W_A, "t_b": W_B}, "limit": 20}),
    ("temporal_reachability", "temporal_reachability",
     {"src": "n1", "window": {"t_a": W_A, "t_b": W_B}, "limit": 50}),
    ("temporal_paths", "temporal_paths",
     {"src": "n1", "dst": "n2", "window": {"t_a": W_A, "t_b": W_B}, "k": 3,
      "max_hops": 2}),
    # A guardrail *refusal* is part of the compatibility surface too (C5:
    # `enforce_cost` runs before execution, at the same site, with the same
    # per-operator `cost_fn`). Freezing one pins the refusal point, which no
    # payload digest can.
    ("temporal_paths_refused", "temporal_paths",
     {"src": "n1", "dst": "n2", "window": {"t_a": W_A, "t_b": W_B}, "k": 3,
      "max_hops": 4}),
    ("co_active", "co_active",
     {"a_spec": {"src": "n1"}, "b_spec": {"src": "n2"},
      "allen_relation": {"relation": "overlaps"}, "limit": 20}),
    ("compute", "compute",
     {"fn": "count", "input": [{"x": 1}, {"x": 2}, {"x": 3}]}),
    # a pinned read: the belief basis is below the frontier, so `pinned` is
    # true and the payload must be identical to the unpinned one on a store
    # nothing has written since (§3.6's bi-temporal immutability).
    ("entity_history_pinned", "entity_history",
     {"uid": "n1", "limit": 50, "as_of_tt": OPEN_END - 1}),
)


def build(backend: str) -> Any:
    """Replay the canonical log into a fresh store of `backend`."""
    if backend == "duckdb":
        from tgms.storage.duckdb_adapter import DuckDBAdapter
        adapter: Any = DuckDBAdapter(":memory:")
    elif backend == "native":
        from tgms.storage.native import NativeAdapter
        adapter = NativeAdapter(Path(tempfile.mkdtemp()) / "store")
    else:
        raise SystemExit(f"unknown backend: {backend}")
    replay(LOG, adapter)
    return adapter


def digests(backend: str) -> dict[str, str]:
    ensure_all_registered()
    adapter = build(backend)
    out: dict[str, str] = {}
    for case_id, op, args in CASES:
        try:
            env = call_operator(adapter, op, dict(args))
        except TgmsError as e:
            out[case_id] = f"!{e.code}"
            continue
        out[case_id] = env["result_digest"]
    adapter.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", action="store_true",
                    help="re-freeze the receipt from the current tree")
    ap.add_argument("--backend", choices=BACKENDS + ("both",), default="both")
    args = ap.parse_args()

    if not LOG.exists():
        print(f"skip: {LOG} is not present (frozen corpus not checked out)")
        return 0

    backends = BACKENDS if args.backend == "both" else (args.backend,)
    got: dict[str, dict[str, str]] = {}
    for backend in backends:
        t0 = time.time()
        got[backend] = digests(backend)
        print(f"{backend}: {len(got[backend])} cases in {time.time() - t0:.1f}s")

    if args.capture:
        payload = {"store": "benchmarks/frozen-v1/collegemsg.eventlog.jsonl",
                   "cases": {c[0]: {"op": c[1], "args": c[2]} for c in CASES},
                   "digests": got}
        FROZEN.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        print(f"captured {FROZEN}")
        return 0

    if not FROZEN.exists():
        print(f"skip: no frozen receipt at {FROZEN} — run with --capture")
        return 0
    want = json.loads(FROZEN.read_text())["digests"]
    bad = 0
    for backend in backends:
        for case_id, digest_now in got[backend].items():
            expected = want.get(backend, {}).get(case_id)
            if expected is None:
                print(f"NEW  {backend}/{case_id}: {digest_now} (not in the receipt)")
                continue
            if expected != digest_now:
                bad += 1
                print(f"DIFF {backend}/{case_id}: {expected} -> {digest_now}")
    if bad:
        print(f"\n{bad} result_digest(s) changed. Every new envelope field must be "
              f"placed on the envelope, never inside a kernel's payload — "
              f"`digest()` covers the payload only (algebra.py).")
        return 1
    n = sum(len(got[b]) for b in backends)
    print(f"OK: {n} result_digests unchanged across {', '.join(backends)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
