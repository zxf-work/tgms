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
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings

from tgms.core.errors import NotFoundError
from tgms.core.model import OPEN_END, canonical_json
from tgms.storage.base import _remainder

from .conftest import RELS, UIDS, fresh_adapter, op_sequences

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
    from tgms.core.model import edge_eid
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
