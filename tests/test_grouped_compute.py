"""`compute` reduces per group (D-067): the capability `G`, `GMEAN` and part
of `SEQ` were naming between them.

`aggregate_events` groups the *store*. Nothing grouped a *result* — so
"count distinct partners per account" (cm-Q17), "initiations per initiator"
(cm-Q51) and "average reply time per receiver" (cm-Q44) all died one step
from the answer, each filed under a different tag.

The reference below is written from the contract with `itertools.groupby`
over a sorted copy — an independent path to the same grouping, the way
`test_compute_arithmetic.py` re-derives the quotients with `Fraction`.

THE PROPERTIES THAT MATTER, none of which mention the implementation:

  * a grouping is a **partition** — the group counts sum to the ungrouped
    count, and no row is in two groups. This is the same property D-060
    needed for `pct`, and for the same reason: a ratio between groups is
    only meaningful over a population that adds up.
  * grouping by a **constant** column gives exactly the ungrouped answer, in
    one group. That pins the new path against the old one at every reducer.
  * the answer does not depend on the order the rows arrived in.

Pagination is **by group**, because the output rows are groups. A truncated
grouped result is therefore a page of groups, and the executor's D-061 guard
already refuses to reduce one to a scalar — a grouped `count` is a `count`.
"""

from __future__ import annotations

import os
from itertools import groupby
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.temporal.algebra import (
    _canonicalize_floats,
    call_operator,
    ensure_all_registered,
)

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


def grouped(**args) -> list[dict]:
    return call_operator(adapter(), "compute", {"limit": 10_000, **args})["rows"]


def plain(**args) -> Any:
    return call_operator(adapter(), "compute", args)["value"]


# --- the reference ---------------------------------------------------------- #

def ref_group(rows: list[dict], key: str, fn: str, field: str | None):
    """Group and reduce, by sorting and walking — not by the kernel's dict."""
    import statistics

    def reduce(vals):
        if fn == "count":
            return len(vals)
        nums = [v for v in vals]
        return {"sum": sum, "min": min, "max": max,
                "mean": statistics.fmean,
                "median": statistics.median}[fn](nums)

    out = []
    for k, grp in groupby(sorted(rows, key=lambda r: (str(type(r[key])), r[key])),
                          key=lambda r: r[key]):
        members = list(grp)
        vals = [m[field] for m in members] if field else members
        out.append({key: k, fn: reduce(vals)})
    # the operator's output passes through `_canonicalize_floats` so a digest
    # is stable across platforms; anything compared against it must too, as
    # `test_compute_arithmetic.py` does
    return _canonicalize_floats(out)


def rows_of(*pairs) -> list[dict]:
    return [{"who": w, "n": n} for w, n in pairs]


# --- oracle cases ----------------------------------------------------------- #

@pytest.mark.parametrize("fn,field,expected", [
    ("count", None, [{"who": "a", "count": 3}, {"who": "b", "count": 1}]),
    ("sum", "n", [{"who": "a", "sum": 9}, {"who": "b", "sum": 5}]),
    ("min", "n", [{"who": "a", "min": 1}, {"who": "b", "min": 5}]),
    ("max", "n", [{"who": "a", "max": 5}, {"who": "b", "max": 5}]),
    ("mean", "n", [{"who": "a", "mean": 3}, {"who": "b", "mean": 5}]),
    ("median", "n", [{"who": "a", "median": 3}, {"who": "b", "median": 5}]),
])
def test_grouped_reducers(fn, field, expected):
    rows = rows_of(("a", 1), ("b", 5), ("a", 3), ("a", 5))
    got = grouped(fn=fn, input=rows, group_by="who", field=field)
    assert got == expected
    assert got == ref_group(rows, "who", fn, field)


def test_one_row_per_group_ordered_by_key():
    rows = rows_of(("c", 1), ("a", 1), ("b", 1), ("a", 1))
    got = grouped(fn="count", input=rows, group_by="who")
    assert [r["who"] for r in got] == ["a", "b", "c"], "groups are ordered"
    assert len(got) == 3


def test_the_integer_rule_survives_grouping():
    """D-044's blessed arithmetic does not get a second spelling here: an
    exact mean of ints is an int, per group, exactly as ungrouped."""
    rows = rows_of(("a", 1), ("a", 3), ("b", 1), ("b", 2))
    got = grouped(fn="mean", input=rows, group_by="who", field="n")
    assert got == [{"who": "a", "mean": 2}, {"who": "b", "mean": 1.5}]
    assert type(got[0]["mean"]) is int and type(got[1]["mean"]) is float


# --- the properties --------------------------------------------------------- #

row_lists = st.lists(st.tuples(st.sampled_from("abcd"), st.integers(-20, 20)),
                     min_size=1, max_size=40)


@SETTINGS
@given(pairs=row_lists)
def test_a_grouping_is_a_partition(pairs):
    """Group counts sum to the ungrouped count. A ratio between two groups is
    only meaningful over a population that adds up (D-060 needed this too)."""
    rows = rows_of(*pairs)
    per_group = grouped(fn="count", input=rows, group_by="who")
    assert sum(r["count"] for r in per_group) == len(rows)
    assert len({r["who"] for r in per_group}) == len(per_group)


@SETTINGS
@given(pairs=row_lists, fn=st.sampled_from(["sum", "min", "max", "mean"]))
def test_grouping_by_a_constant_is_the_ungrouped_answer(pairs, fn):
    """The new path pinned against the old one at every reducer."""
    rows = [{"who": "same", "n": n} for _, n in pairs]
    per_group = grouped(fn=fn, input=rows, group_by="who", field="n")
    assert len(per_group) == 1
    assert per_group[0][fn] == plain(fn=fn, input=rows, field="n")


@SETTINGS
@given(pairs=row_lists, seed=st.integers(0, 10**6))
def test_the_answer_does_not_depend_on_input_order(pairs, seed):
    import random
    rows = rows_of(*pairs)
    shuffled = rows[:]
    random.Random(seed).shuffle(shuffled)
    assert grouped(fn="sum", input=rows, group_by="who", field="n") == \
        grouped(fn="sum", input=shuffled, group_by="who", field="n")


@SETTINGS
@given(pairs=row_lists, fn=st.sampled_from(["count", "sum", "mean", "median"]))
def test_matches_the_reference(pairs, fn):
    rows = rows_of(*pairs)
    field = None if fn == "count" else "n"
    assert grouped(fn=fn, input=rows, group_by="who", field=field) == \
        ref_group(rows, "who", fn, field)


# --- what it refuses -------------------------------------------------------- #

def test_a_missing_group_column_is_refused():
    with pytest.raises(InvalidArgError, match="nope"):
        grouped(fn="count", input=rows_of(("a", 1)), group_by="nope")


def test_group_by_is_refused_on_a_function_that_is_not_a_reduction():
    """`filter`, `derive` and the set operations return rows, not one number
    per group; grouping them is a different operation and is not this one."""
    for fn, extra in (("filter", {"cmp": "gt", "value": 0, "field": "n"}),
                      ("derive", {"field": "n", "value": 1, "op": "add",
                                  "into": "m"})):
        with pytest.raises((InvalidArgError, SchemaError), match="group_by"):
            grouped(fn=fn, input=rows_of(("a", 1)), group_by="who", **extra)


def test_an_unhashable_group_key_is_a_plan_bug_not_an_empty_group():
    with pytest.raises(InvalidArgError):
        grouped(fn="count", input=[{"who": {"a": 1}, "n": 1}], group_by="who")


def test_group_by_is_advertised():
    from tgms.tools.schemas import anthropic_tools

    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    assert "group_by" in shown["input_schema"]["properties"]
    assert "group_by" in shown["description"]


# --- the three questions this is for ---------------------------------------- #

def test_cm_Q17_counts_distinct_partners_per_account():
    """Reciprocal pairs are one call; each row is already a distinct pair, so
    partners per account is a grouped count over that result."""
    pairs = [{"src": "a", "dst": "b"}, {"src": "a", "dst": "c"},
             {"src": "b", "dst": "c"}, {"src": "a", "dst": "d"}]
    per_account = grouped(fn="count", input=pairs, group_by="src")
    assert per_account == [{"src": "a", "count": 3}, {"src": "b", "count": 1}]
    top = call_operator(adapter(), "compute",
                        {"fn": "topk", "input": per_account, "field": "count",
                         "k": 1})["rows"]
    assert top[0]["src"] == "a"


def test_cm_Q44_averages_the_latency_per_receiver():
    """The latency rows run today (D-065). What was missing is the mean PER
    RECEIVER — which is exactly what `GMEAN` named."""
    rows = [{"who": "r1", "latency": 10}, {"who": "r1", "latency": 20},
            {"who": "r2", "latency": 100}]
    per_receiver = grouped(fn="mean", input=rows, group_by="who",
                           field="latency")
    assert per_receiver == [{"who": "r1", "mean": 15},
                            {"who": "r2", "mean": 100}]
