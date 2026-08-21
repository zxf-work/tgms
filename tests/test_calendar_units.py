"""Calendar units (D-057): hour of day, day of week, month of year.

`CAL` blocks 8 of the 110 independent questions and is the sole blocker of
5 — the joint largest item left. Read from the questions, all five want the
same thing and it is a **dimension**, not an aggregate: "what percentage of
messages were sent between 12:00 and 18:00", "which day of the week had the
lowest volume", "any message between 02:00 and 05:00 on a Saturday",
"accounts active in every month from January to June". Every one groups by
a calendar unit and then counts.

A `time_bucket` stride cannot express any of them. Strides are fixed widths
anchored at `t_a`, so a 24-hour stride gives *a* day boundary but not
midnight, and no stride at all gives "Saturday" or "January", which are
cyclic rather than contiguous.

**Cyclic is the property that makes this a different dimension.** A
`time_bucket` group is one interval; an `hour_of_day` group is every
13:00-14:00 in the window, scattered through it. Nothing downstream may
assume a calendar group is contiguous in time, and the ordering cannot come
from the values themselves — "Friday" sorts before "Monday" by code point,
and that is not an ordering of anything. Both are pinned below.

**The timezone is a fixed offset in minutes, never a timezone name.** Valid
times are epoch microseconds with no zone attached, and a `zoneinfo` lookup
would make a `result_digest` depend on the host's tz database version,
which is the one thing this system cannot allow. The offset is an argument,
so it is in `args_echo` and therefore in the digest. D-057 records what that
costs: a window spanning a DST change gets one offset for all of it, and
the caller is choosing which.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
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
from tgms.temporal.oracle import DAY_NAMES, MONTH_NAMES, Oracle

from .conftest import ENVELOPE_META_KEYS, fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

HOUR = 3_600_000_000
DAY = 24 * HOUR
#: 2004-04-15 00:00:00 UTC — a Thursday, and the first day CollegeMsg has.
APRIL_15_2004 = 1_081_987_200_000_000


def at(*stamps: int) -> Any:
    """A store with one instantaneous event per epoch-microsecond stamp."""
    adapter = fresh_adapter()
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
          "props": {}, "vt_s": t, "vt_e": t + 1, "disc": str(i)}
         for i, t in enumerate(stamps)], 1)
    adapter.commit()
    return adapter


def group(adapter, unit: str, window: tuple[int, int], **kw) -> list[dict]:
    t_a, t_b = window
    return call_operator(adapter, "aggregate_events", {
        "window": {"t_a": t_a, "t_b": t_b}, "limit": 10_000,
        "group_by": [{"dim": "calendar_unit", "unit": unit, **kw}],
        "aggregates": [{"agg": "count"}]})["rows"]


WIDE = (0, APRIL_15_2004 + 400 * DAY)


# ---- what each unit is ---------------------------------------------------- #

def test_the_epoch_is_a_thursday_at_midnight():
    """The anchor every other case is measured from. 1970-01-01 was a
    Thursday, and getting that wrong shifts every weekday by a constant."""
    assert group(at(0), "day_of_week", WIDE) == \
        [{"day_of_week": "Thursday", "count": 1}]
    assert group(at(0), "hour_of_day", WIDE) == \
        [{"hour_of_day": 0, "count": 1}]
    assert group(at(0), "month_of_year", WIDE) == \
        [{"month_of_year": "January", "count": 1}]


def test_a_real_timestamp_agrees_with_the_calendar():
    """2004-04-15 00:00 UTC was a Thursday; CollegeMsg starts there."""
    rows = call_operator(at(APRIL_15_2004), "aggregate_events", {
        "window": {"t_a": 0, "t_b": WIDE[1]}, "limit": 10,
        "group_by": [{"dim": "calendar_unit", "unit": "day_of_week"},
                     {"dim": "calendar_unit", "unit": "month_of_year"}],
        "aggregates": [{"agg": "count"}]})["rows"]
    assert rows == [{"day_of_week": "Thursday", "month_of_year": "April",
                     "count": 1}]
    # and the same instant read by the standard library
    d = datetime.fromtimestamp(APRIL_15_2004 / 1e6, tz=timezone.utc)
    assert (DAY_NAMES[d.weekday()], MONTH_NAMES[d.month - 1]) == \
        ("Thursday", "April")


def test_hours_run_zero_to_twenty_three_and_wrap():
    rows = group(at(0, 13 * HOUR, 23 * HOUR, DAY + 13 * HOUR),
                 "hour_of_day", WIDE)
    assert rows == [{"hour_of_day": 0, "count": 1},
                    {"hour_of_day": 13, "count": 2},
                    {"hour_of_day": 23, "count": 1}]


# ---- the two properties that make this its own dimension ------------------ #

def test_a_calendar_group_is_not_contiguous_in_time():
    """The distinguishing property. Three events a week apart, all at
    13:00, are ONE `hour_of_day` group and three `time_bucket` groups. A
    reader that assumes a group is an interval is wrong here, and this is
    the case that says so."""
    adapter = at(13 * HOUR, 8 * DAY + 13 * HOUR, 15 * DAY + 13 * HOUR)
    assert group(adapter, "hour_of_day", WIDE) == \
        [{"hour_of_day": 13, "count": 3}]
    buckets = call_operator(adapter, "aggregate_events", {
        "window": {"t_a": 0, "t_b": 30 * DAY}, "limit": 100, "stride": DAY,
        "group_by": [{"dim": "time_bucket"}],
        "aggregates": [{"agg": "count"}]})["rows"]
    assert len(buckets) == 3 and all(r["count"] == 1 for r in buckets)


def test_named_units_sort_in_calendar_order_not_code_point_order():
    """Friday sorts before Monday as text, and April before January. The
    canonical order of these rows is the calendar's."""
    days = group(at(*(i * DAY for i in range(7))), "day_of_week", WIDE)
    assert [r["day_of_week"] for r in days] == list(DAY_NAMES)
    months = group(at(*(APRIL_15_2004 + i * 31 * DAY for i in range(12))),
                   "month_of_year", WIDE)
    seen = [r["month_of_year"] for r in months]
    assert seen == [m for m in MONTH_NAMES if m in seen]
    assert seen != sorted(seen), "the test data must distinguish the orders"


# ---- the offset, and what it is not --------------------------------------- #

def test_the_offset_shifts_the_calendar_and_lives_in_the_digest():
    """-300 minutes is US Eastern in summer: midnight UTC is the previous
    day at 19:00. The offset is an argument, so two calls that disagree
    about it are two different questions with two different digests."""
    adapter = at(0)
    assert group(adapter, "hour_of_day", WIDE, tz_offset_minutes=-300) == \
        [{"hour_of_day": 19, "count": 1}]
    assert group(adapter, "day_of_week", WIDE, tz_offset_minutes=-300) == \
        [{"day_of_week": "Wednesday", "count": 1}]
    # positive offsets go the other way, and a whole day of them wraps
    assert group(adapter, "day_of_week", WIDE, tz_offset_minutes=1440) == \
        [{"day_of_week": "Friday", "count": 1}]


def test_two_offsets_give_two_digests():
    adapter = at(0)

    def digest(off):
        return call_operator(adapter, "aggregate_events", {
            "window": {"t_a": WIDE[0], "t_b": WIDE[1]}, "limit": 10,
            "group_by": [{"dim": "calendar_unit", "unit": "hour_of_day",
                          "tz_offset_minutes": off}],
            "aggregates": [{"agg": "count"}]})["result_digest"]

    assert digest(0) != digest(-300)
    assert digest(0) == digest(0)


# ---- two calendar dimensions in one call ---------------------------------- #

def test_two_calendar_units_are_not_a_duplicate_dimension():
    """cm-Q32 asks for an hour range on a named weekday, which is both
    units at once. They differ in `unit`, so they are different
    dimensions."""
    saturday_3am = APRIL_15_2004 + 2 * DAY + 3 * HOUR   # 2004-04-17, a Sat
    rows = call_operator(at(saturday_3am), "aggregate_events", {
        "window": {"t_a": 0, "t_b": WIDE[1]}, "limit": 10,
        "group_by": [{"dim": "calendar_unit", "unit": "hour_of_day"},
                     {"dim": "calendar_unit", "unit": "day_of_week"}],
        "aggregates": [{"agg": "count"}]})["rows"]
    assert rows == [{"hour_of_day": 3, "day_of_week": "Saturday",
                     "count": 1}]


def test_the_same_unit_twice_is_a_duplicate():
    with pytest.raises(InvalidArgError, match="duplicate dimension"):
        call_operator(at(0), "aggregate_events", {
            "window": {"t_a": 0, "t_b": DAY}, "limit": 10,
            "group_by": [{"dim": "calendar_unit", "unit": "hour_of_day"},
                         {"dim": "calendar_unit", "unit": "hour_of_day"}],
            "aggregates": [{"agg": "count"}]})


# ---- oracle equivalence over random stamps -------------------------------- #

units = st.sampled_from(["hour_of_day", "day_of_week", "month_of_year"])
offsets = st.sampled_from([0, -300, 330, 60, -720, 1440])
stamps = st.lists(st.integers(0, 3 * 365 * 24 * 3600 * 1_000_000),
                  min_size=0, max_size=25)


@SETTINGS
@given(unit=units, off=offsets, ts=stamps)
def test_matches_the_oracle(unit, off, ts):
    """The engine walks integer civil arithmetic, the oracle walks
    `datetime`. Neither is derived from the other, which is what makes
    agreement evidence rather than a tautology."""
    adapter = at(*ts)
    oracle = Oracle(list(adapter.all_node_versions()),
                    list(adapter.all_edge_versions()))
    args = {"window": {"t_a": 0, "t_b": 4 * 365 * 24 * 3600 * 1_000_000},
            "limit": 10_000,
            "group_by": [{"dim": "calendar_unit", "unit": unit,
                          "tz_offset_minutes": off}],
            "aggregates": [{"agg": "count"},
                           {"agg": "count_distinct", "of": "dst"}]}
    engine = call_operator(adapter, "aggregate_events", args)
    expected = _canonicalize_floats(oracle.aggregate_events(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ENVELOPE_META_KEYS}
    assert canonical_json(payload) == canonical_json(expected)


@SETTINGS
@given(unit=units, ts=stamps)
def test_group_counts_still_sum_to_the_total(unit, ts):
    """Partitioning by a calendar unit cannot create or lose an event, even
    though the groups are not intervals."""
    adapter = at(*ts)
    w = {"t_a": 0, "t_b": 4 * 365 * 24 * 3600 * 1_000_000}
    total = call_operator(adapter, "aggregate_events", {
        "window": w, "limit": 10, "group_by": [],
        "aggregates": [{"agg": "count"}]})["rows"][0]["count"]
    rows = call_operator(adapter, "aggregate_events", {
        "window": w, "limit": 10_000,
        "group_by": [{"dim": "calendar_unit", "unit": unit}],
        "aggregates": [{"agg": "count"}]})["rows"]
    assert sum(r["count"] for r in rows) == total


# ---- argument contract ----------------------------------------------------- #

def test_argument_contract():
    adapter = at(0)

    def bad(match, group_by):
        with pytest.raises((InvalidArgError, SchemaError), match=match):
            call_operator(adapter, "aggregate_events",
                          {"window": {"t_a": 0, "t_b": DAY}, "limit": 10,
                           "group_by": group_by,
                           "aggregates": [{"agg": "count"}]})

    bad("requires 'unit'", [{"dim": "calendar_unit"}])
    bad("takes no role", [{"dim": "calendar_unit", "unit": "hour_of_day",
                           "role": "src"}])
    bad("'unit' is only meaningful", [{"dim": "time_bucket",
                                       "unit": "hour_of_day"}])
    bad("'tz_offset_minutes' is only meaningful",
        [{"dim": "rel_type", "tz_offset_minutes": 60}])
    with pytest.raises(SchemaError):
        call_operator(adapter, "aggregate_events",
                      {"window": {"t_a": 0, "t_b": DAY}, "limit": 10,
                       "group_by": [{"dim": "calendar_unit",
                                     "unit": "fortnight"}],
                       "aggregates": [{"agg": "count"}]})
    # a calendar dimension is not a time_bucket, so it does not license a
    # stride — the pre-existing check has to keep refusing one
    with pytest.raises(InvalidArgError, match="requires a time_bucket"):
        call_operator(adapter, "aggregate_events",
                      {"window": {"t_a": 0, "t_b": DAY}, "limit": 10,
                       "stride": HOUR,
                       "group_by": [{"dim": "calendar_unit",
                                     "unit": "hour_of_day"}],
                       "aggregates": [{"agg": "count"}]})


def test_the_calendar_dimension_is_advertised():
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools

    spec = REGISTRY["aggregate_events"]
    dim = spec.args_schema["properties"]["group_by"]["items"]["properties"]
    shown = [t for t in anthropic_tools() if t["name"] == "aggregate_events"][0]
    assert "calendar_unit" in dim["dim"]["enum"]
    for unit in ("hour_of_day", "day_of_week", "month_of_year"):
        assert unit in dim["unit"]["enum"]
        assert unit in shown["description"], f"{unit} is not advertised"
    assert "tz_offset_minutes" in dim
    assert "tz_offset_minutes" in shown["description"]
