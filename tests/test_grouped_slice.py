"""`topk` ranks within each group (D-068) — `GSLICE`, the half D-067 deferred.

D-067 built the reduction half of grouping a result and deferred the slice.
Its own re-audit then found that the two questions still blocked were blocked
on exactly that, under two different tags: bo-Q52 wants the `src` that
achieved a per-group minimum (filed under `G`) and bo-Q47 wants each
account's first five and last five ratings by time (filed under `SEQ`).

Nothing here is a new semantics. Every rule is `topk`'s, applied per group:

  * the order is `(-value, str(row))`, the tiebreak D-060 stated once;
  * `pct` is `ceil` of **that group's own** size, so each group gets a slice
    proportional to itself — a global count would hand large groups
    everything and small groups nothing;
  * `side: "bottom"` is the complement **within the group**, which keeps
    D-060's partition property one level down;
  * a group smaller than `k` yields the whole group, as ungrouped `min(k, n)`.

It returns ROWS, not one row per group, and that is what makes it compose:
slice each account's first five, then reduce them with D-067's grouped mean.
The two halves of one capability chain.
"""

from __future__ import annotations

import math
import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError
from tgms.temporal.algebra import call_operator, ensure_all_registered

from .conftest import fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

_adapter: list[Any] = []


def adapter():
    if not _adapter:
        _adapter.append(fresh_adapter())
    return _adapter[0]


def sliced(**args) -> list[dict]:
    return call_operator(adapter(), "compute",
                         {"fn": "topk", "limit": 10_000, **args})["rows"]


def rows_of(*triples) -> list[dict]:
    return [{"who": w, "t": t, "v": v} for w, t, v in triples]


# --- the reference ---------------------------------------------------------- #

def ref_slice(rows, key, field, k=None, pct=None, side="top"):
    """Per group, independently: sort by the published order and cut."""
    out = []
    for g in sorted({r[key] for r in rows}, key=lambda v: (type(v).__name__, v)):
        members = [r for r in rows if r[key] == g]
        order = sorted(members, key=lambda r: (-r[field], str(r)))
        n = len(order)
        if pct is not None:
            cut = math.ceil(n * pct / 100) if side == "top" \
                else math.ceil(n * (100 - pct) / 100)
        else:
            cut = min(k, n) if side == "top" else max(0, n - k)
        out.extend(order[:cut] if side == "top" else order[cut:])
    return out


# --- oracle cases ----------------------------------------------------------- #

ROWS = rows_of(("a", 1, 10), ("a", 2, 20), ("a", 3, 30),
               ("b", 1, 5), ("b", 2, 15))


def test_the_top_one_of_each_group():
    got = sliced(input=ROWS, group_by="who", field="t", k=1)
    assert [(r["who"], r["t"]) for r in got] == [("a", 3), ("b", 2)]


def test_the_bottom_one_of_each_group_is_the_argmin_row():
    """bo-Q52 in miniature: the earliest row per group, carrying every column
    it had — which is how an argmin returns a sibling value without a reducer
    that knows how to."""
    got = sliced(input=ROWS, group_by="who", field="t", k=1, side="bottom")
    assert [(r["who"], r["t"], r["v"]) for r in got] == [("a", 1, 10),
                                                         ("b", 1, 5)]


def test_a_group_smaller_than_k_yields_the_whole_group():
    got = sliced(input=ROWS, group_by="who", field="t", k=99)
    assert len(got) == len(ROWS)


def test_pct_is_per_group_not_global():
    """Group a has 3 rows and group b has 2. 50% is 2 and 1 — proportional to
    each group, not a single count shared out."""
    got = sliced(input=ROWS, group_by="who", field="t", pct=50)
    per = {}
    for r in got:
        per[r["who"]] = per.get(r["who"], 0) + 1
    assert per == {"a": 2, "b": 1}


def test_the_slice_is_ordered_by_group_then_rank():
    got = sliced(input=ROWS, group_by="who", field="t", k=2)
    assert [(r["who"], r["t"]) for r in got] == [("a", 3), ("a", 2),
                                                 ("b", 2), ("b", 1)]


# --- the properties --------------------------------------------------------- #

triples = st.lists(
    st.tuples(st.sampled_from("abc"), st.integers(0, 30), st.integers(-9, 9)),
    min_size=1, max_size=30)


@SETTINGS
@given(rows=triples, pct=st.sampled_from([10, 25, 50, 75, 100]))
def test_top_and_bottom_partition_every_group(rows, pct):
    """D-060's property, one level down: within every group, the top p% and
    the bottom (100-p)% cover that group exactly once."""
    data = rows_of(*rows)
    top = sliced(input=data, group_by="who", field="t", pct=pct)
    bottom = sliced(input=data, group_by="who", field="t", pct=100 - pct,
                    side="bottom") if pct < 100 else []
    assert len(top) + len(bottom) == len(data)
    seen = [str(r) for r in top] + [str(r) for r in bottom]
    assert sorted(seen) == sorted(str(r) for r in data)


@SETTINGS
@given(rows=triples, k=st.integers(1, 6))
def test_matches_the_reference(rows, k):
    data = rows_of(*rows)
    for side in ("top", "bottom"):
        assert sliced(input=data, group_by="who", field="t", k=k,
                      side=side) == ref_slice(data, "who", "t", k=k, side=side)


@SETTINGS
@given(rows=triples, k=st.integers(1, 6))
def test_grouping_by_a_constant_is_the_ungrouped_slice(rows, k):
    """Pins the grouped path against the ungrouped one."""
    data = [{"who": "same", "t": t, "v": v} for _, t, v in rows]
    assert sliced(input=data, group_by="who", field="t", k=k) == \
        sliced(input=data, field="t", k=k)


@SETTINGS
@given(rows=triples, seed=st.integers(0, 10**6))
def test_the_slice_does_not_depend_on_input_order(rows, seed):
    import random
    data = rows_of(*rows)
    shuffled = data[:]
    random.Random(seed).shuffle(shuffled)
    assert sliced(input=data, group_by="who", field="t", k=2) == \
        sliced(input=shuffled, group_by="who", field="t", k=2)


# --- what it refuses -------------------------------------------------------- #

def test_a_missing_group_column_is_refused():
    with pytest.raises(InvalidArgError, match="nope"):
        sliced(input=ROWS, group_by="nope", field="t", k=1)


def test_group_by_still_requires_a_field_to_rank_by():
    with pytest.raises(InvalidArgError, match="field"):
        sliced(input=ROWS, group_by="who", k=1)


def test_group_by_is_advertised_on_topk():
    from tgms.tools.schemas import anthropic_tools

    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    assert "group_by" in shown["description"]


# --- the two questions ------------------------------------------------------ #

def test_bo_Q52_the_first_rater_of_each_account():
    """The argmin that returns a sibling column, which `G` named for eight
    sessions without anyone noticing it was a slice."""
    ratings = [{"src": "a", "dst": "x", "t": 5}, {"src": "b", "dst": "x", "t": 2},
               {"src": "c", "dst": "y", "t": 9}, {"src": "d", "dst": "y", "t": 1}]
    first = sliced(input=ratings, group_by="dst", field="t", k=1, side="bottom")
    assert [(r["dst"], r["src"]) for r in first] == [("x", "b"), ("y", "d")]


def test_bo_Q47_first_five_and_last_five_per_account():
    """Slice, then reduce with D-067's grouped mean — the two halves chaining,
    which is the whole argument for building them as one capability."""
    rows = [{"who": "a", "t": i, "v": (1 if i < 5 else 9)} for i in range(10)]
    first5 = sliced(input=rows, group_by="who", field="t", k=5, side="bottom")
    last5 = sliced(input=rows, group_by="who", field="t", k=5)
    assert {r["t"] for r in first5} == {0, 1, 2, 3, 4}
    assert {r["t"] for r in last5} == {5, 6, 7, 8, 9}
    mean = lambda rs: call_operator(  # noqa: E731
        adapter(), "compute",
        {"fn": "mean", "input": rs, "group_by": "who", "field": "v"})["rows"]
    assert mean(first5) == [{"who": "a", "mean": 1}]
    assert mean(last5) == [{"who": "a", "mean": 9}]
