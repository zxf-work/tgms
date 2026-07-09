"""Time-respecting path operators: O4 temporal_reachability, O5 temporal_paths.

Semantics (shared by engine and oracle — the operational definition):

An edge version e = (u -> v, [vt_s, vt_e)) is *traversable* from u at arrival
time a iff tau = max(a, vt_s) satisfies tau < vt_e and tau < t_b; the arrival
at v is tau. Consecutive edges therefore have non-decreasing traversal times
(t_{i+1} >= t_i). Instantaneous events (vt_e = vt_s + 1) reduce to the classic
Wu et al. (PVLDB 2014) earliest-arrival semantics.

`delta_max_wait` (O4): along a path, the wait tau_i - tau_{i-1} at every
intermediate node must be <= delta (no constraint at the source). Semantics
are path-based and exact: a node is reachable iff some time-respecting path
satisfies all wait constraints; earliest_arrival is the minimum over such
paths. Without delta, prefix-optimality holds and a single-label vectorized
fixpoint is exact; with delta, smaller arrivals can *disable* later edges, so
the kernel switches to an exact multi-label search over (node, arrival)
states (Rust rewrite candidate at M3 per spec 7.1).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import CostError, LimitError
from tgms.core.model import OPEN_END
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    UID,
    WINDOW,
    check_window,
    operator,
    paginate,
    required,
)
from tgms.temporal.guardrails import scan_estimate, window_fraction

INF = OPEN_END
MAX_PATH_HOPS = 6
MAX_K = 20
MAX_EXPANSIONS = 500_000


def _reach_cost(args: dict[str, Any], stats: dict[str, Any]) -> dict[str, int]:
    rows = int(stats.get("n_edge_versions", 0) * window_fraction(args, stats))
    # fixpoint rounds are bounded by path length; budget a small multiple
    return {"rows_scanned_est": rows, "expansions_est": rows * 8}


@operator(
    "temporal_reachability",
    {
        "src": required(UID),
        "window": required(WINDOW),
        "as_of_tt": AS_OF_TT,
        "delta_max_wait": {"type": ["integer", "null"], "minimum": 1, "default": None},
        "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "out"},
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Earliest-arrival time per node reachable from `src` via time-respecting "
    "paths inside `window` (source excluded). direction='in' answers "
    "\"who could reach src\".",
    cost_fn=_reach_cost,
    validators=[check_window],
)
def temporal_reachability(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    as_of = args["as_of_tt"]
    delta = args["delta_max_wait"]
    sid = int(adapter.dense_ids([args["src"]])[0])

    e = adapter.edges_columnar(as_of_tt=as_of, vt_min=t_a, vt_max=t_b)
    src, dst, vt_s, vt_e = e["src_id"], e["dst_id"], e["vt_s"], e["vt_e"]
    if args["direction"] == "in":
        src, dst = dst, src
    elif args["direction"] == "both":
        src, dst = np.concatenate([src, dst]), np.concatenate([dst, src])
        vt_s, vt_e = np.concatenate([vt_s, vt_s]), np.concatenate([vt_e, vt_e])

    n = adapter.num_entities()
    if delta is None:
        arr = np.full(n, INF, dtype=np.int64)
        arr[sid] = t_a
        # vectorized label-correcting fixpoint: each round relaxes every edge
        # once; exact because smaller arrivals are always at least as good
        while True:
            a_u = arr[src]
            tau = np.maximum(a_u, vt_s)
            ok = (tau < vt_e) & (tau < t_b) & (a_u < INF)
            new = arr.copy()
            np.minimum.at(new, dst[ok], tau[ok])
            if np.array_equal(new, arr):
                break
            arr = new
    else:
        arr = _multilabel_arrivals(n, sid, t_a, t_b, delta, src, dst, vt_s, vt_e)

    reached = np.flatnonzero((arr < INF) & (np.arange(n) != sid))
    uids = adapter.uids_for(reached)
    rows = [{"uid": u, "earliest_arrival": int(a)}
            for u, a in zip(uids, arr[reached])]
    rows.sort(key=lambda r: (r["earliest_arrival"], r["uid"]))
    return paginate(rows, args["limit"], args["cursor"])


def _multilabel_arrivals(n: int, sid: int, t_a: int, t_b: int, delta: int,
                         src: np.ndarray, dst: np.ndarray,
                         vt_s: np.ndarray, vt_e: np.ndarray) -> np.ndarray:
    """Exact earliest arrivals under a wait cap: BFS over (node, arrival)
    states. All distinct arrival labels per node are kept — under delta,
    dominance pruning is unsound (a smaller arrival can disable an edge)."""
    from collections import deque

    order = np.argsort(src, kind="stable")
    s_sorted = src[order]
    starts = np.searchsorted(s_sorted, np.arange(n), side="left")
    stops = np.searchsorted(s_sorted, np.arange(n), side="right")

    labels: list[set[int]] = [set() for _ in range(n)]
    labels[sid].add(t_a)
    queue = deque([(sid, t_a)])
    expansions = 0
    while queue:
        u, a = queue.popleft()
        for k in range(int(starts[u]), int(stops[u])):
            expansions += 1
            if expansions > MAX_EXPANSIONS:
                raise CostError("temporal_reachability expansion budget exceeded",
                                estimate={"expansions_est": expansions},
                                suggestions=["narrow the window",
                                             "drop or loosen delta_max_wait"])
            i = int(order[k])
            tau = max(a, int(vt_s[i]))
            if tau >= int(vt_e[i]) or tau >= t_b:
                continue
            # the wait cap is waived only for the source *start* label
            if not (u == sid and a == t_a) and tau - a > delta:
                continue
            v = int(dst[i])
            if tau not in labels[v]:
                labels[v].add(tau)
                queue.append((v, tau))
    arr = np.full(n, INF, dtype=np.int64)
    for v in range(n):
        if labels[v]:
            arr[v] = min(labels[v])
    arr[sid] = t_a
    return arr


def _paths_validators(args: dict[str, Any]) -> None:
    check_window(args)
    if args["max_hops"] > MAX_PATH_HOPS:
        raise LimitError(f"max_hops capped at {MAX_PATH_HOPS}")
    if args["k"] > MAX_K:
        raise LimitError(f"k capped at {MAX_K}")


@operator(
    "temporal_paths",
    {
        "src": required(UID),
        "dst": required(UID),
        "window": required(WINDOW),
        "k": {"type": "integer", "minimum": 1, "maximum": MAX_K, "default": 5},
        "max_hops": {"type": "integer", "minimum": 1, "maximum": MAX_PATH_HOPS,
                     "default": 4},
        "as_of_tt": AS_OF_TT,
    },
    "Up to k node-simple time-respecting paths src -> dst inside `window`, "
    "ordered by (arrival, hops, edge sequence).",
    cost_fn=_reach_cost,
    validators=[_paths_validators],
)
def temporal_paths(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    as_of = args["as_of_tt"]
    sid, did = (int(x) for x in adapter.dense_ids([args["src"], args["dst"]]))

    e = adapter.edges_columnar(as_of_tt=as_of, vt_min=t_a, vt_max=t_b)
    # per-source adjacency slices, neighbors ordered by (vt_s, eid) — this
    # ordering also fixes the deterministic enumeration order
    order = np.lexsort((e["eid"], e["vt_s"], e["src_id"]))
    src = e["src_id"][order]
    dst = e["dst_id"][order]
    vt_s = e["vt_s"][order]
    vt_e = e["vt_e"][order]
    eid = e["eid"][order]
    rel = e["rel_type"][order]
    starts = np.searchsorted(src, np.arange(adapter.num_entities()), side="left")
    stops = np.searchsorted(src, np.arange(adapter.num_entities()), side="right")

    paths: list[tuple[int, int, tuple, list[int]]] = []
    expansions = 0

    def dfs(node: int, arrival: int, hops: int, visited: set[int], trail: list[int]) -> None:
        nonlocal expansions
        if node == did and trail:
            key = tuple((int(vt_s[i]), eid[i]) for i in trail)
            paths.append((arrival, hops, key, list(trail)))
            return  # node-simple: dst terminates the path
        if hops == args["max_hops"]:
            return
        for i in range(int(starts[node]), int(stops[node])):
            expansions += 1
            if expansions > MAX_EXPANSIONS:
                raise CostError("temporal_paths expansion budget exceeded",
                                estimate={"expansions_est": expansions},
                                suggestions=["narrow the window",
                                             "reduce max_hops"])
            v = int(dst[i])
            if v in visited:
                continue
            tau = max(arrival, int(vt_s[i]))
            if tau >= int(vt_e[i]) or tau >= t_b:
                continue
            visited.add(v)
            trail.append(i)
            dfs(v, tau, hops + 1, visited, trail)
            trail.pop()
            visited.remove(v)

    dfs(sid, t_a, 0, {sid}, [])
    paths.sort(key=lambda p: (p[0], p[1], p[2]))
    rows = []
    for arrival, hops, _, trail in paths[: args["k"]]:
        s_uids = adapter.uids_for([int(src[i]) for i in trail])
        d_uids = adapter.uids_for([int(dst[i]) for i in trail])
        rows.append({
            "arrival": arrival,
            "hops": hops,
            "edges": [{"src": s, "dst": d, "rel_type": rel[i], "eid": eid[i],
                       "t": int(vt_s[i])}
                      for i, s, d in zip(trail, s_uids, d_uids)],
        })
    return {"rows": rows, "rows_total": len(paths),
            "truncated": len(paths) > args["k"], "cursor": None}
