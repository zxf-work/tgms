"""WP1.2 invariant property tests over random bi-temporal update sequences.

Invariants:
- I1  At any transaction time, believed versions of one logical identity have
      pairwise-disjoint valid intervals.
- I2  tt is strictly increasing per batch (event log monotonicity).
- I3  Every closed tt_e equals the tt of some later batch (closure happens
      only at real write times).
- I4  Bi-temporal immutability: the believed state at as_of_tt = t never
      changes once t has passed, regardless of later writes.
- I5  Props round-trip through canonical JSON.
- I6  A row's belief interval is non-empty: tt_e > tt_s, always. A version
      created and closed at the same transaction time was believed at no
      transaction time at all, so it is not a row (D-059).

Every test below applies its ops in one of two shapes, and both matter:
`_apply_sequence` calls `apply_ops` bare, `_apply_batches` wraps each batch
in the begin()/commit() bracket `Store._write` uses. D-058 found a defect
visible only inside that bracket and only from a second op in the same
batch, which is exactly the pair of choices this file used to make.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings

from tgms.core.errors import NotFoundError
from tgms.core.model import OPEN_END, canonical_json, edge_eid
from tgms.storage.base import _remainder

from .conftest import fresh_adapter, op_batches, op_sequences

SETTINGS = settings(max_examples=120, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


def _all_identities(adapter):
    node_ids = {v.uid for v in adapter.all_node_versions()}
    edge_ids = {v.eid for v in adapter.all_edge_versions()}
    return node_ids, edge_ids


def _believed_state(adapter, as_of_tt):
    """Canonical serialization of everything believed at as_of_tt.

    Excludes tt_e: closing tt_e on a row is the *mechanism* by which later
    change is recorded, so it mutates legitimately; the believed content
    (identity, valid interval, label, props) must stay frozen.
    """
    def strip(r):
        r.pop("tt_e")
        return r

    nodes = sorted(
        (strip(v.to_json()) for v in adapter.all_node_versions() if v.believed_at(as_of_tt)),
        key=lambda r: (r["uid"], r["vt_s"], r["vid"]))
    edges = sorted(
        (strip(v.to_json()) for v in adapter.all_edge_versions() if v.believed_at(as_of_tt)),
        key=lambda r: (r["eid"], r["vt_s"], r["vid"]))
    return canonical_json({"nodes": nodes, "edges": edges})


def _apply_sequence(adapter, ops):
    """One op per batch at tt = 1, 2, 3, ...; skips no-target retract/correct."""
    applied_tts = []
    for i, op in enumerate(ops):
        tt = i + 1
        try:
            adapter.apply_ops([op], tt)
            applied_tts.append(tt)
        except NotFoundError:
            pass
    return applied_tts


def _apply_batches(adapter, batches):
    """Batches at tt = 1, 2, 3, ..., each through the begin()/commit()
    bracket. A batch is atomic: an op with no target aborts the whole batch,
    which is what `Store._write` does, so its tt never happened.

    Returns `(applied_tts, mid)` where `mid` maps each applied tt to the
    belief the batch could see of itself just before it committed — the read
    every op after the first one in a batch depends on.
    """
    applied_tts, mid = [], {}
    for i, batch in enumerate(batches):
        tt = i + 1
        adapter.begin()
        try:
            adapter.apply_ops(batch, tt)
        except NotFoundError:
            adapter.rollback()
            continue
        mid[tt] = _believed_intervals_at(adapter, OPEN_END)
        adapter.commit()
        applied_tts.append(tt)
    return applied_tts, mid


def _believed_intervals_at(adapter, as_of_tt):
    """Each identity's believed valid intervals at `as_of_tt`, sorted.

    Identities with nothing believed are dropped rather than mapped to `[]`,
    so a snapshot taken mid-batch compares equal to the same store read back
    at that tt after later batches have introduced identities of their own.
    """
    node_ids, edge_ids = _all_identities(adapter)
    out = {}
    for uid in node_ids:
        ivs = sorted((v.vt_s, v.vt_e)
                     for v in adapter.believed_node_versions(uid, as_of_tt=as_of_tt))
        if ivs:
            out["node " + uid] = ivs
    for eid in edge_ids:
        ivs = sorted((v.vt_s, v.vt_e)
                     for v in adapter.believed_edge_versions(eid, as_of_tt=as_of_tt))
        if ivs:
            out["edge " + eid] = ivs
    return out


@SETTINGS
@given(ops=op_sequences)
def test_disjointness_at_every_transaction_time(ops):
    adapter = fresh_adapter(paranoid=True)  # paranoid checks I1 at current tt per batch
    _apply_sequence(adapter, ops)
    node_ids, edge_ids = _all_identities(adapter)
    # I1 at *every historical* belief state, not just the final one
    for tt in range(1, len(ops) + 1):
        for uid in node_ids:
            vs = adapter.believed_node_versions(uid, as_of_tt=tt)
            ivs = sorted((v.vt_s, v.vt_e) for v in vs)
            assert all(e1 <= s2 for (_, e1), (s2, _) in zip(ivs, ivs[1:])), \
                f"node {uid} overlap at tt={tt}: {ivs}"
        for eid in edge_ids:
            vs = adapter.believed_edge_versions(eid, as_of_tt=tt)
            ivs = sorted((v.vt_s, v.vt_e) for v in vs)
            assert all(e1 <= s2 for (_, e1), (s2, _) in zip(ivs, ivs[1:])), \
                f"edge {eid} overlap at tt={tt}: {ivs}"
    adapter.close()


@SETTINGS
@given(ops=op_sequences)
def test_closed_tt_and_props_canonical(ops):
    adapter = fresh_adapter()
    applied = set(_apply_sequence(adapter, ops))
    for v in list(adapter.all_node_versions()) + list(adapter.all_edge_versions()):
        # I2/I3: versions are created and closed only at real batch times
        assert v.tt_s in applied
        assert v.tt_e == OPEN_END or (v.tt_e in applied and v.tt_e > v.tt_s)
        # I5: props survive canonical JSON round-trip
        assert json.loads(canonical_json(v.props)) == v.props
        # interval sanity
        assert v.vt_s < v.vt_e
    adapter.close()


@SETTINGS
@given(ops=op_sequences)
def test_bitemporal_immutability(ops):
    """I4 — the signature test: past belief states are frozen forever."""
    adapter = fresh_adapter()
    snapshots = {}
    for i, op in enumerate(ops):
        tt = i + 1
        try:
            adapter.apply_ops([op], tt)
        except NotFoundError:
            pass
        snapshots[tt] = _believed_state(adapter, tt)
    # after all writes, re-derive each historical belief state
    for tt, expected in snapshots.items():
        assert _believed_state(adapter, tt) == expected
    adapter.close()


# ---- the same invariants, inside the bracket and with batches (D-059) ----- #


@SETTINGS
@given(batches=op_batches)
def test_every_invariant_survives_a_multi_op_bracketed_batch(batches):
    """The shape `_apply_sequence` cannot make: more than one op per batch,
    inside begin()/commit().

    Three things are asserted that one op per unbracketed batch cannot see.
    I6 — no stored row was closed at the transaction time that created it.
    I1 — disjointness holds at every historical tt, as before. And the
    belief a batch reports *of itself*, mid-batch, is the belief it commits:
    every op after the first one carves against that read, so a backend that
    still reports a version its own batch has closed carves against a
    version that is gone (D-058, native's `believed_*`).
    """
    adapter = fresh_adapter(paranoid=True)  # I1 re-checked inside each batch
    applied, mid = _apply_batches(adapter, batches)
    for v in list(adapter.all_node_versions()) + list(adapter.all_edge_versions()):
        assert v.tt_s in applied
        assert v.tt_e == OPEN_END or (v.tt_e in applied and v.tt_e > v.tt_s), \
            f"belief interval is empty or unreal: tt_s={v.tt_s} tt_e={v.tt_e}"
    node_ids, edge_ids = _all_identities(adapter)
    for tt in applied:
        for uid in node_ids:
            ivs = sorted((v.vt_s, v.vt_e)
                         for v in adapter.believed_node_versions(uid, as_of_tt=tt))
            assert all(e1 <= s2 for (_, e1), (s2, _) in zip(ivs, ivs[1:])), \
                f"node {uid} overlap at tt={tt}: {ivs}"
        for eid in edge_ids:
            ivs = sorted((v.vt_s, v.vt_e)
                         for v in adapter.believed_edge_versions(eid, as_of_tt=tt))
            assert all(e1 <= s2 for (_, e1), (s2, _) in zip(ivs, ivs[1:])), \
                f"edge {eid} overlap at tt={tt}: {ivs}"
        assert mid[tt] == _believed_intervals_at(adapter, tt), \
            f"batch {tt} saw a different store than it committed"
    adapter.close()


def _edge_rows(adapter):
    return sorted((v.vt_s, v.vt_e, v.tt_s, v.tt_e, v.props.get("p"))
                  for v in adapter.all_edge_versions())


def _assert_edge_op(vt_s, vt_e, p):
    return {"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
            "props": {"p": p}, "vt_s": vt_s, "vt_e": vt_e, "disc": ""}


def test_a_version_created_and_closed_in_one_batch_is_not_stored():
    """I6, stated as the rule it comes from (D-059).

    A version written at tt and closed at tt was believed over the empty
    transaction interval [tt, tt) — at no transaction time at all. It is not
    a correction, it is not a superseded belief, it is not history: it is a
    row the batch changed its mind about before anyone could read it. So
    closing a version at the transaction time that created it *retires* it.

    That is what keeps `_vid = sha256(identity:tt_s:vt_s)` intact. The carve
    re-emits the remainder at the same `vt_s`, which derives the same vid as
    the version it just replaced — a collision only because both rows claim
    to exist, and only one of them ever did.
    """
    for second, expect in [
        # carve right: the remainder keeps vt_s, so it re-derives the vid of
        # the version being replaced — the case DuckDB refused outright
        (_assert_edge_op(15, 25, 2), [(10, 15, 1, OPEN_END, 1),
                                      (15, 25, 1, OPEN_END, 2)]),
        # carve left: no vid collides here, so *both* backends took this one
        # and stored the empty-belief row. The rule, not the collision, is
        # what removes it
        (_assert_edge_op(5, 15, 2), [(5, 15, 1, OPEN_END, 2),
                                     (15, 20, 1, OPEN_END, 1)]),
        # replaced whole: no remainder at all, and the new version itself
        # carries the retired vid
        (_assert_edge_op(10, 30, 2), [(10, 30, 1, OPEN_END, 2)]),
        # untouched neighbour: nothing overlaps, so nothing is retired
        (_assert_edge_op(30, 40, 2), [(10, 20, 1, OPEN_END, 1),
                                      (30, 40, 1, OPEN_END, 2)]),
    ]:
        adapter = fresh_adapter(paranoid=True)
        adapter.begin()
        adapter.apply_ops([_assert_edge_op(10, 20, 1), second], 1)
        adapter.commit()
        assert _edge_rows(adapter) == expect, second
        adapter.close()


def test_retract_and_correct_also_retire_their_own_batch_s_version():
    """The rule is about closing, not about `assert_edge`: `_retract` and
    `_correct` close the versions they replace through the same primitive."""
    eid = edge_eid("a", "b", "R")

    adapter = fresh_adapter(paranoid=True)
    adapter.begin()
    adapter.apply_ops([_assert_edge_op(0, 100, 1),
                       {"op": "retract", "ref": {"kind": "edge", "src": "a",
                                                 "dst": "b", "rel_type": "R",
                                                 "disc": ""}, "t": 50}], 1)
    adapter.commit()
    assert _edge_rows(adapter) == [(0, 50, 1, OPEN_END, 1)]
    assert [(v.vt_s, v.vt_e) for v in adapter.believed_edge_versions(eid)] == [(0, 50)]
    adapter.close()

    adapter = fresh_adapter(paranoid=True)
    adapter.begin()
    adapter.apply_ops([_assert_edge_op(0, 100, 1),
                       {"op": "correct", "ref": {"kind": "edge", "src": "a",
                                                 "dst": "b", "rel_type": "R",
                                                 "disc": ""},
                        "props": {"p": 2}, "vt_s": 40, "vt_e": 60}], 1)
    adapter.commit()
    assert _edge_rows(adapter) == [(0, 40, 1, OPEN_END, 1),
                                   (40, 60, 1, OPEN_END, 2),
                                   (60, 100, 1, OPEN_END, 1)]
    adapter.close()


def test_a_version_closed_by_a_LATER_batch_is_still_history():
    """The other side of the rule, so it cannot be read as "same batch wins".
    Across batches the closed version is exactly what belief history is, and
    retiring it would be erasure — which is the property D-023 vaults."""
    adapter = fresh_adapter(paranoid=True)
    for tt, op in ((1, _assert_edge_op(10, 20, 1)), (2, _assert_edge_op(15, 25, 2))):
        adapter.begin()
        adapter.apply_ops([op], tt)
        adapter.commit()
    assert _edge_rows(adapter) == [(10, 15, 2, OPEN_END, 1),
                                   (10, 20, 1, 2, 1),          # closed, kept
                                   (15, 25, 2, OPEN_END, 2)]
    eid = edge_eid("a", "b", "R")
    assert [(v.vt_s, v.vt_e) for v in
            adapter.believed_edge_versions(eid, as_of_tt=1)] == [(10, 20)]
    adapter.close()


def test_remainder_carving():
    assert _remainder(0, 10, 3, 7) == [(0, 3), (7, 10)]
    assert _remainder(0, 10, 0, 10) == []
    assert _remainder(0, 10, 0, 5) == [(5, 10)]
    assert _remainder(0, 10, 5, 10) == [(0, 5)]
    assert _remainder(3, 7, 0, 10) == []
    assert _remainder(0, 10, 10, 20) == [(0, 10)]  # non-overlap: whole interval kept


def test_retract_is_evolution_not_erasure():
    """After retract at t, the old full interval is still believed at old tt."""
    adapter = fresh_adapter()
    adapter.apply_ops([{"op": "assert_edge", "src": "a", "dst": "b", "rel_type": "R",
                        "props": {}, "vt_s": 0, "vt_e": OPEN_END, "disc": ""}], 1)
    adapter.apply_ops([{"op": "retract",
                        "ref": {"kind": "edge", "src": "a", "dst": "b",
                                "rel_type": "R", "disc": ""}, "t": 100}], 2)
    eid = edge_eid("a", "b", "R")
    old = adapter.believed_edge_versions(eid, as_of_tt=1)
    new = adapter.believed_edge_versions(eid, as_of_tt=2)
    assert [(v.vt_s, v.vt_e) for v in old] == [(0, OPEN_END)]
    assert [(v.vt_s, v.vt_e) for v in new] == [(0, 100)]
    adapter.close()


def test_correct_preserves_remainder_and_replaces_props():
    adapter = fresh_adapter()
    adapter.apply_ops([{"op": "assert_node", "uid": "a", "label": "N",
                        "props": {"p": 1}, "vt_s": 0, "vt_e": 100}], 1)
    adapter.apply_ops([{"op": "correct", "ref": {"kind": "node", "uid": "a"},
                        "props": {"p": 2}, "vt_s": 40, "vt_e": 60}], 2)
    now_believed = adapter.believed_node_versions("a", as_of_tt=2)
    by_iv = {(v.vt_s, v.vt_e): v.props for v in now_believed}
    assert by_iv == {(0, 40): {"p": 1}, (40, 60): {"p": 2}, (60, 100): {"p": 1}}
    # the erroneous belief is still visible at the old as_of_tt
    old = adapter.believed_node_versions("a", as_of_tt=1)
    assert [(v.vt_s, v.vt_e, v.props["p"]) for v in old] == [(0, 100, 1)]
    adapter.close()


def test_columnar_int_columns_are_int64_on_every_backend():
    """`edges_columnar`'s integer columns are int64, whatever the backend.

    Operators do arithmetic on these ids — `src * n + dst` pair keys, group
    offsets — so a narrower dtype would overflow silently on one backend and
    not on the other, which is the one failure mode a differential suite
    cannot see: both backends would still be self-consistent. The native
    engine stores endpoints as u32 and something has to widen them; this
    pins *that* the widening happens, not which side of the boundary does
    it.

    It is also the assertion lesson §13 asks for. A projection experiment
    once "proved" materialization was free by turning a knob that was not
    connected, and one assertion about what the knob returned would have
    caught it; a control deserves the same scrutiny as the thing under test.
    """
    import numpy as np

    adapter = fresh_adapter()
    adapter.apply_ops([{"op": "assert_edge", "src": "a", "dst": "b",
                        "rel_type": "R", "props": {}, "vt_s": 0,
                        "vt_e": OPEN_END, "disc": ""}], 1)
    for cols in (None, ("src_id",), ("src_id", "dst_id", "vt_s", "vt_e")):
        for vt_min, vt_max in ((None, None), (-20, -10)):  # non-empty, empty
            got = adapter.edges_columnar(vt_min=vt_min, vt_max=vt_max,
                                         columns=cols)
            names = adapter.EDGE_INT_COLS if cols is None else cols
            for c in names:
                assert got[c].dtype == np.int64, (c, cols, vt_min, got[c].dtype)
    adapter.close()
