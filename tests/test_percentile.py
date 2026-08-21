"""`PCT` (D-060): `topk` selects by a fraction of the row count, from either
end, and the two ends partition.

The study's three blocked questions all want one thing — a slice of a prior
step's rows sized as a percentage rather than a count:

    bo-Q54  the top 10% most active accounts by total ratings given
    cm-Q52  the top 10 most active senders ... among the bottom 50% of senders
    cm-Q54  the top 1% of accounts by message volume

TWO PROPERTIES CARRY THIS FILE, and neither is a formula-shaped equality.

**The partition.** cm-Q52 divides one slice by another, so "top p%" and
"bottom (100-p)%" must cover every row exactly once. That is not free: the
obvious spelling of "bottom" — D-057's `derive mul -1` in front of `topk`,
which is how bo-Q11 was answered — gives `ceil(n/2)` rows on *both* sides, so
for odd `n` the halves overlap by one row and the ratio is over a population
that does not add up. `side: "bottom"` is therefore defined as the
*complement* of the top slice, and the property below is what says so.

**The count is exact.** `ceil(n * pct/100)` in binary floating point is off
by one whenever `n * (pct/100)` lands just above an integer: `25` rows at
`28%` is `7.000000000000001`, which ceils to 8. The boundary is the entire
subject of this operator, so the count is formed in exact rational
arithmetic and the reference below finds it by integer search instead — an
independent path to the same number, as `test_compute_arithmetic.py` does
for the quotients.
"""

from __future__ import annotations

import math
import os
import random
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.core.model import canonical_json
from tgms.temporal.algebra import call_operator, ensure_all_registered

from .conftest import ENVELOPE_META_KEYS, fresh_adapter

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


def slice_(**args) -> list[dict]:
    return call_operator(adapter(), "compute", {"fn": "topk", **args})["rows"]


def payload(**args) -> str:
    r = call_operator(adapter(), "compute", {"fn": "topk", **args})
    return canonical_json({k: v for k, v in r.items()
                           if k not in ENVELOPE_META_KEYS})


# --- the reference ---------------------------------------------------------- #

def ref_count(n: int, pct: float) -> int:
    """How many rows `pct`% of `n` takes: the smallest m whose share reaches
    `pct`. Found by search over exact integers rather than by ceil() over a
    product, so the reference cannot inherit the kernel's rounding."""
    if n == 0:
        return 0
    p = Fraction(str(pct))
    for m in range(n + 1):
        if Fraction(m, n) * 100 >= p:
            return m
    return n


def ref_order(rows: list[dict], field: str) -> list[dict]:
    """`topk`'s published total order: by value descending, ties by the row's
    canonical string. Restated here rather than imported."""
    return sorted(rows, key=lambda r: (-r[field], str(r)))


# --- what the fraction selects ---------------------------------------------- #

def rows_of(*vals) -> list[dict]:
    return [{"uid": f"u{i}", "n": v} for i, v in enumerate(vals)]


@pytest.mark.parametrize("n,pct,expected", [
    (10, 10, 1), (10, 50, 5), (10, 100, 10),
    (1, 50, 1),                       # never empty: half of one row is one row
    (3, 10, 1), (3, 1, 1),
    (1350, 1, 14), (1350, 10, 135), (1350, 50, 675),   # the canonical store
    (1899, 50, 950),                  # odd n: the top half is the larger half
    (25, 28, 7),                      # 25 * 0.28 = 7.000000000000001 in float
    (50, 14, 7), (75, 28, 21),
])
def test_the_count_is_ceil_and_is_exact(n, pct, expected):
    assert ref_count(n, pct) == expected
    got = slice_(input=rows_of(*range(n)), field="n", pct=pct, limit=10_000)
    assert len(got) == expected


def test_a_fraction_selects_the_same_rows_as_the_count_it_works_out_to():
    """The fraction is not a second ranking — it is `topk` with k derived
    from n, so it must agree row for row with the k it computes."""
    rows = rows_of(*[3, 1, 4, 1, 5, 9, 2, 6, 5, 3])
    assert slice_(input=rows, field="n", pct=30) == \
        slice_(input=rows, field="n", k=3)
    assert slice_(input=rows, field="n", pct=100) == \
        slice_(input=rows, field="n", k=10)


def test_the_bottom_is_the_complement_not_a_second_ranking():
    """The case that rules out `derive mul -1` + `topk`: with an odd row
    count both spellings of "half" want ceil(n/2) = 3 rows, and 3 + 3 > 5."""
    rows = rows_of(10, 20, 30, 40, 50)
    top = slice_(input=rows, field="n", pct=50)
    bottom = slice_(input=rows, field="n", pct=50, side="bottom")
    assert [r["n"] for r in top] == [50, 40, 30]
    assert [r["n"] for r in bottom] == [20, 10]
    assert len(top) + len(bottom) == len(rows)


def test_a_tie_across_the_boundary_is_split_by_rank_not_by_value():
    """The measured case, in miniature: the canonical store has 17 accounts
    tied at 14 messages straddling the 50% cut, 8 in and 9 out. Taking every
    row at the boundary *value* instead would put all 17 on both sides, and
    cm-Q52's ratio would be over 1367 of 1350 senders.

    Which tied row lands where is decided by `topk`'s existing
    `(-value, str(row))` tiebreak. This pins that the split happens at all,
    and that it is the same split every time."""
    rows = rows_of(9, 5, 5, 5, 1)      # three-way tie across a 40% cut
    top = slice_(input=rows, field="n", pct=40)
    assert len(top) == 2
    assert [r["n"] for r in top] == [9, 5]
    bottom = slice_(input=rows, field="n", pct=60, side="bottom")
    assert len(bottom) == 3
    assert [r["n"] for r in bottom] == [5, 5, 1]
    assert payload(input=rows, field="n", pct=40) == \
        payload(input=rows, field="n", pct=40)


def test_side_bottom_also_applies_to_an_absolute_k():
    rows = rows_of(10, 20, 30, 40, 50)
    assert [r["n"] for r in slice_(input=rows, field="n", k=2, side="bottom")] \
        == [20, 10]
    # k larger than the input is the whole input, from either end
    assert len(slice_(input=rows, field="n", k=99, side="bottom")) == 5


# --- the properties --------------------------------------------------------- #

pcts = st.sampled_from([1, 5, 10, 14, 25, 28, 50, 75, 99, 100, 0.5, 12.5, 33.3])
row_lists = st.lists(st.integers(-50, 50), min_size=1, max_size=60)


@SETTINGS
@given(vals=row_lists, pct=pcts)
def test_top_and_bottom_partition_the_input(vals, pct):
    """THE property cm-Q52 depends on: every row is in exactly one side, so a
    ratio between the two sides is over the whole population."""
    rows = rows_of(*vals)
    top = slice_(input=rows, field="n", pct=pct, limit=10_000)
    other = 100 - Fraction(str(pct))
    bottom = slice_(input=rows, field="n", pct=float(other), side="bottom",
                    limit=10_000) if other > 0 else []
    keys = [r["uid"] for r in top] + [r["uid"] for r in bottom]
    assert sorted(keys) == sorted(r["uid"] for r in rows), \
        f"pct={pct}: {len(top)} + {len(bottom)} != {len(rows)}"
    assert len(set(keys)) == len(keys), "a row landed on both sides"


@SETTINGS
@given(vals=row_lists, pct=pcts)
def test_the_count_never_depends_on_how_the_fraction_is_spelled(vals, pct):
    """`10` and `10.0` are the same fraction, and neither may inherit a
    binary rounding. The reference counts by integer search."""
    rows = rows_of(*vals)
    n = len(rows)
    got = slice_(input=rows, field="n", pct=pct, limit=10_000)
    assert len(got) == ref_count(n, pct)
    assert len(got) == len(slice_(input=rows, field="n", pct=float(pct),
                                  limit=10_000))
    assert got, "a positive fraction of a non-empty input is never empty"


@SETTINGS
@given(vals=row_lists, pct=pcts)
def test_the_slice_is_a_prefix_of_the_published_order(vals, pct):
    """Whatever the fraction, the rows are the same ones `topk` would rank —
    the operator gains a way to say how many, not a new ordering."""
    rows = rows_of(*vals)
    got = slice_(input=rows, field="n", pct=pct, limit=10_000)
    assert got == ref_order(rows, "n")[:len(got)]


@SETTINGS
@given(vals=row_lists, pct=pcts, seed=st.integers(0, 10**6))
def test_the_selection_does_not_depend_on_input_order(vals, pct, seed):
    """Permutation invariance, which is what the `str(row)` tiebreak is for:
    a slice that moved when its input was shuffled would make every digest
    downstream of it depend on scan order."""
    rows = rows_of(*vals)
    shuffled = rows[:]
    random.Random(seed).shuffle(shuffled)
    assert slice_(input=rows, field="n", pct=pct, limit=10_000) == \
        slice_(input=shuffled, field="n", pct=pct, limit=10_000)


# --- what it refuses -------------------------------------------------------- #

def test_exactly_one_of_k_or_pct():
    rows = rows_of(1, 2, 3)
    with pytest.raises(InvalidArgError, match="exactly one"):
        slice_(input=rows, field="n", k=2, pct=50)
    with pytest.raises(InvalidArgError, match="k or pct"):
        slice_(input=rows, field="n")


def test_a_fraction_outside_the_unit_range_is_refused():
    rows = rows_of(1, 2, 3)
    for bad in (0, -5, 101):
        with pytest.raises((SchemaError, InvalidArgError)):
            slice_(input=rows, field="n", pct=bad)


def test_pct_still_requires_a_field_to_rank_by():
    with pytest.raises(InvalidArgError, match="field"):
        slice_(input=rows_of(1, 2, 3), pct=50)


def test_pct_is_advertised():
    from tgms.tools.schemas import anthropic_tools

    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    assert "pct" in shown["input_schema"]["properties"]
    assert "side" in shown["input_schema"]["properties"]
    assert "pct" in shown["description"]


# --- the three questions, as the plans that answer them --------------------- #

def test_cm_Q52_s_two_populations_add_up():
    """The readout is a ratio between the top 10 senders and the bottom 50%.
    Both slices come from one ranked list, so the denominator population and
    the numerator population have to come from the same 100%."""
    rows = rows_of(*[100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 9, 8, 7, 6, 5])
    top10 = slice_(input=rows, field="n", k=10)
    bottom50 = slice_(input=rows, field="n", pct=50, side="bottom")
    top50 = slice_(input=rows, field="n", pct=50)
    assert len(top10) == 10
    assert len(top50) + len(bottom50) == len(rows) == 15
    assert len(bottom50) == 7          # 15 - ceil(15 * 0.5) = 15 - 8
    assert not ({r["uid"] for r in top50} & {r["uid"] for r in bottom50})


def test_bo_Q54_and_cm_Q54_take_a_cohort_a_later_step_reduces():
    """Both are "the top x%, then a scalar over that cohort" — the slice
    hands on rows, so `ratio`/`percent` close the readout unchanged."""
    rows = [{"uid": f"u{i}", "given": i, "received": 100 - i} for i in range(20)]
    cohort = slice_(input=rows, field="given", pct=10)
    assert [r["uid"] for r in cohort] == ["u19", "u18"]
    total = call_operator(adapter(), "compute",
                          {"fn": "sum", "input": rows, "field": "given"})["value"]
    part = call_operator(adapter(), "compute",
                         {"fn": "sum", "input": cohort, "field": "given"})["value"]
    pc = call_operator(adapter(), "compute",
                       {"fn": "percent", "x": part, "y": total})["value"]
    assert math.isclose(pc, 100 * 37 / 190)
