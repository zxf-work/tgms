"""M4.1 — `tgms/tgir/footprint.py` against D13.20–D13.22.

The gate this file discharges (M4_IMPLEMENTATION_PLAN §6, M4.1 row):

(a) **all five op kinds × both arms, produced from log records alone** — every
    builder test here reads its op back out of a real `eventlog.jsonl` file
    rather than from the dict that was passed to the writer, so "no store
    access is required to build a footprint" is exercised rather than asserted;
(b) **argument defaults match `apply_ops`**, proved differentially: each op is
    applied to a real store, the rows it actually wrote are diffed out, and the
    footprint is checked to *cover* every one of them. A builder that defaults
    `vt_e` differently from `_assert_node` produces a footprint for an op that
    was applied with different bounds, and that is a false-freshness source
    rather than a cosmetic disagreement.

The soundness scenarios (CE-*, FF-*) are **not** here: they belong to
`tests/test_freshness_soundness.py`, which is authored independently.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, edge_eid
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import TOP
from tgms.tgir.footprint import (
    CLASS_ASSERT, BatchFootprint, footprints_of_batch, footprints_of_op,
)

CARVE_CAPABLE = ("assert_node", "assert_edge", "correct", "retract")


# ---------------------------------------------------------------------------
# the log round-trip: a footprint is built from a logged record, never a dict
# ---------------------------------------------------------------------------

def _logged(ops: list[dict[str, Any]], tt: int = 1000) -> BatchFootprint:
    """Append `ops` to a real event log, read the record back off disk, and
    build footprints from *that*. The dict that went in is discarded, so
    anything the writer added and the reader cannot see would fail here."""
    path = Path(tempfile.mkdtemp()) / "eventlog.jsonl"
    log = EventLog(path)
    log.append(tt, ops)
    batches = list(log.batches())
    assert len(batches) == 1
    return footprints_of_batch(batches[0])


def test_batch_footprint_carries_the_logged_batch_id_and_tt():
    fp = _logged([make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=10)],
                 tt=1234)
    assert fp.tt == 1234
    assert len(fp.batch_id) == 16  # the log's own content-addressed id


@pytest.mark.parametrize("op,expect_kind,expect_class,expect_entity", [
    (make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=10),
     "assert_node", CLASS_ASSERT, "node"),
    (make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={}, vt_s=0, vt_e=10),
     "assert_edge", CLASS_ASSERT, "edge"),
    (make_op("correct", ref={"kind": "node", "uid": "A"}, props={"p": 1}, vt_s=0, vt_e=10),
     "correct", "C", "node"),
    (make_op("retract", ref={"kind": "node", "uid": "A"}, t=5),
     "retract", "D", "node"),
])
def test_the_four_carve_capable_kinds_emit_exactly_two_arms(
        op, expect_kind, expect_class, expect_entity):
    """D13.21a: the carve arm is emitted **unconditionally**, without knowing
    whether the op carved — the builder cannot know, and D13.1 says widen."""
    fps = _logged([op]).ops
    assert [f.arm for f in fps] == ["value", "carve"]
    value, carve = fps
    assert value.kind == carve.kind == expect_kind
    assert value.cls == carve.cls == expect_class
    assert value.entity_kind == carve.entity_kind == expect_entity
    # same kind, entity_kind, identity and rel_type; only vt and props differ
    assert carve.identity == value.identity
    assert carve.rel_type == value.rel_type
    assert carve.vt is TOP
    assert set(carve.props) == {"@recut", "@version"}
    assert carve.vt is not value.vt or value.vt is TOP


def test_ingest_events_emits_two_value_arms_and_no_carve_arm():
    """D13.21a: `ingest_events` supersedes nothing, because every event without
    an explicit `disc` gets its batch offset as discriminator and is therefore
    its own logical edge (D2.1)."""
    op = make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 46},
    ])
    fps = _logged([op]).ops
    assert [f.arm for f in fps] == ["value", "value"]
    assert [f.entity_kind for f in fps] == ["edge", "node"]
    assert all(f.cls == "A" for f in fps)
    assert not any(f.arm == "carve" for f in fps)


def test_all_five_kinds_are_covered_by_one_batch():
    """(a) of the gate, in one record: five kinds, six value arms, four carve
    arms."""
    ops = [
        make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=10),
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={}, vt_s=0, vt_e=10),
        make_op("correct", ref={"kind": "edge", "src": "A", "dst": "B", "rel_type": "MSG"},
                props={"w": 2}, vt_s=0, vt_e=10),
        make_op("retract", ref={"kind": "edge", "src": "A", "dst": "B", "rel_type": "MSG"}, t=5),
        make_op("ingest_events", events=[{"src": "C", "dst": "D",
                                          "rel_type": "MSG", "vt_s": 45}]),
    ]
    fps = _logged(ops).ops
    assert {f.kind for f in fps} == {"assert_node", "assert_edge", "correct",
                                     "retract", "ingest_events"}
    assert sum(1 for f in fps if f.arm == "carve") == 4
    assert sum(1 for f in fps if f.arm == "value") == 6
    assert [f.seq for f in fps] == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


# ---------------------------------------------------------------------------
# D13.22's table, row by row
# ---------------------------------------------------------------------------

def test_assert_props_is_star_not_the_asserted_keys():
    """An overwriting assert replaces a whole version, so keys the new props
    *omit* also change. Narrowing to `keys(props)` would be a false-freshness
    source for every operator reading a key this assert dropped."""
    value = _logged([make_op("assert_node", uid="A", label="N",
                             props={"tier": "gold"}, vt_s=0, vt_e=10)]).ops[0]
    assert value.props is TOP


def test_correct_on_a_node_emits_label_and_an_edge_correct_does_not():
    """By L2.2 a multi-hit `correct` can change a label, nondeterministically.
    Omitting `@label` here would be a false-freshness source for every
    label-sensitive operator."""
    node = _logged([make_op("correct", ref={"kind": "node", "uid": "A"},
                            props={"tier": "gold"}, vt_s=0, vt_e=10)]).ops[0]
    edge = _logged([make_op("correct", ref={"kind": "edge", "src": "A", "dst": "B",
                                            "rel_type": "MSG"},
                            props={"w": 2}, vt_s=0, vt_e=10)]).ops[0]
    assert set(node.props) == {"tier", "@label", "@extent", "@event_key"}
    assert set(edge.props) == {"w", "@extent", "@event_key"}
    assert "@label" not in edge.props


def test_correct_unions_the_pseudo_keys_rather_than_replacing_them():
    """CE-5's channel: a property-only correction over a sub-interval
    multiplies an identity's *events*, so an event-keyed operator that reads no
    property at all still changes. An event-keyed scope must not be narrowed to
    the properties it reads."""
    value = _logged([make_op("correct", ref={"kind": "node", "uid": "A"},
                             props={}, vt_s=0, vt_e=10)]).ops[0]
    assert set(value.props) == {"@label", "@extent", "@event_key"}


def test_retract_value_arm_is_extent_and_event_key_not_star():
    """The narrowing D13.22 deliberately made when `@version` moved to the
    carve arm. A retract removes coverage and re-keys events; it does not
    rewrite arbitrary property values."""
    value, carve = _logged([make_op("retract", ref={"kind": "node", "uid": "A"},
                                    t=5)]).ops
    assert value.props is not TOP
    assert set(value.props) == {"@extent", "@event_key"}
    assert set(carve.props) == {"@recut", "@version"}


def test_retract_vt_is_from_t_to_open_end():
    value = _logged([make_op("retract", ref={"kind": "node", "uid": "A"}, t=5)]).ops[0]
    assert value.vt == ((5, OPEN_END),)


def test_retract_entity_kind_follows_the_ref():
    node = _logged([make_op("retract", ref={"kind": "node", "uid": "A"}, t=5)]).ops[0]
    edge = _logged([make_op("retract", ref={"kind": "edge", "src": "A", "dst": "B",
                                            "rel_type": "MSG"}, t=5)]).ops[0]
    assert node.entity_kind == "node" and edge.entity_kind == "edge"
    assert edge.rel_type == ("MSG",)


def test_the_ingest_node_arm_is_unconditional_and_reaches_open_end():
    """Emitted even when no uid is new: the builder reads only the log and
    cannot know which uids existed. Widening, therefore sound."""
    _edge, node = _logged([make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "C", "dst": "E", "rel_type": "MSG", "vt_s": 50},
    ])]).ops
    assert node.identity.uid == ("C", "D", "E")
    assert node.vt == ((45, OPEN_END),)
    assert set(node.props) == {"@identity", "@extent", "@event_key"}


def test_the_ingest_edge_arm_hulls_the_events_valid_times():
    """§13.6's `fp1e`: two events at 45 and 46 give `vt_closed(45, 47)`."""
    edge, _node = _logged([make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 46},
    ])]).ops
    assert edge.vt == ((45, 48),)
    assert edge.props is TOP


# ---------------------------------------------------------------------------
# CO-8: every coarsened field is a set, and nothing is a scalar
# ---------------------------------------------------------------------------

def test_ingest_coarsened_fields_are_sets_not_scalars():
    """CO-8. `["C"] ∈ ["X","B","C"]` returns false under a scalar-typed
    membership test, and a false here is a false negative — unsound."""
    edge, node = _logged([make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "X", "dst": "D", "rel_type": "LIKE", "vt_s": 46},
    ])]).ops
    assert edge.identity.src == ("C", "X")
    assert edge.identity.dst == ("D",)
    assert edge.identity.rel_type == ("LIKE", "MSG")
    assert edge.rel_type == ("LIKE", "MSG")
    assert node.identity.uid == ("C", "D", "X")
    # and the wire form is a list even for the single-valued arm
    assert edge.to_json()["identity"]["dst"] == ["D"]


def test_a_single_identity_op_renders_a_scalar_on_the_wire():
    """D13.20's specimen and §13.6's witnesses both spell it `{"uid": "A"}`."""
    value = _logged([make_op("assert_node", uid="A", label="N", props={},
                             vt_s=0, vt_e=10)]).ops[0]
    assert value.to_json()["identity"] == {"uid": "A"}
    assert value.identity.uid == ("A",)  # still a set internally


# ---------------------------------------------------------------------------
# the eid is DERIVED, not read (obligation 2)
# ---------------------------------------------------------------------------

def test_eid_is_derived_from_four_logged_fields_and_the_record_carries_none():
    """`_ref_json` writes an edge ref as `{kind, src, dst, rel_type, disc}` and
    carries no `eid` at all — so the footprint has to compute it, and that it
    *can* is what keeps D13.20's restriction true."""
    op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", disc="d1",
                 props={}, vt_s=0, vt_e=10)
    assert "eid" not in op
    value = _logged([op]).ops[0]
    assert value.identity.eid == (edge_eid("A", "B", "MSG", "d1"),)


def test_assert_edge_disc_defaults_to_empty_string_not_a_generated_one():
    """`_assert_edge` reads `op.get("disc", "")`. Using `_ingest_events`' rule
    here would key an entirely different logical edge."""
    op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                 vt_s=0, vt_e=10)
    value = _logged([op]).ops[0]
    assert value.identity.eid == (edge_eid("A", "B", "MSG", ""),)


def test_ingest_disc_default_uses_the_ops_offset_and_the_event_index():
    """`ev.get("disc", f"#{op.get('offset', 0) + i}")` — both halves matter."""
    op = make_op("ingest_events", offset=100, events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 46},
    ])
    edge, _node = _logged([op]).ops
    assert edge.identity.eid == tuple(sorted({
        edge_eid("C", "D", "MSG", "#100"), edge_eid("C", "D", "MSG", "#101")}))


def test_an_explicit_disc_on_an_event_is_honoured():
    """The §9.4 gap: an event carrying an explicit `disc` addresses the logical
    edge that `disc` names, which may already exist."""
    op = make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45, "disc": "d9"}])
    edge, _node = _logged([op]).ops
    assert edge.identity.eid == (edge_eid("C", "D", "MSG", "d9"),)
    assert edge.eids == edge.identity.eid
    # E-1 amends D13.22's table, which carried no `eid` on this arm at all
    assert edge.to_json()["identity"]["eid"] == [edge_eid("C", "D", "MSG", "d9")]


# ---------------------------------------------------------------------------
# (b) the differential: the defaults match `apply_ops`
# ---------------------------------------------------------------------------

def _rows(store) -> tuple[dict[str, Any], dict[str, Any]]:
    nodes = {v.vid: v for v in store.adapter.all_node_versions()}
    edges = {v.vid: v for v in store.adapter.all_edge_versions()}
    return nodes, edges


def _covers(fp_vt, vt_s: int, vt_e: int) -> bool:
    """Does the footprint's `vt` overlap `[vt_s, vt_e)`? `"*"` overlaps
    everything, which is the carve arm's whole point."""
    if fp_vt is TOP:
        return True
    return any(a < vt_e and vt_s < b for a, b in fp_vt)


def _apply_and_diff(op: dict[str, Any], setup=None, backend: str = "duckdb"):
    """Apply one op through the real write path, and hand back (footprints, the
    node/edge versions that op actually wrote)."""
    path = Path(tempfile.mkdtemp()) / "store"
    store = tgms.open(path, backend=backend)
    if setup is not None:
        setup(store)
    before_n, before_e = _rows(store)
    store._write([op])  # the same path a public write takes, one op at a time
    after_n, after_e = _rows(store)
    written_n = [v for vid, v in after_n.items() if vid not in before_n]
    written_e = [v for vid, v in after_e.items() if vid not in before_e]
    log = EventLog(path / "eventlog.jsonl")
    logged = list(log.batches())[-1]
    fps = footprints_of_batch(logged).ops
    store.close()
    return fps, written_n, written_e


@pytest.mark.parametrize("vt_e_given", [True, False])
def test_assert_node_footprint_covers_every_row_the_applier_wrote(vt_e_given):
    """The default that matters: `_assert_node` reads `op.get("vt_e",
    OPEN_END)`. Omit `vt_e` from the op and the footprint must still describe
    `[vt_s, OPEN_END)`."""
    kwargs = {"uid": "A", "label": "N", "props": {"p": 1}, "vt_s": 10}
    if vt_e_given:
        kwargs["vt_e"] = 20
    fps, written_n, _ = _apply_and_diff(make_op("assert_node", **kwargs))
    value = fps[0]
    assert written_n, "the op wrote nothing; the differential proves nothing"
    for v in written_n:
        assert v.uid in value.identity.uid
        assert _covers(value.vt, v.vt_s, v.vt_e), (value.vt, v.vt_s, v.vt_e)
    if not vt_e_given:
        assert value.vt == ((10, OPEN_END),)


@pytest.mark.parametrize("vt_e_given", [True, False])
def test_assert_edge_footprint_covers_every_row_the_applier_wrote(vt_e_given):
    kwargs = {"src": "A", "dst": "B", "rel_type": "MSG", "props": {}, "vt_s": 10}
    if vt_e_given:
        kwargs["vt_e"] = 20
    fps, _, written_e = _apply_and_diff(make_op("assert_edge", **kwargs))
    value = fps[0]
    assert written_e
    for v in written_e:
        assert v.eid in value.identity.eid
        assert v.rel_type in value.rel_type
        assert _covers(value.vt, v.vt_s, v.vt_e)


def test_ingest_events_footprint_covers_every_row_the_applier_wrote():
    """Both arms together: the edge arm covers the event rows, the node arm
    covers the node versions `_ingest_events` writes for new uids."""
    op = make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
        {"src": "C", "dst": "E", "rel_type": "MSG", "vt_s": 50, "vt_e": 60},
    ])
    fps, written_n, written_e = _apply_and_diff(op)
    edge, node = fps
    assert written_e and written_n
    for v in written_e:
        assert v.src in edge.identity.src and v.dst in edge.identity.dst
        assert v.eid in edge.identity.eid
        assert _covers(edge.vt, v.vt_s, v.vt_e)
    for v in written_n:
        assert v.uid in node.identity.uid
        assert _covers(node.vt, v.vt_s, v.vt_e)


def test_the_carve_arm_covers_the_fragments_the_value_arm_cannot():
    """D13.21a, differentially. An overwriting assert over a *sub-interval* of
    a believed version makes `_remainder` re-insert fragments whose endpoints
    come from the superseded version. Here the right fragment sits **above**
    the op's own `vt_e`, so the value arm's interval does not reach it and only
    the carve arm does."""
    def setup(store):
        store.assert_node("A", "N", {"tier": "bronze"}, 0, 100)

    fps, written_n, _ = _apply_and_diff(
        make_op("assert_node", uid="A", label="N", props={"tier": "gold"},
                vt_s=40, vt_e=50), setup=setup)
    value, carve = fps
    outside = [v for v in written_n if not _covers(value.vt, v.vt_s, v.vt_e)]
    assert outside, "no fragment landed outside the op's own bounds; widen the setup"
    assert all(v.vt_s >= 50 or v.vt_e <= 40 for v in outside)
    for v in outside:
        assert _covers(carve.vt, v.vt_s, v.vt_e)
        assert v.uid in carve.identity.uid


def test_correct_and_retract_footprints_cover_what_they_wrote():
    def setup(store):
        store.assert_node("A", "N", {"tier": "bronze"}, 0, 100)

    fps, written_n, _ = _apply_and_diff(
        make_op("correct", ref={"kind": "node", "uid": "A"},
                props={"tier": "gold"}, vt_s=10, vt_e=20), setup=setup)
    assert written_n
    assert all(_covers(fps[0].vt, v.vt_s, v.vt_e) or _covers(fps[1].vt, v.vt_s, v.vt_e)
               for v in written_n)

    fps, written_n, _ = _apply_and_diff(
        make_op("retract", ref={"kind": "node", "uid": "A"}, t=60), setup=setup)
    assert written_n
    assert all(v.uid in fps[0].identity.uid for v in written_n)


# ---------------------------------------------------------------------------
# D13.21: the three constructors, and nothing else, build a vt
# ---------------------------------------------------------------------------

def test_vt_closed_is_right_closed_so_a_fragment_starting_at_vt_e_is_covered():
    """D8.6 made structural. A carve fragment can start exactly at the op's
    `vt_e`, so the footprint's interval is closed at the right."""
    value = _logged([make_op("assert_node", uid="A", label="N", props={},
                             vt_s=0, vt_e=10)]).ops[0]
    assert value.vt == ((0, 11),)
    assert _covers(value.vt, 10, 20)   # the fragment at the boundary
    assert not _covers(value.vt, 11, 20)


def test_vt_closed_saturates_at_open_end_rather_than_overflowing():
    value = _logged([make_op("assert_node", uid="A", label="N", props={},
                             vt_s=0, vt_e=OPEN_END)]).ops[0]
    assert value.vt == ((0, OPEN_END),)


def test_an_ingest_event_with_a_falsy_vt_e_falls_back_like_the_applier():
    """`_ingest_events` reads `ev.get("vt_e") or vt_s + 1` — note `or`, so a
    literal `0` also falls back. A builder using `.get("vt_e", vt_s + 1)`
    would describe `[45, 0)` and raise, or worse, describe nothing."""
    edge, _node = _logged([make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45, "vt_e": 0}])]).ops
    assert edge.vt == ((45, 47),)  # vt_closed(45, 46)


# ---------------------------------------------------------------------------
# totality and refusal
# ---------------------------------------------------------------------------

def test_an_unknown_op_kind_refuses_rather_than_producing_nothing():
    """A silently empty footprint list is a false negative for every scope."""
    with pytest.raises(InvalidArgError):
        footprints_of_op({"op": "teleport", "uid": "A"}, 0)


def test_an_empty_ingest_batch_describes_nothing():
    """`_ingest_events` over an empty list writes nothing at all, so there is
    nothing to describe — and a vacuous `"*"`-shaped footprint would match
    every scope in the store."""
    assert footprints_of_op(make_op("ingest_events", events=[]), 0) == ()


def test_class_e_within_batch_retirement_emits_no_special_footprint():
    """L2.1: nothing in the log record distinguishes Class E — it is a
    consequence of two ops on one identity in one batch, and the second op's
    own footprint already covers the region. So the builder needs no Class-E
    branch, and this test pins the *absence*."""
    ops = [make_op("assert_node", uid="A", label="N", props={"v": 1}, vt_s=0, vt_e=10),
           make_op("assert_node", uid="A", label="N", props={"v": 2}, vt_s=0, vt_e=10)]
    fps = _logged(ops).ops
    assert len(fps) == 4  # two ops, two arms each — no more, no fewer
    assert {f.cls for f in fps} == {CLASS_ASSERT}
    assert [f.seq for f in fps] == [0, 0, 1, 1]


def test_class_is_the_literal_disjunction_for_asserts():
    """CO-3: A-vs-B is decided by `believed_node_versions(uid)`, which is store
    state a log record does not carry. `"A|B"` is the wire spelling."""
    assert CLASS_ASSERT == "A|B"
    fps = _logged([
        make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=10),
        make_op("ingest_events", events=[{"src": "C", "dst": "D",
                                          "rel_type": "MSG", "vt_s": 1}]),
        make_op("correct", ref={"kind": "node", "uid": "A"}, props={}, vt_s=0, vt_e=10),
        make_op("retract", ref={"kind": "node", "uid": "A"}, t=5),
    ]).ops
    by_kind = {f.kind: f.cls for f in fps}
    assert by_kind == {"assert_node": "A|B", "ingest_events": "A",
                       "correct": "C", "retract": "D"}


def test_a_node_footprint_carries_a_widening_rel_type_not_an_empty_one():
    """`intersects` guards the `rel_types` conjunct on entity kind and never
    reads this. It is `"*"` rather than `()` so that if the guard were ever
    dropped the failure would widen, not narrow."""
    for op in (make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=10),
               make_op("retract", ref={"kind": "node", "uid": "A"}, t=5)):
        assert _logged([op]).ops[0].rel_type is TOP


def test_the_footprint_json_round_trips_through_canonical_json():
    """A footprint is dumped into harness artifacts, so it has to serialize."""
    fps = _logged([make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45}])])
    text = json.dumps(fps.to_json(), sort_keys=True)
    assert json.loads(text)["ops"][0]["kind"] == "ingest_events"
