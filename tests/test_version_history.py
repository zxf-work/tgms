"""The belief log as a queryable population (D-058): O15 `version_history`,
and a `filter` that compares two fields.

`GLOB` blocks 7 of the 110 independent questions and is the worst tag on the
board: read from the questions it is **three unrelated capabilities** — a
scan over the version log (bo-Q40, bo-Q45, bo-Q41), a row-wise field-to-field
comparison (cm-Q25, cm-Q38), and a longest temporal path (bo-Q34, cm-Q6).
The first two are built here and are scored separately; the third is not.

**The capability is the one thing this system has that the baselines do
not.** Every operator so far answers *what did we believe* at some `as_of`.
None answers *what did we revise, and when* — so a store designed around
keeping corrections could not count them. `version_history` is
`entity_history` without the uid: the same version rows, the same
censoring, over the whole store.

THE PROPERTY THAT MATTERS, and the one an implementation gets wrong first:
**a pinned result may not leak the future.** A version superseded *after*
`as_of_tt` was still believed at `as_of_tt`, so it must be reported as
current with an open `tt_e`, and a version written after `as_of_tt` must not
appear at all. Ask for `belief: "superseded"` as of a transaction time
before the correction and the honest answer is an empty page — otherwise the
operator reports revisions the caller's belief state has not seen, and
bi-temporal immutability is a slogan rather than a property.

"How many corrections exist" is then a count of **superseded** versions —
beliefs that were revised. A store nobody has corrected answers 0, and each
`correct` that carves one version into three closes exactly one belief and
so counts once.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, NotFoundError, SchemaError
from tgms.core.model import OPEN_END, canonical_json
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


def _apply(adapter, ops, tt):
    adapter.begin()
    try:
        adapter.apply_ops(ops, tt)
    except Exception:
        adapter.rollback()
        raise
    adapter.commit()


def corrected_store() -> Any:
    """One edge asserted at tt=1 and corrected at tt=2 — the smallest store
    in which the belief log says something a snapshot cannot."""
    a = fresh_adapter()
    _apply(a, [{"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
                "props": {"rating": 1}, "vt_s": 10, "vt_e": 20,
                "disc": ""}], 1)
    _apply(a, [{"op": "correct",
                "ref": {"kind": "edge", "src": "a", "dst": "b",
                        "rel_type": "R", "disc": ""},
                "props": {"rating": -1}, "vt_s": 10, "vt_e": 20}], 2)
    return a


def vh(adapter, **kw) -> dict[str, Any]:
    return call_operator(adapter, "version_history",
                         {"kind": "edge", "window": W, "limit": 1000, **kw})


# ---- what the belief log says --------------------------------------------- #

def test_a_correction_leaves_one_superseded_belief_and_one_current():
    a = corrected_store()
    assert vh(a, belief="current")["rows_total"] == 1
    assert vh(a, belief="superseded")["rows_total"] == 1
    assert vh(a, belief="all")["rows_total"] == 2


def test_an_uncorrected_store_has_no_superseded_beliefs():
    """The number this whole operator exists to report, on the store where
    it must be zero."""
    a = fresh_adapter()
    _apply(a, [{"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
                "props": {}, "vt_s": 10, "vt_e": 20, "disc": ""}], 1)
    assert vh(a, belief="superseded")["rows_total"] == 0
    assert vh(a, belief="current")["rows_total"] == 1


def test_the_rows_carry_both_clocks():
    row = vh(corrected_store(), belief="superseded")["rows"][0]
    assert row["vt_s"] == 10 and row["vt_e"] == 20
    assert row["tt_s"] == 1 and row["tt_e"] == 2
    assert row["src"] == "a" and row["dst"] == "b" and row["rel_type"] == "R"
    # props are deliberately absent: this operator reports the shape of the
    # belief log, and a props column would make every row carry a JSON blob
    assert "props" not in row


def test_rows_are_ordered_by_transaction_time():
    """The belief log's own order, which is what distinguishes this from a
    snapshot. `entity_history` orders by valid time; this does not."""
    a = corrected_store()
    rows = vh(a, belief="all")["rows"]
    assert [r["tt_s"] for r in rows] == [1, 2]


# ---- the property that matters -------------------------------------------- #

def test_a_pinned_result_cannot_see_the_correction():
    """As of tt=1 the correction has not happened. Nothing is superseded,
    the one believed version is open-ended, and the row is byte-identical to
    what the same call returned before the correction was written."""
    a = corrected_store()
    assert vh(a, belief="superseded", as_of_tt=1)["rows_total"] == 0
    cur = vh(a, belief="current", as_of_tt=1)
    assert cur["rows_total"] == 1
    assert cur["rows"][0]["tt_e"] == OPEN_END, \
        "a belief that ends after as_of has not ended yet"
    assert vh(a, belief="all", as_of_tt=1)["rows_total"] == 1


def test_a_version_written_after_as_of_does_not_exist_yet():
    a = corrected_store()
    for belief in ("current", "superseded", "all"):
        rows = vh(a, belief=belief, as_of_tt=1)["rows"]
        assert all(r["tt_s"] <= 1 for r in rows), belief


@SETTINGS
@given(as_of=st.integers(1, 3))
def test_immutability_under_a_pinned_as_of(as_of):
    """The same call at the same `as_of` is byte-identical before and after
    a later correction — the whole bi-temporal promise, asserted on the
    operator that reads corrections."""
    a = fresh_adapter()
    _apply(a, [{"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
                "props": {"v": 1}, "vt_s": 10, "vt_e": 20, "disc": ""}], 1)
    before = canonical_json(vh(a, belief="all", as_of_tt=as_of)["rows"])
    _apply(a, [{"op": "correct",
                "ref": {"kind": "edge", "src": "a", "dst": "b",
                        "rel_type": "R", "disc": ""},
                "props": {"v": 2}, "vt_s": 10, "vt_e": 20}], 4)
    assert canonical_json(vh(a, belief="all", as_of_tt=as_of)["rows"]) == before


# ---- filters and shape ----------------------------------------------------- #

def test_the_valid_time_window_selects_by_overlap():
    a = corrected_store()
    assert vh(a, belief="all", window={"t_a": 0, "t_b": 10})["rows_total"] == 0
    assert vh(a, belief="all", window={"t_a": 19, "t_b": 30})["rows_total"] == 2
    assert vh(a, belief="all", window={"t_a": 20, "t_b": 30})["rows_total"] == 0


def test_node_versions_are_a_separate_population():
    a = fresh_adapter()
    _apply(a, [{"op": "assert_node", "uid": "a", "label": "N",
                "props": {}, "vt_s": 10, "vt_e": 20}], 1)
    _apply(a, [{"op": "correct", "ref": {"kind": "node", "uid": "a"},
                "props": {"x": 1}, "vt_s": 10, "vt_e": 20}], 2)
    out = call_operator(a, "version_history",
                        {"kind": "node", "window": W, "limit": 100,
                         "belief": "superseded"})
    assert out["rows_total"] == 1
    assert out["rows"][0]["uid"] == "a" and out["rows"][0]["label"] == "N"


def test_rel_types_filters_edges_only():
    a = corrected_store()
    assert vh(a, belief="all", rel_types=["R"])["rows_total"] == 2
    assert vh(a, belief="all", rel_types=["S"])["rows_total"] == 0
    with pytest.raises(InvalidArgError, match="rel_types"):
        call_operator(a, "version_history",
                      {"kind": "node", "window": W, "limit": 10,
                       "rel_types": ["R"]})


def test_argument_contract():
    a = corrected_store()
    with pytest.raises(SchemaError):
        call_operator(a, "version_history",
                      {"kind": "relationship", "window": W, "limit": 10})
    with pytest.raises(SchemaError):
        call_operator(a, "version_history",
                      {"kind": "edge", "window": W, "limit": 10,
                       "belief": "maybe"})
    with pytest.raises((InvalidArgError, SchemaError)):
        call_operator(a, "version_history",
                      {"kind": "edge", "limit": 10,
                       "window": {"t_a": 10, "t_b": 10}})


def test_the_operator_is_advertised():
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools

    assert "version_history" in REGISTRY
    shown = [t for t in anthropic_tools() if t["name"] == "version_history"]
    assert shown, "version_history is not in the tool surface"
    for word in ("superseded", "belief", "corrections"):
        assert word in shown[0]["description"], f"{word!r} is not advertised"


# ---- oracle equivalence ---------------------------------------------------- #

@SETTINGS
@given(seed=st.integers(0, 7),
       belief=st.sampled_from(["current", "superseded", "all"]),
       as_of=st.one_of(st.integers(1, 8), st.just(2**62)),
       kind=st.sampled_from(["node", "edge"]))
def test_matches_the_oracle(seed, belief, as_of, kind):
    import random

    rng = random.Random(seed)
    # paranoid off: this case is about what `version_history` reports, and
    # the in-batch integrity check is the defect pinned at the bottom of
    # this file — leaving it on would fail here for a reason that has
    # nothing to do with the operator under test
    a = fresh_adapter(paranoid=False)
    for tt in range(1, 8):
        s, d = rng.choice("abc"), rng.choice("abc")
        vt = rng.randrange(0, 40)
        try:
            if rng.random() < 0.4:
                _apply(a, [{"op": "assert_node", "uid": s, "label": "N",
                            "props": {"n": tt}, "vt_s": vt,
                            "vt_e": vt + 10}], tt)
            elif rng.random() < 0.7:
                _apply(a, [{"op": "assert_edge", "src": s, "dst": d,
                            "rel_type": "R", "props": {"n": tt},
                            "vt_s": vt, "vt_e": vt + 10, "disc": ""}], tt)
            else:
                _apply(a, [{"op": "correct",
                            "ref": {"kind": "edge", "src": s, "dst": d,
                                    "rel_type": "R", "disc": ""},
                            "props": {"n": tt}, "vt_s": vt,
                            "vt_e": vt + 10}], tt)
        except (NotFoundError, InvalidArgError):
            pass
    oracle = Oracle(list(a.all_node_versions()), list(a.all_edge_versions()))
    args = {"kind": kind, "window": {"t_a": 0, "t_b": 60}, "limit": 1000,
            "belief": belief, "as_of_tt": as_of}
    engine = call_operator(a, "version_history", args)
    expected = _canonicalize_floats(oracle.version_history(engine["args_echo"]))
    payload = {k: v for k, v in engine.items()
               if k not in ENVELOPE_META_KEYS}
    assert canonical_json(payload) == canonical_json(expected)


# ---- the second capability: filter comparing two fields -------------------- #

ROWS = [{"src": "a", "dst": "a", "count": 3},
        {"src": "a", "dst": "b", "count": 5},
        {"src": "b", "dst": "b", "count": 7}]


def _compute(**args) -> dict[str, Any]:
    return call_operator(fresh_adapter(), "compute", args)


def test_filter_compares_two_fields():
    """cm-Q25 and cm-Q38 both ask how many messages an account sent to
    itself. `filter` could compare a field to a literal and `derive` could
    combine two fields, but nothing could *compare* two — so `src == dst`
    was unwritable."""
    rows = _compute(fn="filter", input=ROWS, field="src", cmp="eq",
                    field2="dst")["rows"]
    assert [r["count"] for r in rows] == [3, 7]
    rows = _compute(fn="filter", input=ROWS, field="src", cmp="ne",
                    field2="dst")["rows"]
    assert [r["count"] for r in rows] == [5]


def test_filter_takes_exactly_one_of_value_and_field2():
    """The same rule `derive` has carried since D-055, and the same words."""
    with pytest.raises(InvalidArgError, match="exactly one"):
        _compute(fn="filter", input=ROWS, field="src", cmp="eq",
                 value="a", field2="dst")
    with pytest.raises(InvalidArgError, match="missing"):
        _compute(fn="filter", input=ROWS, field="src", cmp="eq",
                 field2="nope")


def test_a_two_field_comparison_still_refuses_a_null():
    """D-056's rule does not get a hole cut in it: presence is the only
    comparison a null takes part in, whichever side it is on."""
    rows = [{"a": 1, "b": None}]
    with pytest.raises(InvalidArgError, match="not_null"):
        _compute(fn="filter", input=rows, field="a", cmp="lt", field2="b")


def test_field2_is_advertised():
    from tgms.tools.schemas import anthropic_tools

    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    assert "field2" in shown["input_schema"]["properties"]
    assert "field2" in shown["description"]


# ---- a defect found while writing the above, fixed in D-059 --------------- #
#
# Building random stores for the oracle case above raised `StateError:
# disjointness violated` on the native backend and not on DuckDB, for
# writes that produce byte-identical version rows on both. The root cause
# was one line of behaviour: **mid-batch, the native backend still reported
# a version that the same batch had already closed.**
#
#     duckdb   believed MID-batch : [(10, 15), (15, 25)]
#     native   believed MID-batch : [(10, 15), (10, 20), (15, 25)]
#                       after commit, both: [(10, 15), (15, 25)]
#
# Closes against rows staged in the same batch were already visible; closes
# against rows a *previous* batch committed were not, and those are the
# only kind an ordinary correction makes. So the read-your-own-writes
# overlay had a hole exactly where corrections live.
#
# These two run under both backends explicitly, because CI runs the suite
# once and `TGMS_TEST_BACKEND` defaults to DuckDB: a native-only regression
# here would otherwise be invisible until someone re-ran the whole suite by
# hand. The claim is that the two AGREE, so both are built in the test.
#
# The write-side half of the finding — a second op in one batch carving
# against belief the first op changed — is D-059's semantics rule, and its
# tests live with the other bi-temporal invariants in
# tests/test_storage_invariants.py.


def _both_backends(probe):
    """Run `probe` against a fresh adapter of each backend; return both."""
    import tempfile

    from tgms.storage.duckdb_adapter import DuckDBAdapter
    from tgms.storage.native import NativeAdapter

    duck = DuckDBAdapter(":memory:")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            native = NativeAdapter(tmp)
            try:
                return probe(native), probe(duck)
            finally:
                native.close()
    finally:
        duck.close()


def test_a_closed_version_is_not_believed_midway_through_its_own_batch():
    def believed_midway(adapter) -> list[tuple[int, int]]:
        e = {"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
             "props": {}, "disc": ""}
        adapter.begin()
        adapter.apply_ops([{**e, "vt_s": 10, "vt_e": 20}], 1)
        adapter.commit()
        eid = next(iter(adapter.all_edge_versions())).eid
        adapter.begin()
        adapter.apply_ops([{**e, "vt_s": 15, "vt_e": 25}], 2)
        mid = sorted((v.vt_s, v.vt_e)
                     for v in adapter.believed_edge_versions(eid))
        adapter.commit()
        return mid

    native, duckdb = _both_backends(believed_midway)
    assert native == duckdb == [(10, 15), (15, 25)]


def test_the_whole_row_agrees_mid_batch_not_just_the_valid_interval():
    """The overlay is in `close_index`, which every read shares, so the fix
    is not scoped to `believed_*`: `all_*_versions` reports the pending
    close as the closed row's `tt_e` inside the batch that made it, the same
    way DuckDB's uncommitted UPDATE does. Pinning the whole row also pins
    that nothing else about it moved."""
    def rows_midway(adapter):
        e = {"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
             "props": {}, "disc": ""}
        adapter.begin()
        adapter.apply_ops([{**e, "vt_s": 10, "vt_e": 20}], 1)
        adapter.commit()
        adapter.begin()
        adapter.apply_ops([{**e, "vt_s": 15, "vt_e": 25}], 2)
        mid = sorted((v.vt_s, v.vt_e, v.tt_s, v.tt_e)
                     for v in adapter.all_edge_versions())
        adapter.commit()
        after = sorted((v.vt_s, v.vt_e, v.tt_s, v.tt_e)
                       for v in adapter.all_edge_versions())
        return mid, after

    native, duckdb = _both_backends(rows_midway)
    assert native == duckdb
    assert native == ([(10, 15, 2, OPEN_END),
                       (10, 20, 1, 2),          # closed by the open batch
                       (15, 25, 2, OPEN_END)],
                      [(10, 15, 2, OPEN_END),
                       (10, 20, 1, 2),
                       (15, 25, 2, OPEN_END)])


# ---- the cost model, which the measurement said was wrong ------------------ #

def test_the_version_scan_is_priced_as_materialization_not_a_scan():
    """Measured at 10M versions: 153.9 s and 13.3 GB, against 1.3 s and
    2.3 GB for the columnar count over the same population — 116x the time
    and 54x the bytes per row, because `all_*_versions` builds one Python
    object per version and no columnar version scan exists.

    `scan_estimate` would price that as an ordinary scan and let it through,
    so this operator carries its own: no window pruning, because there is no
    pushdown and the window filters the output rather than the work, and the
    per-row cost charged as an EXPANSION, because per-row allocation is what
    the expansion ceiling is for.
    """
    from tgms.temporal.ops_versions import _version_cost

    stats = {"n_edge_versions": 10_000_000, "n_node_versions": 20_000,
             "vt_min": 0, "vt_max": 1_000_000}
    est = _version_cost({"window": {"t_a": 0, "t_b": 10}}, stats)
    assert est["expansions_est"] == 10_020_000
    # a narrow window must NOT make it look cheap — that is the trap
    wide = _version_cost({"window": {"t_a": 0, "t_b": 1_000_000}}, stats)
    assert est == wide


def test_a_store_too_large_to_materialize_is_refused():
    from tgms.core.errors import CostError
    from tgms.temporal.guardrails import add_time_estimate, enforce_cost
    from tgms.temporal.ops_versions import _version_cost

    # refusal moved to the attached time estimate (D-087): version_history
    # carries its own measured coefficient — 15,400 ms per million rows, the
    # D-058 116x off-class rate — so 10M rows prices at ~154 s against the
    # 10 s default and must refuse, while CollegeMsg's 61k rows price at
    # under a second and must answer
    big = add_time_estimate("version_history",
                            _version_cost({}, {"n_edge_versions": 10_000_000,
                                               "n_node_versions": 0}))
    with pytest.raises(CostError):
        enforce_cost("version_history", big)
    small = add_time_estimate("version_history",
                              _version_cost({}, {"n_edge_versions": 59_835,
                                                 "n_node_versions": 1_908}))
    enforce_cost("version_history", small)      # CollegeMsg still answers
