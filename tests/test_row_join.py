"""`ROW` and `JOIN` (D-055): a derived column, and aligning two results.

`JOIN` blocks 13 questions and is the sole blocker of none — every one also
needs `ROW` — so the two ship together. Read from the questions rather than
from the tag:

  * `derive` adds one computed column to a row set, from two fields or from
    a field and a literal. That is the whole of `ROW`: `max_vt_s - min_vt_s`
    per group, a floor-divide to a calendar day, a ratio of two directions,
    and a concatenated `(day, account)` key. It adds a column and nothing
    else, so `filter`, `topk` and `mean` compose with it for free.
  * `join` aligns two prior steps on a key. **Keys must be unique on both
    sides** — that is the bound, not a cost model: a grouped result has one
    row per group and so always qualifies, and requiring it caps the output
    at min(|left|, |right|) instead of their product.

The unmatched-key choice is the semantics and both behaviours are needed:
bo-Q14's "positives minus negatives" wants an account with no negatives to
score zero, bo-Q46 wants only accounts present in both. Hence `inner` and
`left`, defaulting to `inner`, because `inner` invents nothing.

Arithmetic is D-051's, unchanged: exact where the inputs are integral, one
blessed rounding otherwise.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.temporal.algebra import call_operator, ensure_all_registered

from .conftest import fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

_a: list[Any] = []


def adapter():
    if not _a:
        _a.append(fresh_adapter())
    return _a[0]


def c(**args) -> dict[str, Any]:
    return call_operator(adapter(), "compute", args)


DAY = 86_400_000_000
SPANS = [{"src": "u0", "min_vt_s": 0, "max_vt_s": 3 * DAY},
         {"src": "u1", "min_vt_s": DAY, "max_vt_s": DAY + 1},
         {"src": "u2", "min_vt_s": 5 * DAY, "max_vt_s": 5 * DAY}]


# --- derive ----------------------------------------------------------------- #

def test_derive_from_two_fields():
    r = c(fn="derive", input=SPANS, field="max_vt_s", field2="min_vt_s",
          op="sub", into="span")
    assert [x["span"] for x in r["rows"]] == [3 * DAY, 1, 0]
    assert r["rows"][0]["src"] == "u0"          # the original row survives


def test_derive_from_a_field_and_a_literal():
    """The floor-divide to a calendar day — four questions want exactly it."""
    spans = c(fn="derive", input=SPANS, field="max_vt_s", field2="min_vt_s",
              op="sub", into="span")["rows"]
    r = c(fn="derive", input=spans, field="span", value=DAY,
          op="floordiv", into="days")
    assert [x["days"] for x in r["rows"]] == [3, 0, 0]


def test_derive_composes_into_the_existing_reducers():
    """bo-Q23's shape end to end: a per-row span, then the whole-input mean
    that already existed. Not a grouped mean — that is SEQ's problem."""
    spans = c(fn="derive", input=SPANS, field="max_vt_s", field2="min_vt_s",
              op="sub", into="span")["rows"]
    days = c(fn="derive", input=spans, field="span", value=DAY,
             op="floordiv", into="days")["rows"]
    assert c(fn="mean", input=days, field="days")["value"] == 1


def test_derive_arithmetic_is_d051s():
    rows = [{"a": 7, "b": 2}, {"a": 6, "b": 3}]
    got = [x["q"] for x in c(fn="derive", input=rows, field="a", field2="b",
                             op="div", into="q")["rows"]]
    assert got == [3.5, 2]                       # inexact -> float, exact -> int
    assert isinstance(got[1], int)


def test_derive_concat_builds_a_key():
    """bo-Q51: a (day, account) key so two per-day sets can be matched."""
    rows = [{"day": 3, "uid": "u0"}, {"day": 4, "uid": "u1"}]
    r = c(fn="derive", input=rows, field="day", field2="uid",
          op="concat", into="k")
    assert [x["k"] for x in r["rows"]] == ["3|u0", "4|u1"]


def test_derive_refuses_to_overwrite_a_column():
    with pytest.raises(InvalidArgError):
        c(fn="derive", input=SPANS, field="max_vt_s", value=1, op="add",
          into="src")


def test_derive_rejects_division_by_zero_and_non_numbers():
    with pytest.raises(InvalidArgError):
        c(fn="derive", input=[{"a": 1, "b": 0}], field="a", field2="b",
          op="div", into="q")
    with pytest.raises(InvalidArgError):
        c(fn="derive", input=[{"a": "x", "b": 1}], field="a", field2="b",
          op="sub", into="q")


# --- join ------------------------------------------------------------------- #

GIVEN = [{"src": "u0", "count": 5}, {"src": "u1", "count": 2}]
GOT = [{"dst": "u0", "count": 3}, {"dst": "u9", "count": 7}]


def test_inner_join_keeps_only_matched_keys():
    r = c(fn="join", input=GIVEN, other=GOT, on="src", other_on="dst")
    assert r["rows"] == [{"src": "u0", "count": 5, "r_dst": "u0",
                          "r_count": 3}]


def test_left_join_fills_the_unmatched_side():
    """bo-Q14's shape: an account with no negatives scores zero, not absent."""
    r = c(fn="join", input=GIVEN, other=GOT, on="src", other_on="dst",
          how="left", fill=0)
    assert [x["r_count"] for x in r["rows"]] == [3, 0]
    assert r["rows"][1]["src"] == "u1"


def test_join_then_derive_is_the_whole_point():
    """cm-Q29: sent and received aligned per account, then a ratio."""
    j = c(fn="join", input=GIVEN, other=GOT, on="src", other_on="dst",
          how="left", fill=1)["rows"]
    r = c(fn="derive", input=j, field="count", field2="r_count",
          op="div", into="ratio")
    # the envelope canonicalizes floats to 9 decimals, so compare there
    assert [x["ratio"] for x in r["rows"]] == [round(5 / 3, 9), 2]


def test_duplicate_keys_are_refused_on_either_side():
    """This is the bound. A grouped result has one row per group and always
    qualifies; without it the output would be the product of two pages."""
    dup = [{"src": "u0", "count": 1}, {"src": "u0", "count": 2}]
    with pytest.raises(InvalidArgError):
        c(fn="join", input=dup, other=GOT, on="src", other_on="dst")
    with pytest.raises(InvalidArgError):
        c(fn="join", input=GIVEN, other=dup, on="src", other_on="src")


def test_join_output_is_ordered_by_key():
    r = c(fn="join", input=list(reversed(GIVEN)), other=GOT, on="src",
          other_on="dst", how="left", fill=0)
    assert [x["src"] for x in r["rows"]] == ["u0", "u1"]


def test_a_prefix_collision_is_refused_not_silently_overwritten():
    left = [{"src": "u0", "r_count": 1}]
    with pytest.raises(InvalidArgError):
        c(fn="join", input=left, other=GOT, on="src", other_on="dst")


@SETTINGS
@given(keys=st.lists(st.text(min_size=1, max_size=2), unique=True, max_size=8),
       other=st.lists(st.text(min_size=1, max_size=2), unique=True, max_size=8))
def test_inner_is_the_intersection_and_left_preserves_the_left(keys, other):
    a = [{"k": x, "v": i} for i, x in enumerate(keys)]
    b = [{"k": x, "w": i} for i, x in enumerate(other)]
    inner = c(fn="join", input=a, other=b, on="k")["rows_total"]
    left = c(fn="join", input=a, other=b, on="k", how="left", fill=None)
    assert inner == len(set(keys) & set(other))
    assert left["rows_total"] == len(keys)


def test_argument_contract():
    with pytest.raises((InvalidArgError, SchemaError)):
        c(fn="join", input=GIVEN, on="src")            # no other
    with pytest.raises((InvalidArgError, SchemaError)):
        c(fn="join", input=GIVEN, other=GOT)           # no key
    with pytest.raises(InvalidArgError):
        c(fn="join", input=GIVEN, other=GOT, on="nope", other_on="dst")
    with pytest.raises((InvalidArgError, SchemaError)):
        c(fn="derive", input=SPANS, field="max_vt_s", op="sub", into="x")
    with pytest.raises((InvalidArgError, SchemaError)):
        c(fn="derive", input=SPANS, field="max_vt_s", field2="min_vt_s",
          value=1, op="sub", into="x")                 # both operands


def test_the_new_surface_is_advertised():
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools
    enum = REGISTRY["compute"].args_schema["properties"]["fn"]["enum"]
    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    for fn in ("derive", "join"):
        assert fn in enum
        assert fn in shown["description"], f"{fn} missing from the manual"
