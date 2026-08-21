"""Property typing (D-052): reading a value out of untyped JSON props.

D-044 deferred numeric-property aggregates because "mean of rating" would
either silently coerce or silently skip, and both are wrong. D-052 chose the
third thing: coerce **by JSON type, never by parsing text**, and make every
row that fails to qualify visible in the answer rather than absent from it.

THE CONTRACT THESE TESTS PIN

  * a value participates iff its JSON type matches what the call asks of it —
    a number for min/max/mean, and the same type class as the literal for a
    predicate. `"3"` is not `3`; `true` is not `1`; `null` and a missing key
    are simply absent;
  * every row excluded that way is **counted**, per property, in the
    `prop_coercion` payload field — which is inside `result_digest`, so an
    answer cannot quietly rest on a shrunken denominator;
  * the arithmetic is D-051's blessed rule, unchanged: integer props give
    exact integer answers, and anything inexact spends exactly one rounding;
  * a group with no contributing rows is `null`, which is D-044's existing
    empty-aggregate semantics rather than a new one.

The oracle is the third implementation, as everywhere else here: it reads
`EdgeVersion.props` directly, in a Python loop, with no columnar scan and no
kernel between it and the answer.
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

T_MAX = 60
WINDOW = {"t_a": 0, "t_b": T_MAX}

#: One row per JSON shape a real property bag contains. The ratings are the
#: Bitcoin-OTC shape (small signed integers); the rest are the ways a value
#: fails to be one.
ROWS: list[tuple[str, str, int, dict[str, Any]]] = [
    ("u0", "u1", 1, {"rating": 2}),            # int: participates
    ("u1", "u2", 2, {"rating": -1}),           # negative int
    ("u2", "u3", 3, {"rating": 5}),
    ("u3", "u4", 4, {"rating": 1.5}),          # float: participates
    ("u4", "u5", 5, {"rating": "3"}),          # STRING: never parsed
    ("u5", "u6", 6, {"rating": True}),         # BOOL: not a number
    ("u6", "u7", 7, {"rating": None}),         # explicit null
    ("u7", "u0", 8, {"other": 9}),             # key absent
    ("u0", "u2", 9, {}),                       # no props at all
]
#: The four that a numeric aggregate may see, and the five it may not.
NUMERIC = [2, -1, 5, 1.5]
N_SKIPPED = 5


def build_store():
    a = fresh_adapter()
    tt = 0
    for src, dst, vt, props in ROWS:
        tt += 1
        a.begin()
        try:
            a.apply_ops([{"op": "assert_edge", "src": src, "dst": dst,
                          "rel_type": "RATES", "props": props,
                          "vt_s": vt, "vt_e": vt + 1, "disc": ""}], tt)
        except Exception:
            a.rollback()
            raise
        a.commit()
    oracle = Oracle(list(a.all_node_versions()), list(a.all_edge_versions()))
    return a, oracle


_cache: list[Any] = []


def store():
    if not _cache:
        _cache.extend(build_store())
    return _cache[0], _cache[1]


def agg(**over) -> dict[str, Any]:
    args = {"group_by": [], "aggregates": [{"agg": "count"}],
            "window": dict(WINDOW), **over}
    return call_operator(store()[0], "aggregate_events", args)


def one_row(**over) -> dict[str, Any]:
    return agg(**over)["rows"][0]


# --- the coercion rule ------------------------------------------------------ #

def test_only_json_numbers_participate():
    """The four numeric rows are the whole population; the string, the bool,
    the null, the absent key and the empty bag are all excluded."""
    r = one_row(aggregates=[{"agg": "count"},
                            {"agg": "mean", "of": "prop", "prop": "rating"},
                            {"agg": "min", "of": "prop", "prop": "rating"},
                            {"agg": "max", "of": "prop", "prop": "rating"}])
    assert r["count"] == len(ROWS)              # count is never reduced
    assert r["min_prop_rating"] == -1
    assert r["max_prop_rating"] == 5
    assert r["mean_prop_rating"] == pytest.approx(sum(NUMERIC) / len(NUMERIC))


def test_a_numeric_string_is_never_parsed():
    """`"3"` would be the single largest rating if it were parsed. It is not,
    and that is the whole point of the rule."""
    assert one_row(aggregates=[{"agg": "max", "of": "prop",
                                "prop": "rating"}])["max_prop_rating"] == 5


def test_a_boolean_is_not_a_number():
    """`True == 1` in Python, and a bool reaching an arithmetic path is a
    data bug rather than a 1."""
    r = one_row(aggregates=[{"agg": "count"},
                            {"agg": "min", "of": "prop", "prop": "rating"}])
    assert r["min_prop_rating"] == -1           # not True/1


def test_excluded_rows_are_counted_not_silent():
    res = agg(aggregates=[{"agg": "mean", "of": "prop", "prop": "rating"}])
    assert res["prop_coercion"] == {"rating": N_SKIPPED}


def test_the_count_is_inside_the_digest():
    """A skipped-row count that is not hashed can be dropped on the way to an
    answer; this is what stops that."""
    res = agg(aggregates=[{"agg": "mean", "of": "prop", "prop": "rating"}])
    payload = {k: v for k, v in res.items()
               if k not in ENVELOPE_META_KEYS}
    assert "prop_coercion" in payload
    assert "prop_coercion" in canonical_json(payload)


def test_a_property_nobody_has_is_all_skipped_and_null():
    res = agg(aggregates=[{"agg": "mean", "of": "prop", "prop": "nope"}])
    assert res["prop_coercion"] == {"nope": len(ROWS)}
    assert res["rows"][0]["mean_prop_nope"] is None      # D-044 empty semantics


# --- the arithmetic is D-051's, unchanged ----------------------------------- #

def test_integer_properties_stay_exact():
    """Same rule as `compute`: exact where the inputs are integral, so a
    property mean over large integers cannot drift into a float. Excluding
    the one float row leaves {2, -1, 5}, whose mean is exactly 2."""
    r = one_row(prop_filter={"prop": "rating", "cmp": "ne", "value": 1.5},
                aggregates=[{"agg": "mean", "of": "prop", "prop": "rating"}])
    assert r["mean_prop_rating"] == 2
    assert isinstance(r["mean_prop_rating"], int)   # not 2.0


def test_mean_spends_one_rounding_when_it_must():
    """{2, 5} averages to 3.5 — inexact, so exactly one IEEE rounding, in
    the same form `compute` uses."""
    from tgms.temporal.ops_compute import _mean as blessed
    r = one_row(prop_filter={"prop": "rating", "cmp": "ge", "value": 2},
                aggregates=[{"agg": "mean", "of": "prop", "prop": "rating"}])
    assert r["mean_prop_rating"] == 3.5 == blessed([2, 5])


# --- the predicate ---------------------------------------------------------- #

def test_predicate_selects_by_value():
    r = one_row(prop_filter={"prop": "rating", "cmp": "gt", "value": 0},
                aggregates=[{"agg": "count"}])
    assert r["count"] == 3                      # 2, 5, 1.5 — not "3", not True


def test_predicate_type_mismatch_excludes_and_counts():
    """Comparing a stored string to a numeric literal is a type error, not a
    comparison; the row leaves the population and is counted."""
    res = agg(prop_filter={"prop": "rating", "cmp": "gt", "value": 0},
              aggregates=[{"agg": "count"}])
    # exactly the five that are not numbers. `-1` is a number whose
    # comparison simply returned False — a filtered row, not a coerced one,
    # and conflating the two would make the count meaningless.
    assert res["prop_coercion"]["rating"] == N_SKIPPED
    assert res["rows"][0]["count"] == 3


def test_predicate_on_a_string_property_is_a_string_comparison():
    """The rule is same-type-class, not numbers-only: a string literal
    against a string value is a legitimate predicate."""
    r = one_row(prop_filter={"prop": "rating", "cmp": "eq", "value": "3"},
                aggregates=[{"agg": "count"}])
    assert r["count"] == 1                      # the row nothing else can use


# --- oracle equivalence and determinism ------------------------------------- #

prop_aggs = st.sampled_from(["min", "max", "mean"])
cmps = st.sampled_from(["eq", "ne", "lt", "le", "gt", "ge"])


@SETTINGS
@given(a=prop_aggs, cmp=cmps, v=st.integers(-3, 6))
def test_matches_the_oracle(a, cmp, v):
    adapter, oracle = store()
    args = {"group_by": [], "window": dict(WINDOW),
            "aggregates": [{"agg": "count"},
                           {"agg": a, "of": "prop", "prop": "rating"}],
            "prop_filter": {"prop": "rating", "cmp": cmp, "value": v}}
    got = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(oracle.aggregate_events(got["args_echo"]))
    payload = {k: v2 for k, v2 in got.items()
               if k not in ENVELOPE_META_KEYS}
    assert canonical_json(payload) == canonical_json(expected)


@SETTINGS
@given(a=prop_aggs)
def test_grouped_property_aggregates_match_the_oracle(a):
    adapter, oracle = store()
    args = {"group_by": [{"dim": "endpoint", "role": "src"}],
            "window": dict(WINDOW),
            "aggregates": [{"agg": a, "of": "prop", "prop": "rating"}]}
    got = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(oracle.aggregate_events(got["args_echo"]))
    payload = {k: v for k, v in got.items()
               if k not in ENVELOPE_META_KEYS}
    assert canonical_json(payload) == canonical_json(expected)


def test_result_is_byte_identical_on_repeat():
    a = {"aggregates": [{"agg": "mean", "of": "prop", "prop": "rating"}]}
    assert agg(**a)["result_digest"] == agg(**a)["result_digest"]


# --- argument contract ------------------------------------------------------ #

def test_argument_contract():
    # `of: prop` requires a property name, and a name requires `of: prop`
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(aggregates=[{"agg": "mean", "of": "prop"}])
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(aggregates=[{"agg": "mean", "of": "vt_s", "prop": "rating"}])
    # count and count_distinct take no property
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(aggregates=[{"agg": "count", "of": "prop", "prop": "rating"}])
    # two aggregates over the same property collide on the output field
    with pytest.raises(InvalidArgError):
        agg(aggregates=[{"agg": "mean", "of": "prop", "prop": "rating"},
                        {"agg": "mean", "of": "prop", "prop": "rating"}])
    # a property named like a built-in source must not collide with it
    r = one_row(aggregates=[{"agg": "max", "of": "vt_s"},
                            {"agg": "max", "of": "prop", "prop": "vt_s"}])
    assert "max_vt_s" in r and "max_prop_vt_s" in r
    # the predicate needs a comparison
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(prop_filter={"prop": "rating"})


def test_props_are_opt_in_on_the_columnar_scan():
    """The expensive column stays unmaterialized unless asked for — the
    projection-pushdown lesson, which a default-on props column would undo."""
    adapter, _ = store()
    assert "props" not in adapter.edges_columnar(vt_min=0, vt_max=T_MAX)
    got = adapter.edges_columnar(vt_min=0, vt_max=T_MAX,
                                 columns=("vt_s", "props"))
    assert "props" in got and len(got["props"]) == len(ROWS)
