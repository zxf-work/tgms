"""Metamorphic tests (WP1.6).

- Diff composition: additions/removals of diff(t1,t2) and diff(t2,t3) must
  compose consistently with diff(t1,t3): an identity is added over (t1,t3)
  iff its (t1,t2)/(t2,t3) trajectory nets out to added, etc.
- Bi-temporal immutability (the signature test of the whole model): any
  operator evaluated at a fixed as_of_tt returns byte-identical results
  before and after later corrections/retractions.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.temporal.algebra import call_operator, ensure_all_registered

from .test_operators_oracle import T_MAX, UIDS, build_store

ensure_all_registered()

SETTINGS = settings(max_examples=60, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])

times3 = st.tuples(st.integers(0, T_MAX), st.integers(0, T_MAX),
                   st.integers(0, T_MAX)).map(sorted)


def _diff(adapter, t1, t2):
    return call_operator(adapter, "diff_snapshots",
                         {"t1": t1, "t2": t2, "limit": 10_000})


@SETTINGS
@given(seed=st.integers(0, 5), ts=times3)
def test_diff_composition(seed, ts):
    t1, t2, t3 = ts
    adapter, _, _ = build_store(seed)
    d12, d23, d13 = _diff(adapter, t1, t2), _diff(adapter, t2, t3), _diff(adapter, t1, t3)

    for kind in ("nodes", "edges"):
        def ids(d, key):
            rows = d[f"{kind}_{key}"]
            return {r if isinstance(r, str) else r["eid"] for r in rows}

        a12, r12 = ids(d12, "added"), ids(d12, "removed")
        a23, r23 = ids(d23, "added"), ids(d23, "removed")
        a13, r13 = ids(d13, "added"), ids(d13, "removed")
        # present at t3 but not t1  <=>  net trajectory through t2 is "added"
        assert a13 == (a12 - r23) | (a23 - a12 - r12), kind
        assert r13 == (r12 - a23) | (r23 - r12 - a12), kind


@SETTINGS
@given(seed=st.integers(0, 5), uid=st.sampled_from(UIDS),
       t=st.integers(0, T_MAX - 10))
def test_bitemporal_immutability_of_operators(seed, uid, t):
    """Corrections after tt=100 must not change any result pinned to tt=46."""
    from .test_operators_oracle import _store_cache

    # this test mutates the store: always build fresh, never share the cache
    _store_cache.pop(seed, None)
    adapter, _, _ = build_store(seed)  # workload used tts 1..41
    _store_cache.pop(seed, None)
    pinned_tt = 46
    queries = [
        ("snapshot_subgraph", {"seeds": [uid], "hops": 2, "t_valid": t,
                               "as_of_tt": pinned_tt}),
        ("entity_history", {"uid": uid, "as_of_tt": pinned_tt,
                            "include_edges": True}),
        ("temporal_reachability", {"src": uid, "window": {"t_a": t, "t_b": t + 10},
                                   "as_of_tt": pinned_tt}),
        ("graph_metric_timeseries", {"metric": "active_edge_count",
                                     "window": {"t_a": 0, "t_b": T_MAX},
                                     "stride": 5, "as_of_tt": pinned_tt}),
    ]
    before = {op: call_operator(adapter, op, dict(args))["result_digest"]
              for op, args in queries}

    # later writes: a correction and a retraction at tt >= 100
    adapter.apply_ops([{"op": "correct", "ref": {"kind": "node", "uid": uid},
                        "props": {"p": 777}, "vt_s": 0, "vt_e": T_MAX}], 100)
    adapter.apply_ops([{"op": "assert_edge", "src": uid, "dst": UIDS[0],
                        "rel_type": "R", "props": {"w": 9}, "vt_s": 0,
                        "vt_e": T_MAX, "disc": "imm"}], 101)

    for op, args in queries:
        assert call_operator(adapter, op, dict(args))["result_digest"] == before[op], op
