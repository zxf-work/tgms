"""Why the remaining 19 questions are blocked, as assertions (D-066).

Two sessions running, a blocked entry turned out not to be blocked: D-063
found cm-Q39 published expressible when it cannot run, and D-065 retired
`PROJ` outright because D-052, D-055 and D-058 had built it between them.
Both were found by asking the operators rather than reading the need strings.

So this session asked all nineteen, and none of them moved. That is a
negative result worth keeping, because a negative result decays: the need
strings say things like "the sign filter is not available inside the motif
operator", which is true today and is prose, and prose does not fail when
someone adds the argument.

Each test below pins the *reason* a tag survives. When one starts failing,
the capability it names has arrived and its questions want re-reading — which
is exactly the signal that was missing for `PROJ` for three sessions.
"""

from __future__ import annotations

import pytest

from tgms.tools.schemas import anthropic_tools


@pytest.fixture(scope="module")
def tools():
    return {t["name"]: t for t in anthropic_tools()}


def _props(tools, name):
    return tools[name]["input_schema"]["properties"]


def test_PROP_no_property_predicate_inside_motif_or_path_operators(tools):
    """`PROP` blocks 5. All five want a sign predicate *inside* another
    operator — 'each rated the next positively' along a path or a cycle.
    `prop_filter` reached `aggregate_events` in D-052 and no further, so a
    motif cannot say which edges count."""
    for name in ("count_temporal_motifs", "find_temporal_motif_instances",
                 "temporal_reachability", "temporal_paths", "co_active"):
        args = _props(tools, name)
        assert not [a for a in args if "prop" in a], (
            f"{name} gained a property argument — re-read the five PROP "
            f"questions (bo-Q31, bo-Q32, bo-Q34, bo-Q35, bo-Q37)")


def test_SEQ_the_motif_catalogue_has_no_two_edge_shape(tools):
    """cm-Q13 wants 'A sent to B and B sent to A within the same hour' — two
    edges with a delta between them. Every catalogued motif is three edges,
    which is also what blocks cm-Q19."""
    motifs = _props(tools, "count_temporal_motifs")["motif"]["enum"]
    assert set(motifs) == {"M_2node_pingpong", "M_path_3", "M_star_out_3",
                           "M_triangle_acyclic_1", "M_triangle_cyclic"}, (
        "the motif catalogue changed — if a 2-edge shape arrived, cm-Q13 and "
        "cm-Q19 want re-reading")


def test_SEQ_a_sliding_window_cannot_count_distinct(tools):
    """cm-Q31 wants the largest number of DISTINCT accounts messaged inside
    one 1-hour window. `max_in_window` counts events and `count_distinct`
    counts distinct values, and an aggregate picks exactly one `agg`."""
    item = _props(tools, "aggregate_events")["aggregates"]["items"]
    aggs = item["properties"]["agg"]["enum"]
    assert "max_in_window" in aggs and "count_distinct" in aggs
    assert "distinct_in_window" not in aggs and "count_distinct_in_window" \
        not in aggs, "a distinct-within-window aggregate arrived — see cm-Q31"


def test_GSLICE_arrived_and_this_tripwire_has_been_spent(tools):
    """Written in D-066 as "compute has no grouping", re-aimed in D-067 when
    it fired, and spent in D-068 when the slice half landed and bo-Q47 and
    bo-Q52 moved. It is kept as a record of what it caught rather than
    deleted: a tripwire that fires twice and is right twice is the argument
    for writing more of them."""
    args = _props(tools, "compute")
    assert "pct" in args and "side" in args and "group_by" in args, (
        "D-060's fraction or D-067/D-068's grouping is gone")


def test_EGO_grouping_is_still_by_one_endpoint_role(tools):
    """`EGO` blocks 2 (cm-Q14, cm-Q24): an account's events in EITHER role,
    in one group. `endpoint_filter` unions the roles in the population;
    grouping is still by `src` or by `dst`."""
    dim = _props(tools, "aggregate_events")["group_by"]["items"]["properties"]
    assert dim["role"]["enum"] == ["src", "dst", None], (
        "an either-role grouping arrived — re-read cm-Q14 and cm-Q24")


def test_DIM3_the_grouping_budget_is_still_two(tools):
    """cm-Q39 needs pair AND day. D-063 moved it to class 3 for this reason;
    if the budget ever widens it goes back."""
    gb = _props(tools, "aggregate_events")["group_by"]
    assert gb["maxItems"] == 2, (
        "group_by takes more than two dimensions now — cm-Q39 is expressible "
        "again and DIM3 retires")
