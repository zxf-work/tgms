"""aggregate_events (O14, D-044): oracle equivalence, metamorphic properties,
and argument-contract cases.

Runs against the backend `TGMS_TEST_BACKEND` selects (conftest.fresh_adapter),
so the same ground truth judges the portable fallback and the native kernel.
"""

from __future__ import annotations

import os
import random
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, LimitError, SchemaError
from tgms.core.model import OPEN_END, canonical_json
from tgms.temporal.algebra import (
    _canonicalize_floats,
    call_operator,
    ensure_all_registered,
)
from tgms.temporal.oracle import Oracle

from .conftest import fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

T_MAX = 60
UIDS = [f"u{i}" for i in range(8)]

_store_cache: dict[int, tuple[Any, Oracle]] = {}


def _apply(adapter, ops, tt):
    """Bracket one batch the way Store._write does: the native engine
    publishes a generation at commit, and columnar scans read published
    generations only."""
    adapter.begin()
    try:
        adapter.apply_ops(ops, tt)
    except Exception:
        adapter.rollback()
        raise
    adapter.commit()


def build_store(seed: int) -> tuple[Any, Oracle]:
    """Deterministic random bi-temporal store with everything the operator
    can see: interval edges, instantaneous events, open-ended intervals
    (duration-excluded rows), label changes over valid time, corrections."""
    if seed in _store_cache:
        return _store_cache[seed]
    rng = random.Random(seed)
    a = fresh_adapter()
    tt = 0
    for _ in range(30):
        tt += 1
        kind = rng.choice(["an", "an", "ae", "ae", "rt", "co"])
        u, v = rng.choice(UIDS), rng.choice(UIDS)
        s = rng.randrange(0, T_MAX - 1)
        e = s + rng.randrange(1, T_MAX - s)
        try:
            if kind == "an":
                # labels differ across versions, so the label dimension has
                # a temporal join to get wrong
                _apply(a, [{"op": "assert_node", "uid": u,
                              "label": rng.choice(["A", "B"]),
                              "props": {"name": f"Name {u}"},
                              "vt_s": s, "vt_e": e}], tt)
            elif kind == "ae":
                # a slice of edges is open-ended: excluded from duration
                # aggregates, counted by everything else
                vt_e = OPEN_END if rng.random() < 0.25 else e
                _apply(a, [{"op": "assert_edge", "src": u, "dst": v,
                              "rel_type": rng.choice(["R", "S"]),
                              "props": {}, "vt_s": s, "vt_e": vt_e,
                              "disc": ""}], tt)
            elif kind == "rt":
                _apply(a, [{"op": "retract",
                              "ref": {"kind": "edge", "src": u, "dst": v,
                                      "rel_type": "R", "disc": ""},
                              "t": rng.randrange(0, T_MAX)}], tt)
            else:
                _apply(a, [{"op": "correct",
                              "ref": {"kind": "node", "uid": u},
                              "props": {"name": f"Name {u} v2"},
                              "vt_s": s, "vt_e": e}], tt)
        except Exception:
            pass
    tt += 1
    events = [{"src": rng.choice(UIDS), "dst": rng.choice(UIDS),
               "rel_type": "MSG", "vt_s": rng.randrange(0, T_MAX)}
              for _ in range(100)]
    _apply(a, [{"op": "ingest_events", "events": events, "offset": 0,
                  "node_label": "Node"}], tt)
    oracle = Oracle(list(a.all_node_versions()), list(a.all_edge_versions()))
    _store_cache[seed] = (a, oracle)
    return _store_cache[seed]


def check_against_oracle(args: dict[str, Any], seed: int) -> None:
    adapter, oracle = build_store(seed)
    engine = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(
        oracle.aggregate_events(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ("op", "args_echo", "dataset_extent",
                            "result_digest")}
    assert canonical_json(payload) == canonical_json(expected), \
        f"aggregate_events mismatch for args={args}"


# ---- strategies ------------------------------------------------------------ #

seeds = st.integers(0, 5)
tts = st.one_of(st.integers(1, 40), st.just(2**62))
limits = st.one_of(st.just(100), st.integers(1, 5))

DIMS = [
    {"dim": "time_bucket"},
    {"dim": "rel_type"},
    {"dim": "endpoint", "role": "src"},
    {"dim": "endpoint", "role": "dst"},
    {"dim": "label", "role": "src"},
    {"dim": "label", "role": "dst"},
]
AGGS = [
    {"agg": "count"},
    {"agg": "count_distinct", "of": "src"},
    {"agg": "count_distinct", "of": "dst"},
    {"agg": "min", "of": "vt_s"},
    {"agg": "max", "of": "vt_s"},
    {"agg": "mean", "of": "vt_s"},
    {"agg": "min", "of": "duration"},
    {"agg": "max", "of": "duration"},
    {"agg": "mean", "of": "duration"},
]


def window():
    return st.tuples(st.integers(0, T_MAX - 1), st.integers(1, T_MAX)) \
        .map(lambda t: {"t_a": min(t[0], t[1] - 1), "t_b": max(t[0] + 1, t[1])})


@st.composite
def group_by(draw):
    dims = draw(st.lists(st.sampled_from(range(len(DIMS))), max_size=2,
                         unique=True))
    return [DIMS[i] for i in dims]


@st.composite
def aggregates(draw):
    idx = draw(st.lists(st.sampled_from(range(len(AGGS))), min_size=1,
                        max_size=4, unique=True))
    return [AGGS[i] for i in idx]


@st.composite
def agg_args(draw):
    gb = draw(group_by())
    args: dict[str, Any] = {
        "group_by": gb,
        "aggregates": draw(aggregates()),
        "window": draw(window()),
        "as_of_tt": draw(tts),
        "limit": draw(limits),
    }
    if any(d["dim"] == "time_bucket" for d in gb):
        args["stride"] = draw(st.integers(1, T_MAX))
    if draw(st.booleans()):
        args["rel_types"] = draw(st.sampled_from(
            [["R"], ["MSG"], ["R", "S"], ["R", "S", "MSG"]]))
    return args


# ---- oracle equivalence ---------------------------------------------------- #

@SETTINGS
@given(seed=seeds, args=agg_args())
def test_aggregate_events_matches_oracle(seed, args):
    check_against_oracle(args, seed)


# ---- metamorphic properties ------------------------------------------------ #

W = {"t_a": 0, "t_b": T_MAX}


def _call(adapter, **kw):
    args = {"window": W, "limit": 10_000, **kw}
    return call_operator(adapter, "aggregate_events", args)


@SETTINGS
@given(seed=seeds, dims=group_by())
def test_group_counts_sum_to_the_global_count(seed, dims):
    """Partitioning events cannot create or lose any: group counts sum to
    the ungrouped count, whatever the dimensions."""
    adapter, _ = build_store(seed)
    total = _call(adapter, group_by=[],
                  aggregates=[{"agg": "count"}])["rows"][0]["count"]
    stride = {"stride": 7} if any(d["dim"] == "time_bucket" for d in dims) \
        else {}
    grouped = _call(adapter, group_by=dims,
                    aggregates=[{"agg": "count"}], **stride)
    assert not grouped["truncated"]
    assert sum(r["count"] for r in grouped["rows"]) == total


@SETTINGS
@given(seed=seeds)
def test_bucket_counts_agree_with_graph_metric_timeseries(seed):
    """Grouping by time_bucket must reproduce edge_event_count's non-empty
    buckets — two operators, one truth."""
    adapter, _ = build_store(seed)
    agg = _call(adapter, group_by=[{"dim": "time_bucket"}],
                aggregates=[{"agg": "count"}], stride=10)
    series = call_operator(adapter, "graph_metric_timeseries",
                           {"metric": "edge_event_count", "window": W,
                            "stride": 10, "limit": 10_000})
    nonzero = [{"t_a": r["t_a"], "t_b": r["t_b"], "count": r["value"]}
               for r in series["rows"] if r["value"] > 0]
    got = [{"t_a": r["t_a"], "t_b": r["t_b"], "count": r["count"]}
           for r in agg["rows"]]
    assert got == nonzero


@SETTINGS
@given(seed=seeds, stride=st.integers(1, 4))
def test_series_keeps_the_empty_buckets_aggregate_events_omits(seed, stride):
    """The two operators disagree on purpose, and the disagreement is the
    contract (D-044).

    `graph_metric_timeseries` emits **every** bucket in the window, value 0
    where nothing happened, and reports `n_buckets`; `aggregate_events`
    emits only non-empty groups. Sharing an implementation underneath is
    allowed; letting the sharing leak upward is not. A small stride over a
    sparse store guarantees empty buckets exist, so this fails loudly if the
    series operator ever starts returning the aggregation operator's rows.
    """
    adapter, _ = build_store(seed)
    series = call_operator(adapter, "graph_metric_timeseries",
                           {"metric": "edge_event_count", "window": W,
                            "stride": stride, "limit": 10_000})
    agg = _call(adapter, group_by=[{"dim": "time_bucket"}],
                aggregates=[{"agg": "count"}], stride=stride, limit=10_000)

    expected_buckets = -(-(W["t_b"] - W["t_a"]) // stride)
    assert series["n_buckets"] == expected_buckets
    assert len(series["rows"]) == expected_buckets
    assert [r["t_a"] for r in series["rows"]] == \
        list(range(W["t_a"], W["t_b"], stride))
    # every bucket the aggregation omitted is present above with value 0
    nonempty = {r["t_a"] for r in agg["rows"]}
    for r in series["rows"]:
        assert (r["value"] > 0) == (r["t_a"] in nonempty), r
    assert len(agg["rows"]) <= expected_buckets


@SETTINGS
@given(seed=seeds)
def test_rel_dimension_agrees_with_rel_filter(seed):
    """Grouping by rel_type == running one rel_types-filtered ungrouped call
    per relation (grouping and filtering are the same partition)."""
    adapter, _ = build_store(seed)
    grouped = _call(adapter, group_by=[{"dim": "rel_type"}],
                    aggregates=[{"agg": "count"},
                                {"agg": "count_distinct", "of": "dst"}])
    for row in grouped["rows"]:
        single = _call(adapter, group_by=[], rel_types=[row["rel_type"]],
                       aggregates=[{"agg": "count"},
                                   {"agg": "count_distinct", "of": "dst"}])
        assert single["rows"][0]["count"] == row["count"]
        assert single["rows"][0]["distinct_dst"] == row["distinct_dst"]


@SETTINGS
@given(seed=seeds, dims=group_by())
def test_distinct_never_exceeds_count(seed, dims):
    adapter, _ = build_store(seed)
    stride = {"stride": 9} if any(d["dim"] == "time_bucket" for d in dims) \
        else {}
    out = _call(adapter, group_by=dims,
                aggregates=[{"agg": "count"},
                            {"agg": "count_distinct", "of": "src"},
                            {"agg": "count_distinct", "of": "dst"}],
                **stride)
    for r in out["rows"]:
        assert 0 < r["distinct_src"] <= r["count"]
        assert 0 < r["distinct_dst"] <= r["count"]


@SETTINGS
@given(seed=seeds)
def test_pages_concatenate_to_the_full_answer(seed):
    adapter, _ = build_store(seed)
    full = _call(adapter, group_by=[{"dim": "endpoint", "role": "src"}],
                 aggregates=[{"agg": "count"}])
    pages, cursor = [], None
    while True:
        page = call_operator(adapter, "aggregate_events",
                             {"window": W, "limit": 2, "cursor": cursor,
                              "group_by": [{"dim": "endpoint", "role": "src"}],
                              "aggregates": [{"agg": "count"}]})
        pages.extend(page["rows"])
        assert page["rows_total"] == full["rows_total"]
        if not page["truncated"]:
            break
        cursor = page["cursor"]
    assert pages == full["rows"]


@SETTINGS
@given(seed=seeds)
def test_result_is_immutable_under_a_pinned_as_of(seed):
    """The bi-temporal contract: an as_of pinned before the last write batch
    ignores it, and equals the same query on the store as it then was."""
    adapter, oracle = build_store(seed)
    pre = max(v.tt_s for v in oracle.ev) - 1
    args = {"window": W, "limit": 10_000, "as_of_tt": pre,
            "group_by": [{"dim": "rel_type"}], "aggregates": [{"agg": "count"}]}
    engine = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(
        oracle.aggregate_events(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ("op", "args_echo", "dataset_extent",
                            "result_digest")}
    assert canonical_json(payload) == canonical_json(expected)


# ---- argument contract ----------------------------------------------------- #

def test_argument_contract():
    adapter, _ = build_store(0)

    def bad(match, **kw):
        with pytest.raises((InvalidArgError, SchemaError, LimitError),
                           match=match):
            call_operator(adapter, "aggregate_events",
                          {"window": W, "aggregates": [{"agg": "count"}],
                           "group_by": [], **kw})

    bad("requires 'stride'", group_by=[{"dim": "time_bucket"}])
    bad("requires a time_bucket", stride=5)
    bad("duplicate dimension",
        group_by=[{"dim": "rel_type"}, {"dim": "rel_type"}])
    bad("requires role", group_by=[{"dim": "endpoint"}])
    bad("takes no role", group_by=[{"dim": "rel_type", "role": "src"}])
    bad("takes no 'of'", aggregates=[{"agg": "count", "of": "src"}])
    bad("requires of 'src' or 'dst'", aggregates=[{"agg": "count_distinct"}])
    # D-052 widened the legal sources to include 'prop'; the message names
    # all three so the repair loop can act on it
    bad(r"requires of 'vt_s', 'duration' or 'prop'",
        aggregates=[{"agg": "mean", "of": "src"}])
    bad("duplicate aggregate",
        aggregates=[{"agg": "count"}, {"agg": "count"}])
    bad("exceeds cap", group_by=[{"dim": "time_bucket"}], stride=1,
        window={"t_a": 0, "t_b": 10_000_000})
    with pytest.raises(SchemaError):
        call_operator(adapter, "aggregate_events",
                      {"window": W, "aggregates": [{"agg": "count"}],
                       "group_by": [{"dim": "rel_type"}] * 3})


def test_open_ended_rows_are_excluded_from_duration_only():
    """An open-ended edge counts as an event but contributes no duration; a
    group of only open-ended rows has null duration aggregates."""
    adapter = fresh_adapter()
    _apply(adapter, [{"op": "assert_edge", "src": "a", "dst": "b",
                        "rel_type": "R", "props": {}, "vt_s": 5,
                        "vt_e": OPEN_END, "disc": ""}], 1)
    _apply(adapter, [{"op": "assert_edge", "src": "a", "dst": "c",
                        "rel_type": "S", "props": {}, "vt_s": 5,
                        "vt_e": 9, "disc": ""}], 2)
    out = _call(adapter, group_by=[{"dim": "rel_type"}],
                aggregates=[{"agg": "count"}, {"agg": "mean", "of": "duration"},
                            {"agg": "min", "of": "duration"}])
    by_rel = {r["rel_type"]: r for r in out["rows"]}
    assert by_rel["R"]["count"] == 1
    assert by_rel["R"]["mean_duration"] is None
    assert by_rel["R"]["min_duration"] is None
    assert by_rel["S"]["mean_duration"] == 4.0
    assert by_rel["S"]["min_duration"] == 4


def test_ungrouped_call_returns_one_row_even_when_empty():
    adapter = fresh_adapter()
    _apply(adapter, [{"op": "assert_edge", "src": "a", "dst": "b",
                        "rel_type": "R", "props": {}, "vt_s": 50,
                        "vt_e": 55, "disc": ""}], 1)
    out = call_operator(adapter, "aggregate_events",
                        {"window": {"t_a": 0, "t_b": 10}, "group_by": [],
                         "aggregates": [{"agg": "count"},
                                        {"agg": "count_distinct", "of": "src"},
                                        {"agg": "min", "of": "vt_s"}]})
    assert out["rows"] == [{"count": 0, "distinct_src": 0, "min_vt_s": None}]
    assert out["rows_total"] == 1
    # and a grouped call over the same empty window returns no rows
    out = call_operator(adapter, "aggregate_events",
                        {"window": {"t_a": 0, "t_b": 10},
                         "group_by": [{"dim": "rel_type"}],
                         "aggregates": [{"agg": "count"}]})
    assert out["rows"] == [] and out["rows_total"] == 0
