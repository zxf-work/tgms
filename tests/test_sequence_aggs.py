"""Sequence aggregates (D-056): gaps, sliding windows, and sessions.

`SEQ` blocks 14 of the 110 independent questions and is the sole blocker of
8 — the largest single item left on the board. Read from the questions
rather than from the tag, it is three walks over the event times that
`aggregate_events` already holds grouped and unreduced at exactly the right
moment:

  * `max_gap` — the longest hole between two consecutive events in a group
    (bo-Q17, "the longest gap between two consecutive ratings").
  * `max_in_window` — the largest number of events in any window of a given
    span (bo-Q18 "more than 5 ratings within a single 24-hour period",
    cm-Q9 "any 24-hour period where one account sent more than 100"). Not a
    special case of the first, and not what a `time_bucket` grouping does:
    buckets are a fixed stride anchored at `t_a`, and "any 24-hour period"
    is any 24 hours.
  * `max_session_span` — the longest run of events with no consecutive gap
    over a threshold (cm-Q35's "conversation chain ... where no gap between
    consecutive messages exceeds 60 minutes").

**The rule for an absent value, and why the three do not agree.** A count is
0 over an empty population; a measurement of something that has to exist is
null when it does not. So `max_in_window` is 0 for a group with no events —
every window holds none, which is a fact and not a missing answer —
`max_gap` is null below two events because there is no consecutive pair, and
`max_session_span` is null only on an empty group, because one event is
already a run and its span is 0.

Every gap is measured *inside a group and inside the window*: an event in
the next bucket does not close this bucket's gap, and neither does an event
just outside `window`. That is grouping working as specified rather than a
special case, and the last test here pins it so nobody has to rediscover it.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.core.model import canonical_json
from tgms.temporal.algebra import (
    _canonicalize_floats,
    call_operator,
    ensure_all_registered,
)
from tgms.temporal.oracle import Oracle

from .conftest import ENVELOPE_META_KEYS, fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

W = {"t_a": 0, "t_b": 1_000}


def store(times: list[int], src: str = "a", dst: str = "b",
          rel: str = "R") -> Any:
    """One instantaneous event per time, all on the same edge unless told
    otherwise — the shape every sequence question actually has."""
    adapter = fresh_adapter()
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel,
          "props": {}, "vt_s": t, "vt_e": t + 1, "disc": str(i)}
         for i, t in enumerate(times)], 1)
    adapter.commit()
    return adapter


def agg(adapter, aggregates: list[dict[str, Any]], **kw) -> list[dict]:
    return call_operator(adapter, "aggregate_events",
                         {"window": W, "group_by": [], "limit": 10_000,
                          "aggregates": aggregates, **kw})["rows"]


GAP = [{"agg": "max_gap"}]


# ---- the absent value, which is the whole edge-case surface --------------- #

def test_one_event_has_no_gap_but_is_a_session():
    """A group with a single event: `null` for the gap, because there is no
    consecutive pair to measure — not 0, which is what two simultaneous
    events mean. The run, on the other hand, exists and spans nothing."""
    rows = agg(store([500]), [{"agg": "max_gap"},
                              {"agg": "max_session_span", "gap": 10},
                              {"agg": "max_in_window", "span": 10},
                              {"agg": "count"}])
    assert rows == [{"max_gap": None, "max_session_span": 0,
                     "max_in_window": 1, "count": 1}]


def test_simultaneous_events_give_a_zero_gap_not_a_null():
    rows = agg(store([500, 500]), GAP)
    assert rows == [{"max_gap": 0}]


def test_an_empty_group_counts_zero_and_measures_null():
    """The rule, stated where it is easiest to check: over no events at all,
    the count-shaped aggregate is 0 and the two duration-shaped ones are
    null. Only `group_by: []` can produce an empty group."""
    rows = agg(store([500]), [{"agg": "max_in_window", "span": 10},
                              {"agg": "max_gap"},
                              {"agg": "max_session_span", "gap": 10},
                              {"agg": "count"}],
               window={"t_a": 0, "t_b": 10})
    assert rows == [{"max_in_window": 0, "max_gap": None,
                     "max_session_span": None, "count": 0}]


# ---- what each one actually computes -------------------------------------- #

def test_max_gap_is_the_largest_hole_and_not_the_extent():
    """Events at 0, 10, 40, 45: holes of 10, 30, 5. The extent is 45, which
    is what `max_vt_s - min_vt_s` would have given."""
    rows = agg(store([0, 10, 40, 45]),
               GAP + [{"agg": "min", "of": "vt_s"}, {"agg": "max", "of": "vt_s"}])
    assert rows == [{"max_gap": 30, "min_vt_s": 0, "max_vt_s": 45}]


def test_the_window_is_half_open():
    """`[t, t + span)`, as every window in this system is: an event exactly
    `span` later starts the next window rather than joining this one."""
    assert agg(store([0, 10]), [{"agg": "max_in_window", "span": 10}]) == \
        [{"max_in_window": 1}]
    assert agg(store([0, 9]), [{"agg": "max_in_window", "span": 10}]) == \
        [{"max_in_window": 2}]


def test_a_sliding_window_is_not_a_bucket():
    """The test that says why this is a new aggregate and not a stride.

    Events at 9, 10, 11 with `t_a = 0`. Fixed 10-wide buckets anchored at
    `t_a` split them 1/2, so the busiest *bucket* holds 2; the busiest
    10-wide *window* is [9, 19) and holds all three. bo-Q18 and cm-Q9 ask
    for the second number.
    """
    adapter = store([9, 10, 11])
    assert agg(adapter, [{"agg": "max_in_window", "span": 10}]) == \
        [{"max_in_window": 3}]
    buckets = call_operator(adapter, "aggregate_events",
                            {"window": W, "limit": 10_000, "stride": 10,
                             "group_by": [{"dim": "time_bucket"}],
                             "aggregates": [{"agg": "count"}]})["rows"]
    assert max(r["count"] for r in buckets) == 2


def test_a_session_survives_a_gap_equal_to_the_threshold():
    """"no gap exceeds 60 minutes" is `<=`, so a gap of exactly the
    threshold keeps the run together and one microsecond more splits it."""
    assert agg(store([0, 5, 10]),
               [{"agg": "max_session_span", "gap": 5}]) == \
        [{"max_session_span": 10}]
    # the same three events one unit apart at the first hop: 0 | 6, 10
    assert agg(store([0, 6, 10]),
               [{"agg": "max_session_span", "gap": 5}]) == \
        [{"max_session_span": 4}]


def test_the_longest_session_is_not_the_first_or_the_last():
    """Runs of span 2, 12 and 3, in that order, separated by gaps of 20."""
    rows = agg(store([0, 1, 2, 22, 30, 34, 54, 55, 57]),
               [{"agg": "max_session_span", "gap": 15}])
    assert rows == [{"max_session_span": 12}]


def test_gaps_are_measured_inside_the_group_and_inside_the_window():
    """Grouping cuts the sequence, and so does the window. Events at 5 and
    at 105 are 100 apart; a 100-wide bucketing puts them in different groups
    and neither group then has a gap at all."""
    adapter = store([5, 105])
    assert agg(adapter, GAP) == [{"max_gap": 100}]
    rows = call_operator(adapter, "aggregate_events",
                         {"window": W, "limit": 10_000, "stride": 100,
                          "group_by": [{"dim": "time_bucket"}],
                          "aggregates": [{"agg": "max_gap"}]})["rows"]
    assert [r["max_gap"] for r in rows] == [None, None]
    # and an event outside `window` does not close the gap either
    assert agg(adapter, GAP, window={"t_a": 0, "t_b": 100}) == \
        [{"max_gap": None}]


# ---- metamorphic: each one collapses onto something already trusted ------- #

seeds = st.integers(0, 5)
event_times = st.lists(st.integers(0, 200), min_size=0, max_size=40)


@SETTINGS
@given(times=event_times)
def test_a_window_spanning_everything_is_just_the_count(times):
    """A window at least as wide as the whole valid-time window must hold
    every event, so `max_in_window` degenerates to `count`."""
    rows = agg(store(times), [{"agg": "max_in_window", "span": W["t_b"]},
                              {"agg": "count"}])
    assert rows[0]["max_in_window"] == rows[0]["count"]


@SETTINGS
@given(times=event_times)
def test_a_threshold_above_every_gap_makes_one_session(times):
    """With a gap threshold nothing can exceed, the single run covers the
    group and its span is `max_vt_s - min_vt_s`."""
    rows = agg(store(times), [{"agg": "max_session_span", "gap": W["t_b"]},
                              {"agg": "min", "of": "vt_s"},
                              {"agg": "max", "of": "vt_s"}])
    if rows[0]["min_vt_s"] is None:
        assert rows[0]["max_session_span"] is None
    else:
        assert rows[0]["max_session_span"] == \
            rows[0]["max_vt_s"] - rows[0]["min_vt_s"]


@SETTINGS
@given(times=event_times)
def test_a_gap_never_exceeds_the_extent_and_a_session_never_does_either(times):
    rows = agg(store(times), [{"agg": "max_gap"},
                              {"agg": "max_session_span", "gap": 7},
                              {"agg": "min", "of": "vt_s"},
                              {"agg": "max", "of": "vt_s"}])
    r = rows[0]
    if r["min_vt_s"] is None:
        return
    extent = r["max_vt_s"] - r["min_vt_s"]
    assert r["max_session_span"] <= extent
    assert r["max_gap"] is None or r["max_gap"] <= extent


# ---- oracle equivalence for the compositions that force the portable path - #

@SETTINGS
@given(seed=seeds, gap=st.integers(1, 30), span=st.integers(1, 30))
def test_matches_the_oracle_alongside_the_cohort_filter(seed, gap, span):
    """These aggregates only exist on the portable path, which is also where
    `endpoint_filter` and `pair_mode` live (D-054). Compose them: a cohort
    pre-filter must narrow the population *before* the sequence is walked,
    or every gap is measured over the wrong events.
    """
    import random

    rng = random.Random(seed)
    uids = ["a", "b", "c"]
    adapter = fresh_adapter()
    ops = []
    for i in range(20):
        s, d = rng.choice(uids), rng.choice(uids)
        t = rng.randrange(0, 200)
        ops.append({"op": "assert_edge", "src": s, "dst": d, "rel_type": "R",
                    "props": {}, "vt_s": t, "vt_e": t + 1, "disc": str(i)})
    adapter.begin()
    adapter.apply_ops(ops, 1)
    adapter.commit()
    oracle = Oracle(list(adapter.all_node_versions()),
                    list(adapter.all_edge_versions()))
    args = {"window": {"t_a": 0, "t_b": 200}, "limit": 10_000,
            "group_by": [{"dim": "endpoint", "role": "src"}],
            "aggregates": [{"agg": "max_gap"},
                           {"agg": "max_in_window", "span": span},
                           {"agg": "max_session_span", "gap": gap}],
            "endpoint_filter": {"role": "either", "uids": ["a", "b"]}}
    engine = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(oracle.aggregate_events(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ENVELOPE_META_KEYS}
    assert canonical_json(payload) == canonical_json(expected)


# ---- argument contract ---------------------------------------------------- #

def test_argument_contract():
    adapter = store([1, 2, 3])

    def bad(match, aggregates):
        with pytest.raises((InvalidArgError, SchemaError), match=match):
            call_operator(adapter, "aggregate_events",
                          {"window": W, "group_by": [],
                           "aggregates": aggregates})

    bad("takes no 'of'", [{"agg": "max_gap", "of": "vt_s"}])
    bad("takes no 'of'", [{"agg": "max_in_window", "of": "vt_s", "span": 5}])
    bad("requires 'span'", [{"agg": "max_in_window"}])
    bad("requires 'gap'", [{"agg": "max_session_span"}])
    bad("'span' is only meaningful", [{"agg": "count", "span": 5}])
    bad("'gap' is only meaningful", [{"agg": "max_gap", "gap": 5}])
    bad("'span' is only meaningful", [{"agg": "max_session_span", "gap": 5,
                                       "span": 5}])
    # two spans in one call would collide on the output field; say so in the
    # terms the repair loop can act on rather than as a duplicate
    bad("one 'max_in_window' per call",
        [{"agg": "max_in_window", "span": 5},
         {"agg": "max_in_window", "span": 10}])
    with pytest.raises(SchemaError):
        call_operator(adapter, "aggregate_events",
                      {"window": W, "group_by": [],
                       "aggregates": [{"agg": "max_in_window", "span": 0}]})


def test_the_sequence_aggregates_are_advertised():
    """A capability the planner cannot see does not exist.

    `aggregate_events` has no `configs/tool_manual.yaml` entry, so its
    registry description *is* the prose the planner reads — but that is a
    fact about today's manual, not a guarantee. Assert against whatever
    `anthropic_tools` actually shows, which is the surface either way.
    """
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools

    spec = REGISTRY["aggregate_events"]
    enum = spec.args_schema["properties"]["aggregates"]["items"] \
        ["properties"]["agg"]["enum"]
    shown = [t for t in anthropic_tools() if t["name"] == "aggregate_events"][0]
    for name in ("max_gap", "max_in_window", "max_session_span"):
        assert name in enum
        assert name in spec.description
        assert name in shown["description"], f"{name} is not advertised"
    # the two spans are arguments a planner has to bind, so they need naming
    for key in ("span", "gap"):
        assert key in shown["input_schema"]["properties"]["aggregates"] \
            ["items"]["properties"]
