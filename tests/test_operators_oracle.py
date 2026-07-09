"""M2 acceptance: every operator's output equals the brute-force oracle
(canonical-JSON equality) across randomized stores and args.

Run with TGMS_HYP_EXAMPLES=500 for the full milestone acceptance sweep.
"""

from __future__ import annotations

import os
import random
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import NotFoundError
from tgms.core.model import canonical_json
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.temporal.algebra import _canonicalize_floats, call_operator, ensure_all_registered
from tgms.temporal.oracle import Oracle

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

T_MAX = 60  # small dense time domain
UIDS = [f"u{i}" for i in range(8)]
RELS = ["R", "S", "MSG"]

_store_cache: dict[int, tuple[DuckDBAdapter, Oracle, list[str]]] = {}


def build_store(seed: int) -> tuple[DuckDBAdapter, Oracle, list[str]]:
    """Deterministic random bi-temporal store: interval edges via the update
    path (asserts/retracts/corrections) plus instantaneous events."""
    if seed in _store_cache:
        return _store_cache[seed]
    rng = random.Random(seed)
    a = DuckDBAdapter(":memory:")
    tt = 0
    for _ in range(40):
        tt += 1
        kind = rng.choice(["an", "an", "ae", "ae", "ae", "rt", "co"])
        u, v = rng.choice(UIDS), rng.choice(UIDS)
        s = rng.randrange(0, T_MAX - 1)
        e = s + rng.randrange(1, T_MAX - s)
        try:
            if kind == "an":
                a.apply_ops([{"op": "assert_node", "uid": u, "label": "N",
                              "props": {"name": f"Name {u}", "p": rng.randrange(3)},
                              "vt_s": s, "vt_e": e}], tt)
            elif kind == "ae":
                a.apply_ops([{"op": "assert_edge", "src": u, "dst": v,
                              "rel_type": rng.choice(RELS[:2]),
                              "props": {"w": rng.randrange(4)},
                              "vt_s": s, "vt_e": e, "disc": ""}], tt)
            elif kind == "rt":
                a.apply_ops([{"op": "retract",
                              "ref": {"kind": "edge", "src": u, "dst": v,
                                      "rel_type": "R", "disc": ""},
                              "t": rng.randrange(0, T_MAX)}], tt)
            else:
                a.apply_ops([{"op": "correct", "ref": {"kind": "node", "uid": u},
                              "props": {"name": f"Name {u} v2", "p": 9},
                              "vt_s": s, "vt_e": e}], tt)
        except NotFoundError:
            pass
    tt += 1
    events = [{"src": rng.choice(UIDS), "dst": rng.choice(UIDS), "rel_type": "MSG",
               "vt_s": rng.randrange(0, T_MAX)} for _ in range(120)]
    a.apply_ops([{"op": "ingest_events", "events": events, "offset": 0,
                  "node_label": "Node"}], tt)
    oracle = Oracle(list(a.all_node_versions()), list(a.all_edge_versions()))
    known = a.uids_for(list(range(a.num_entities())))
    _store_cache[seed] = (a, oracle, known)
    return _store_cache[seed]


def check_against_oracle(op: str, args: dict[str, Any], seed: int) -> None:
    adapter, oracle, _ = build_store(seed)
    engine = call_operator(adapter, op, args)
    expected = _canonicalize_floats(getattr(oracle, op)(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ("op", "args_echo", "dataset_extent", "result_digest")}
    assert canonical_json(payload) == canonical_json(expected), \
        f"{op} mismatch for args={args}"


# ---- shared strategies ----------------------------------------------------- #

seeds = st.integers(0, 5)
times = st.integers(0, T_MAX)
tts = st.one_of(st.integers(1, 45), st.just(2**62))
limits = st.one_of(st.just(100), st.integers(1, 5))
known_uid = st.sampled_from(UIDS)


def window():
    return st.tuples(st.integers(0, T_MAX - 1), st.integers(1, T_MAX)) \
        .map(lambda t: {"t_a": min(t[0], t[1] - 1), "t_b": max(t[0] + 1, t[1])})


# ---- per-operator property tests ------------------------------------------- #

@SETTINGS
@given(seed=seeds, uid=known_uid, as_of=tts, inc=st.booleans(), limit=limits)
def test_entity_history(seed, uid, as_of, inc, limit):
    check_against_oracle("entity_history",
                         {"uid": uid, "as_of_tt": as_of, "include_edges": inc,
                          "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, s=st.lists(known_uid, min_size=1, max_size=3, unique=True),
       hops=st.integers(0, 3), t=times, as_of=tts, limit=limits,
       rels=st.one_of(st.none(), st.just(["R"]), st.just(["R", "MSG"])))
def test_snapshot_subgraph(seed, s, hops, t, as_of, limit, rels):
    check_against_oracle("snapshot_subgraph",
                         {"seeds": s, "hops": hops, "t_valid": t, "as_of_tt": as_of,
                          "rel_types": rels, "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, t1=times, t2=times, as_of=tts, limit=limits,
       scope=st.one_of(st.none(),
                       st.builds(lambda s, h: {"seeds": s, "hops": h},
                                 st.lists(known_uid, min_size=1, max_size=2,
                                          unique=True),
                                 st.integers(0, 2))))
def test_diff_snapshots(seed, t1, t2, as_of, limit, scope):
    check_against_oracle("diff_snapshots",
                         {"t1": t1, "t2": t2, "as_of_tt": as_of, "scope": scope,
                          "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, src=known_uid, w=window(), as_of=tts,
       delta=st.one_of(st.none(), st.integers(1, 20)),
       direction=st.sampled_from(["out", "in", "both"]), limit=limits)
def test_temporal_reachability(seed, src, w, as_of, delta, direction, limit):
    check_against_oracle("temporal_reachability",
                         {"src": src, "window": w, "as_of_tt": as_of,
                          "delta_max_wait": delta, "direction": direction,
                          "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, src=known_uid, dst=known_uid, w=window(), as_of=tts,
       k=st.integers(1, 8), hops=st.integers(1, 4))
def test_temporal_paths(seed, src, dst, w, as_of, k, hops):
    check_against_oracle("temporal_paths",
                         {"src": src, "dst": dst, "window": w, "k": k,
                          "max_hops": hops, "as_of_tt": as_of}, seed)


MOTIFS = ["M_triangle_cyclic", "M_triangle_acyclic_1", "M_2node_pingpong",
          "M_star_out_3", "M_path_3"]


@SETTINGS
@given(seed=seeds, motif=st.sampled_from(MOTIFS), delta=st.integers(1, 30),
       w=window(), as_of=tts,
       nf=st.one_of(st.none(), st.lists(known_uid, min_size=2, max_size=6,
                                        unique=True)))
def test_count_temporal_motifs(seed, motif, delta, w, as_of, nf):
    check_against_oracle("count_temporal_motifs",
                         {"motif": motif, "delta": delta, "window": w,
                          "node_filter": nf, "as_of_tt": as_of}, seed)


@SETTINGS
@given(seed=seeds, motif=st.sampled_from(MOTIFS), delta=st.integers(1, 20),
       w=window(), as_of=tts, limit=limits)
def test_find_temporal_motif_instances(seed, motif, delta, w, as_of, limit):
    check_against_oracle("find_temporal_motif_instances",
                         {"motif": motif, "delta": delta, "window": w,
                          "as_of_tt": as_of, "limit": limit}, seed)


METRICS = ["node_count", "edge_event_count", "active_edge_count",
           "mean_out_degree", "new_node_rate", "reciprocity"]


@SETTINGS
@given(seed=seeds, metric=st.sampled_from(METRICS), w=window(),
       stride=st.integers(1, 20), as_of=tts, limit=limits)
def test_graph_metric_timeseries(seed, metric, w, stride, as_of, limit):
    check_against_oracle("graph_metric_timeseries",
                         {"metric": metric, "window": w, "stride": stride,
                          "as_of_tt": as_of, "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, w=window(), stride=st.integers(1, 15), as_of=tts,
       method=st.sampled_from(["zscore", "ratio"]),
       params=st.fixed_dictionaries({}, optional={"w": st.integers(2, 8),
                                                  "z": st.just(1.5),
                                                  "r": st.just(2.0)}),
       target=st.one_of(
           st.just({"kind": "edge_event_rate"}),
           st.just({"kind": "edge_event_rate", "rel_type": "MSG"}),
           st.builds(lambda u: {"kind": "node_activity", "uid": u}, known_uid)))
def test_burst_detection(seed, w, stride, as_of, method, params, target):
    check_against_oracle("burst_detection",
                         {"target": target, "window": w, "stride": stride,
                          "method": method, "params": params, "as_of_tt": as_of},
                         seed)


@SETTINGS
@given(seed=seeds, uid=known_uid, t1=st.integers(0, T_MAX - 2), span=st.integers(1, 30),
       as_of=tts, stride=st.one_of(st.none(), st.integers(1, 10)), limit=limits)
def test_neighborhood_evolution(seed, uid, t1, span, as_of, stride, limit):
    check_against_oracle("neighborhood_evolution",
                         {"uid": uid, "t1": t1, "t2": min(t1 + span, T_MAX),
                          "as_of_tt": as_of, "stride": stride, "limit": limit}, seed)


spec = st.fixed_dictionaries({}, optional={
    "src": known_uid, "dst": known_uid, "rel_type": st.sampled_from(RELS)})
relation = st.one_of(
    st.builds(lambda r: {"relation": r},
              st.sampled_from(["overlaps", "during", "meets"])),
    st.builds(lambda g: {"relation": "before", "gap": g}, st.integers(1, 20)))


@SETTINGS
@given(seed=seeds, a=spec, b=spec, rel=relation, as_of=tts, limit=limits)
def test_co_active(seed, a, b, rel, as_of, limit):
    check_against_oracle("co_active",
                         {"a_spec": a, "b_spec": b, "allen_relation": rel,
                          "as_of_tt": as_of, "limit": limit}, seed)


@SETTINGS
@given(seed=seeds, q=st.one_of(known_uid, st.sampled_from(["u", "Name", "u3", "zz"])),
       label=st.one_of(st.none(), st.sampled_from(["N", "Node", "X"])),
       as_of=tts, limit=limits)
def test_resolve_entities(seed, q, label, as_of, limit):
    check_against_oracle("resolve_entities",
                         {"query": q, "label": label, "as_of_tt": as_of,
                          "limit": limit}, seed)


# ---- deterministic pagination round-trip ----------------------------------- #

def test_cursor_pagination_is_deterministic():
    adapter, _, _ = build_store(0)
    first = call_operator(adapter, "temporal_reachability",
                          {"src": "u0", "window": {"t_a": 0, "t_b": T_MAX},
                           "limit": 2})
    if first["truncated"]:
        nxt = call_operator(adapter, "temporal_reachability",
                            {"src": "u0", "window": {"t_a": 0, "t_b": T_MAX},
                             "limit": 2, "cursor": first["cursor"]})
        assert nxt["rows"][0] not in first["rows"]
        assert first["rows_total"] == nxt["rows_total"]
    again = call_operator(adapter, "temporal_reachability",
                          {"src": "u0", "window": {"t_a": 0, "t_b": T_MAX},
                           "limit": 2})
    assert again["result_digest"] == first["result_digest"]
