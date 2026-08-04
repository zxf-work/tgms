"""The `SET` capability (D-054): set operations, a uid pre-filter, and pairs.

`SET` was 41 blocked and 16 sole after D-053 — more than twice the next tag.
Reading those sixteen, it is not one capability but three, with very
different costs, and this suite pins all three:

  1. **set operations over uid lists** — "both gave and received" is an
     intersection, "received but never sent" a difference. Two lists
     arriving by `$ref` and one answer; no store access at all, so it lives
     in `compute` alongside the other row-shaped functions.
  2. **a uid pre-filter** on `aggregate_events` — restricting a grouping to
     a cohort computed by an earlier step. The scan has carried
     `touching_ids` all along; the operator simply never offered it.
  3. **pair modes** — `undirected` merges A->B with B->A, `reciprocal` keeps
     only pairs where both directions exist. Both are decided over the
     *whole* group set before pagination, so neither can be defeated by a
     page boundary the way a `$ref`-level transpose would be.

Why the pair work is not in `compute`: reciprocity is not decomposable per
page. A pair's transpose can sit on any page, so a `$ref` list truncated at
10,000 would silently produce a wrong count — the failure this suite exists
to make impossible.
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

from .conftest import fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

T_MAX = 60
WINDOW = {"t_a": 0, "t_b": T_MAX}

#: (src, dst, vt, rating). Built so every pair shape is present:
#: u0<->u1 reciprocal both positive, u1<->u2 reciprocal mixed signs,
#: u3->u4 one-way only, u5->u5 a self-pair.
ROWS: list[tuple[str, str, int, int]] = [
    ("u0", "u1", 1, 2), ("u1", "u0", 2, 3),      # reciprocal, both positive
    ("u1", "u2", 3, 5), ("u2", "u1", 4, -4),     # reciprocal, mixed
    ("u0", "u1", 5, 1),                          # a second A->B
    ("u3", "u4", 6, 1),                          # one-way
    ("u5", "u5", 7, 1),                          # self-pair
]


def build_store():
    a = fresh_adapter()
    tt = 0
    for src, dst, vt, rating in ROWS:
        tt += 1
        a.begin()
        try:
            a.apply_ops([{"op": "assert_edge", "src": src, "dst": dst,
                          "rel_type": "RATES", "props": {"rating": rating},
                          "vt_s": vt, "vt_e": vt + 1, "disc": str(vt)}], tt)
        except Exception:
            a.rollback()
            raise
        a.commit()
    return a, Oracle(list(a.all_node_versions()), list(a.all_edge_versions()))


_cache: list[Any] = []


def store():
    if not _cache:
        _cache.extend(build_store())
    return _cache[0], _cache[1]


def compute(**args) -> dict[str, Any]:
    return call_operator(store()[0], "compute", args)


def agg(**over) -> dict[str, Any]:
    a = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": dict(WINDOW), **over}
    return call_operator(store()[0], "aggregate_events", a)


PAIRS = [{"dim": "endpoint", "role": "src"}, {"dim": "endpoint", "role": "dst"}]


# --- 1. set operations over uid lists --------------------------------------- #

def test_set_ops_over_bare_lists():
    a, b = ["u0", "u1", "u2"], ["u1", "u2", "u3"]
    assert compute(fn="intersect", input=a, other=b)["rows"] == ["u1", "u2"]
    assert compute(fn="difference", input=a, other=b)["rows"] == ["u0"]
    assert compute(fn="union", input=a, other=b)["rows"] == \
        ["u0", "u1", "u2", "u3"]


def test_set_ops_are_deduplicated_and_ordered():
    """The answer is a *set*: duplicates collapse and the order is the
    canonical one, so the digest does not depend on input order."""
    r = compute(fn="union", input=["b", "a", "b"], other=["a", "c"])
    assert r["rows"] == ["a", "b", "c"]
    assert r["rows_total"] == 3
    flipped = compute(fn="union", input=["c", "a"], other=["b", "a", "b"])
    assert flipped["rows"] == r["rows"]


def test_set_ops_project_a_field_from_rows():
    """The shape plans actually use: two groupings feeding their `src`/`dst`
    columns straight in, without an intervening projection step."""
    left = [{"src": "u0", "count": 3}, {"src": "u1", "count": 1}]
    right = [{"dst": "u1"}, {"dst": "u9"}]
    assert compute(fn="intersect", input=left, other=right,
                   field="src", other_field="dst")["rows"] == ["u1"]


def test_difference_is_not_symmetric():
    a, b = ["u0", "u1"], ["u1", "u2"]
    assert compute(fn="difference", input=a, other=b)["rows"] == ["u0"]
    assert compute(fn="difference", input=b, other=a)["rows"] == ["u2"]


@SETTINGS
@given(a=st.lists(st.text(min_size=1, max_size=3), max_size=20),
       b=st.lists(st.text(min_size=1, max_size=3), max_size=20))
def test_set_ops_match_python_sets(a, b):
    for fn, expected in (("intersect", set(a) & set(b)),
                         ("difference", set(a) - set(b)),
                         ("union", set(a) | set(b))):
        assert compute(fn=fn, input=a, other=b)["rows"] == sorted(expected)


@SETTINGS
@given(a=st.lists(st.text(min_size=1, max_size=3), max_size=15),
       b=st.lists(st.text(min_size=1, max_size=3), max_size=15))
def test_inclusion_exclusion_holds(a, b):
    """|A| + |B| = |A∪B| + |A∩B| — a property no implementation detail can
    satisfy by accident."""
    n = lambda **k: compute(**k)["rows_total"]  # noqa: E731
    assert len(set(a)) + len(set(b)) == \
        n(fn="union", input=a, other=b) + n(fn="intersect", input=a, other=b)


# --- 2. the uid pre-filter -------------------------------------------------- #

def test_endpoint_filter_restricts_by_role():
    """`aggregate_events` has never had a uid pre-filter; the C14 header says
    so explicitly, and it is why a cohort computed by one step could not
    restrict the next."""
    assert agg(endpoint_filter={"role": "src", "uids": ["u0"]}
               )["rows"][0]["count"] == 2          # u0->u1 twice
    assert agg(endpoint_filter={"role": "dst", "uids": ["u0"]}
               )["rows"][0]["count"] == 1          # u1->u0 once
    assert agg(endpoint_filter={"role": "either", "uids": ["u0"]}
               )["rows"][0]["count"] == 3


def test_endpoint_filter_composes_with_everything_else():
    r = agg(endpoint_filter={"role": "src", "uids": ["u0", "u1"]},
            prop_filter={"prop": "rating", "cmp": "gt", "value": 2},
            aggregates=[{"agg": "count"}])
    assert r["rows"][0]["count"] == 2               # u1->u0 (3), u1->u2 (5)


def test_an_empty_cohort_is_an_empty_population_not_an_error():
    """A cohort computed upstream can legitimately come back empty; the
    ungrouped call still emits its one row (D-044)."""
    r = agg(endpoint_filter={"role": "src", "uids": []})
    assert r["rows"] == [{"count": 0}]


def test_unknown_uids_in_the_cohort_are_not_an_error():
    r = agg(endpoint_filter={"role": "src", "uids": ["u0", "nobody"]})
    assert r["rows"][0]["count"] == 2


# --- 3. pair modes ---------------------------------------------------------- #

def test_undirected_merges_both_directions():
    r = agg(group_by=PAIRS, pair_mode="undirected",
            aggregates=[{"agg": "count"}])
    got = {(x["src"], x["dst"]): x["count"] for x in r["rows"]}
    assert got == {("u0", "u1"): 3, ("u1", "u2"): 2,
                   ("u3", "u4"): 1, ("u5", "u5"): 1}


def test_reciprocal_keeps_only_pairs_with_both_directions():
    r = agg(group_by=PAIRS, pair_mode="reciprocal",
            aggregates=[{"agg": "count"}])
    got = {(x["src"], x["dst"]): x["count"] for x in r["rows"]}
    # u3->u4 is one-way and drops; the self-pair is its own transpose
    assert got == {("u0", "u1"): 3, ("u1", "u2"): 2, ("u5", "u5"): 1}


def test_reciprocal_is_decided_before_pagination():
    """The property that makes this an operator rather than a `compute`
    function: a pair's transpose may live on any page, so a limit must not
    change which pairs qualify."""
    full = agg(group_by=PAIRS, pair_mode="reciprocal",
               aggregates=[{"agg": "count"}])
    paged = agg(group_by=PAIRS, pair_mode="reciprocal",
                aggregates=[{"agg": "count"}], limit=1)
    assert paged["rows_total"] == full["rows_total"]
    assert paged["rows"][0] == full["rows"][0]


def test_reciprocal_composes_with_a_property_predicate():
    """bo-Q25's shape: filter to positive ratings first, and a reciprocal
    pair among them is one where *both* rated the other positively."""
    both_positive = agg(group_by=PAIRS, pair_mode="reciprocal",
                        prop_filter={"prop": "rating", "cmp": "gt", "value": 0},
                        aggregates=[{"agg": "count"}])
    # u1<->u2 loses its negative direction and stops being reciprocal
    assert {(x["src"], x["dst"]) for x in both_positive["rows"]} == \
        {("u0", "u1"), ("u5", "u5")}


@SETTINGS
@given(mode=st.sampled_from(["undirected", "reciprocal"]))
def test_pair_modes_match_the_oracle(mode):
    adapter, oracle = store()
    args = {"group_by": PAIRS, "window": dict(WINDOW), "pair_mode": mode,
            "aggregates": [{"agg": "count"}, {"agg": "min", "of": "vt_s"}]}
    got = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(oracle.aggregate_events(got["args_echo"]))
    payload = {k: v for k, v in got.items()
               if k not in ("op", "args_echo", "dataset_extent", "result_digest")}
    assert canonical_json(payload) == canonical_json(expected)


def test_reciprocal_never_exceeds_undirected():
    u = agg(group_by=PAIRS, pair_mode="undirected",
            aggregates=[{"agg": "count"}])["rows_total"]
    r = agg(group_by=PAIRS, pair_mode="reciprocal",
            aggregates=[{"agg": "count"}])["rows_total"]
    assert r <= u


# --- argument contract ------------------------------------------------------ #

def test_argument_contract():
    # set ops need both operands
    for fn in ("intersect", "difference", "union"):
        with pytest.raises(InvalidArgError):
            compute(fn=fn, input=["a"])
        with pytest.raises(InvalidArgError):
            compute(fn=fn, other=["a"])
    # a field named on rows that lack it is an error, not a silent skip
    with pytest.raises(InvalidArgError):
        compute(fn="intersect", input=[{"a": 1}], other=["x"], field="missing")
    # set members must be hashable scalars, not rows
    with pytest.raises(InvalidArgError):
        compute(fn="union", input=[{"a": 1}], other=["x"])

    # a pair mode is only meaningful over an (src, dst) grouping
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(pair_mode="reciprocal")
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(group_by=[{"dim": "endpoint", "role": "src"}],
            pair_mode="reciprocal")
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(group_by=PAIRS, pair_mode="sideways")
    # the pre-filter needs a legal role
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(endpoint_filter={"role": "middle", "uids": ["u0"]})
    with pytest.raises((InvalidArgError, SchemaError)):
        agg(endpoint_filter={"uids": ["u0"]})


def test_the_new_surface_is_advertised():
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools
    enum = REGISTRY["compute"].args_schema["properties"]["fn"]["enum"]
    for fn in ("intersect", "difference", "union"):
        assert fn in enum
    shown = {t["name"]: t for t in anthropic_tools()}
    assert "intersect" in shown["compute"]["description"]
    a = shown["aggregate_events"]
    assert "pair_mode" in a["input_schema"]["properties"]
    assert "endpoint_filter" in a["input_schema"]["properties"]
    assert "reciprocal" in a["description"]
