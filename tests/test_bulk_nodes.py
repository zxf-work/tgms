"""Structured bulk node loading — `ingest_events`' optional `nodes` array.

The problem it solves is not subtle. `Store.assert_node` writes **one batch per
call**, and a batch is a log append plus a manifest commit whose cost grows
with the store, so loading N labelled nodes that way is O(N²) in time *and* in
bytes. Measured on this machine before the change: 8,435 asserts produced 8,436
manifests and 15 GB, with the instantaneous rate falling from 29 to 15 ops/s
over the first 8k nodes. LDBC SNB needs eight node labels with rich properties,
which only `assert_*` could express — hence this path.

Three properties are load-bearing and each is tested here:

1. **Absent field = exactly today's behaviour**, byte for byte. The frozen
   digest receipt is the repo-level proof; this file pins it per-store.
2. **A collision refuses.** An explicit node whose uid already has a believed
   version is a loader bug, and refusing is what keeps the op Class A (D2.1) —
   an op that cannot supersede cannot carve, which is what lets its freshness
   footprint keep emitting no carve arm (D13.21a).
3. **The footprint covers what the op writes.** The node arm has to reach the
   explicit uids, their intervals and their property keys, or a scope narrowed
   to one of them would be missed — a false negative, the one direction D1.13
   forbids.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import TOP
from tgms.tgir.footprint import footprints_of_op

PEOPLE = [
    {"uid": "p1", "label": "Person", "props": {"name": "Ada", "city": "London"},
     "vt_s": 10},
    {"uid": "p2", "label": "Person", "props": {"name": "Bo"}, "vt_s": 20},
]
FORUMS = [{"uid": "f1", "label": "Forum", "props": {"title": "T"},
           "vt_s": 30, "vt_e": 500}]
EVENTS = [{"src": "p1", "dst": "p2", "rel_type": "KNOWS", "vt_s": 100}]


def _open(tmp_path, name="s"):
    return tgms.open(tmp_path / name)


# ---------------------------------------------------------------------------
# 1. the optional field is optional, and absent means unchanged
# ---------------------------------------------------------------------------

def _content(store) -> str:
    """Store content with **transaction time projected out**.

    `store_digest()` covers every version's `tt_s`/`tt_e`, and the hybrid
    logical clock stamps those at write time — so two independently built
    stores of identical data never share a digest, by construction, and `vid`
    inherits the difference because it hashes `tt_s`. Comparing digests across
    two builds therefore tests the clock, not the code. What the compatibility
    claim is actually about is the rows, so that is what this compares.

    The byte-for-byte claim at the repo level is carried by the frozen digest
    receipt (`scripts/check_digest_stability.py`, 38 digests over fixed
    stores), which is the right instrument for it precisely because those
    stores are not rebuilt.
    """
    def rows(vs, keys):
        return sorted(tuple(getattr(v, k) for k in keys) for v in vs)
    nodes = rows(store.adapter.all_node_versions(),
                 ("uid", "label", "vt_s", "vt_e", "source"))
    edges = rows(store.adapter.all_edge_versions(),
                 ("eid", "src", "dst", "rel_type", "disc", "vt_s", "vt_e"))
    props = sorted((v.uid, json.dumps(v.props, sort_keys=True))
                   for v in store.adapter.all_node_versions())
    return json.dumps([nodes, edges, props], sort_keys=True)


def test_without_nodes_the_store_is_what_it_always_was(tmp_path):
    """The compatibility claim: the same event stream, once through the old
    call signature and once through the new one with `nodes` left out, builds
    the same store."""
    a = _open(tmp_path, "a")
    b = _open(tmp_path, "b")
    try:
        a.ingest_events(EVENTS)
        b.ingest_events(EVENTS, nodes=None)
        assert _content(a) == _content(b)
        # and the auto-created endpoints are still bare, still `Node`-labelled
        rows = {v.uid: v for v in a.adapter.all_node_versions()}
        assert set(rows) == {"p1", "p2"}
        assert {v.label for v in rows.values()} == {"Node"}
        assert all(v.props == {} for v in rows.values())
    finally:
        a.close()
        b.close()


def test_the_logged_op_carries_no_nodes_key_when_none_was_given(tmp_path):
    """An old reader must not even see the new field on an old-shaped load."""
    s = _open(tmp_path)
    try:
        s.ingest_events(EVENTS)
        ops = [op for b in s.eventlog.batches() for op in b["ops"]]
        assert ops and all("nodes" not in op for op in ops)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 2. what the structured path writes
# ---------------------------------------------------------------------------

def test_explicit_nodes_get_their_own_labels_props_and_intervals(tmp_path):
    s = _open(tmp_path)
    try:
        s.ingest_events(EVENTS, nodes=PEOPLE + FORUMS)
        rows = {v.uid: v for v in s.adapter.all_node_versions()}
        assert set(rows) == {"p1", "p2", "f1"}
        assert rows["p1"].label == "Person"
        assert rows["p1"].props == {"name": "Ada", "city": "London"}
        assert rows["p1"].vt_s == 10 and rows["p1"].vt_e == OPEN_END
        assert rows["f1"].label == "Forum"
        assert (rows["f1"].vt_s, rows["f1"].vt_e) == (30, 500)
    finally:
        s.close()


def test_an_explicit_node_is_not_also_auto_created_as_a_bare_endpoint(tmp_path):
    """Two versions of one uid over overlapping intervals would violate
    disjointness. The explicit record wins and the endpoint is skipped."""
    s = _open(tmp_path)
    try:
        s.ingest_events(EVENTS, nodes=PEOPLE)
        versions = [v for v in s.adapter.all_node_versions() if v.uid == "p1"]
        assert len(versions) == 1
        assert versions[0].label == "Person"
    finally:
        s.close()


def test_endpoints_with_no_explicit_record_are_still_auto_created(tmp_path):
    """Mixing the two halves is the point: SNB has labelled Persons and also
    edges to uids the node file did not describe."""
    s = _open(tmp_path)
    try:
        s.ingest_events(
            [{"src": "p1", "dst": "stranger", "rel_type": "KNOWS", "vt_s": 100}],
            nodes=PEOPLE)
        rows = {v.uid: v for v in s.adapter.all_node_versions()}
        assert rows["p1"].label == "Person"
        assert rows["stranger"].label == "Node" and rows["stranger"].props == {}
    finally:
        s.close()


def test_nodes_can_be_loaded_with_no_events_at_all(tmp_path):
    s = _open(tmp_path)
    try:
        s.ingest_events([], nodes=PEOPLE)
        assert {v.uid for v in s.adapter.all_node_versions()} == {"p1", "p2"}
        assert not list(s.adapter.all_edge_versions())
    finally:
        s.close()


def test_the_paranoid_disjointness_check_passes_on_a_structured_load(tmp_path):
    """`apply_ops(paranoid=True)` re-checks disjointness per touched identity.
    A bulk path that wrote overlapping versions would surface here."""
    s = tgms.open(tmp_path / "p", paranoid=True)
    try:
        s.ingest_events(EVENTS, nodes=PEOPLE + FORUMS)
        assert {v.uid for v in s.adapter.all_node_versions()} == {"p1", "p2", "f1"}
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 3. the collision rule — and why it is a soundness rule, not a taste rule
# ---------------------------------------------------------------------------

def test_a_collision_with_an_existing_believed_version_refuses(tmp_path):
    """Bulk load appends; it never supersedes. `assert_node` is what replaces a
    belief and `correct` is what revises one."""
    s = _open(tmp_path)
    try:
        s.ingest_events([], nodes=PEOPLE)
        with pytest.raises(InvalidArgError) as excinfo:
            s.ingest_events([], nodes=[dict(PEOPLE[0], props={"name": "Ada2"})])
        message = str(excinfo.value)
        assert "p1" in message
        assert "never supersedes" in message
        # and the refusal left the first load exactly as it was
        rows = {v.uid: v for v in s.adapter.all_node_versions()}
        assert rows["p1"].props == {"name": "Ada", "city": "London"}
        assert len([v for v in s.adapter.all_node_versions() if v.uid == "p1"]) == 1
    finally:
        s.close()


def test_a_collision_with_an_auto_created_endpoint_also_refuses(tmp_path):
    """The endpoint written by an earlier event batch is a believed version
    like any other, so re-describing that uid explicitly is still a collision."""
    s = _open(tmp_path)
    try:
        s.ingest_events(EVENTS)
        with pytest.raises(InvalidArgError):
            s.ingest_events([], nodes=PEOPLE)
    finally:
        s.close()


def test_a_uid_stated_twice_in_one_array_refuses(tmp_path):
    """Not caught by the believed-version check — nothing is written yet — and
    it would insert two overlapping versions of one identity."""
    s = _open(tmp_path)
    try:
        with pytest.raises(InvalidArgError) as excinfo:
            s.ingest_events([], nodes=[PEOPLE[0], dict(PEOPLE[0], vt_s=99)])
        assert "twice" in str(excinfo.value)
    finally:
        s.close()


def test_the_refusal_names_a_bounded_number_of_colliding_uids(tmp_path):
    """A million-node load that collides must not print a million uids."""
    s = _open(tmp_path)
    try:
        many = [{"uid": f"n{i}", "label": "L", "vt_s": 0} for i in range(40)]
        s.ingest_events([], nodes=many)
        with pytest.raises(InvalidArgError) as excinfo:
            s.ingest_events([], nodes=many)
        assert "..." in str(excinfo.value)
        assert str(excinfo.value).count("n") < 200
    finally:
        s.close()


def test_the_collision_rule_is_what_keeps_the_op_class_a(tmp_path):
    """The link stated as a test rather than a comment: because an explicit
    node ingest can never supersede, no version is ever carved, so the
    footprint emits **no carve arm** — which is what D13.21a requires of a
    Class A op, and what would be unsound if collisions were allowed to
    overwrite."""
    op = make_op("ingest_events", events=EVENTS, nodes=PEOPLE, offset=0)
    arms = footprints_of_op(op, 0)
    assert all(f.arm == "value" for f in arms), "a carve arm appeared"
    assert {f.cls for f in arms} == {"A"}


# ---------------------------------------------------------------------------
# 4. the freshness footprint covers what the op writes
# ---------------------------------------------------------------------------

def test_the_node_arm_reaches_every_explicitly_ingested_uid(tmp_path):
    op = make_op("ingest_events", events=EVENTS, nodes=PEOPLE + FORUMS, offset=0)
    node_arm = next(f for f in footprints_of_op(op, 0) if f.entity_kind == "node")
    assert set(node_arm.identity.uid) >= {"p1", "p2", "f1"}


def test_the_node_arm_hull_reaches_below_the_earliest_event(tmp_path):
    """An explicit node may start before anything the event stream mentions;
    a hull taken from events alone would miss it."""
    op = make_op("ingest_events", nodes=[{"uid": "old", "label": "L", "vt_s": 1}],
                 events=[{"src": "a", "dst": "b", "rel_type": "R", "vt_s": 9000}],
                 offset=0)
    node_arm = next(f for f in footprints_of_op(op, 0) if f.entity_kind == "node")
    assert node_arm.vt[0][0] == 1


def test_the_node_arm_widens_props_to_star_when_nodes_are_explicit(tmp_path):
    """An explicit record sets arbitrary property keys, so the value-arm props
    must be `"*"` — `{@identity, @extent, @event_key}` would miss a scope
    narrowed to `name`, which is a false negative."""
    plain = make_op("ingest_events", events=EVENTS, offset=0)
    structured = make_op("ingest_events", events=EVENTS, nodes=PEOPLE, offset=0)

    plain_node = next(f for f in footprints_of_op(plain, 0)
                      if f.entity_kind == "node")
    struct_node = next(f for f in footprints_of_op(structured, 0)
                       if f.entity_kind == "node")
    assert set(plain_node.props) == {"@identity", "@extent", "@event_key"}
    assert struct_node.props is TOP


def test_a_scope_narrowed_to_an_ingested_property_is_invalidated(tmp_path):
    """The end-to-end statement of the previous test: a reader whose scope
    names the property this load writes must be told, or the mechanism is
    unsound for structured bulk loads."""
    from tgms.tgir.check import check
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    s = _open(tmp_path)
    try:
        s.ingest_events([], nodes=[{"uid": "seed", "label": "L", "vt_s": 0}])
        log = s.eventlog
        ident = store_identity(log.header(), log.first_batch())
        tt_q = s.frontier_tt()
        s.ingest_events([], nodes=PEOPLE)

        term = ScopeTerm(targets=Targets(nodes=("p1",)), props=("name",))
        verdict = check(DependencyScope(store=ident, tt_q=tt_q, terms=(term,)), log)
        assert verdict.state == "possibly-stale", (
            "a scope naming the ingested property was not invalidated")
        assert verdict.witnesses[0].kind == "ingest_events"
    finally:
        s.close()


def test_the_edge_arms_eid_set_is_unaffected_by_the_nodes_field(tmp_path):
    """Erratum E-1's widening must survive the change untouched."""
    plain = footprints_of_op(make_op("ingest_events", events=EVENTS, offset=0), 0)
    structured = footprints_of_op(
        make_op("ingest_events", events=EVENTS, nodes=PEOPLE, offset=0), 0)
    plain_edge = next(f for f in plain if f.entity_kind == "edge")
    struct_edge = next(f for f in structured if f.entity_kind == "edge")
    assert plain_edge.identity.eid == struct_edge.identity.eid
    assert plain_edge.vt == struct_edge.vt
    assert plain_edge.identity.src == struct_edge.identity.src


def test_a_nodes_only_op_emits_a_node_arm_and_no_edge_arm(tmp_path):
    arms = footprints_of_op(
        make_op("ingest_events", events=[], nodes=PEOPLE, offset=0), 0)
    assert [f.entity_kind for f in arms] == ["node"]


def test_an_op_with_neither_events_nor_nodes_still_describes_nothing(tmp_path):
    assert footprints_of_op(make_op("ingest_events", events=[], offset=0), 0) == ()


# ---------------------------------------------------------------------------
# 5. replay
# ---------------------------------------------------------------------------

def test_a_log_carrying_the_new_field_replays_to_an_identical_store(tmp_path):
    """The event log is the stable format (STABILITY §1): a structured load
    must rebuild byte-identically from its own log."""
    src = _open(tmp_path, "src")
    try:
        src.ingest_events(EVENTS, nodes=PEOPLE + FORUMS)
        want = src.digest()
        log_path = src.path / "eventlog.jsonl"
        raw = log_path.read_bytes()
    finally:
        src.close()

    dst_dir = tmp_path / "replayed"
    dst_dir.mkdir()
    (dst_dir / "eventlog.jsonl").write_bytes(raw)
    dst = tgms.open(dst_dir)
    try:
        from tgms.storage.eventlog import replay
        replay(dst_dir / "eventlog.jsonl", dst.adapter)
        assert dst.digest() == want
    finally:
        dst.close()


def test_the_nodes_field_survives_the_canonical_json_round_trip(tmp_path):
    """`make_op` round-trips through canonical JSON before the op is logged,
    so anything the field carries has to be JSON-shaped."""
    s = _open(tmp_path)
    try:
        s.ingest_events([], nodes=PEOPLE + FORUMS)
        logged = [op for b in s.eventlog.batches() for op in b["ops"]]
        assert len(logged) == 1
        assert logged[0]["nodes"] == json.loads(json.dumps(PEOPLE + FORUMS))
    finally:
        s.close()


def test_an_old_reader_silently_ignores_the_new_field(tmp_path):
    """**Forward-compatibility, stated honestly rather than hoped for.**

    The appliers read named keys and ignore the rest, so a tgms predating this
    change replays a log carrying `nodes` **without error and without the
    nodes** — silent divergence, not a loud failure. This test pins the
    mechanism by simulating the old reader (an op with the field stripped) and
    showing the two stores differ, so the STABILITY §1 note stays true to the
    code.
    """
    new_op = make_op("ingest_events", events=EVENTS, nodes=PEOPLE, offset=0)
    old_op = {k: v for k, v in new_op.items() if k != "nodes"}

    new_store, old_store = _open(tmp_path, "new"), _open(tmp_path, "old")
    try:
        new_store._write([new_op])
        old_store._write([old_op])          # what an old applier would do
        assert _content(new_store) != _content(old_store)
        old_rows = {v.uid: v for v in old_store.adapter.all_node_versions()}
        assert {v.label for v in old_rows.values()} == {"Node"}, (
            "the old reader kept labels it could not have known")
    finally:
        new_store.close()
        old_store.close()


def test_the_batch_count_is_one_per_chunk_not_one_per_node(tmp_path):
    """The whole point: N nodes cost O(N/chunk) batches, and a batch is what
    carries the manifest whose cost grows with the store."""
    s = _open(tmp_path)
    try:
        s.ingest_events([], nodes=[{"uid": f"n{i}", "label": "L", "vt_s": 0}
                                   for i in range(5000)])
        assert len(list(EventLog(s.path / "eventlog.jsonl").batches())) == 1
    finally:
        s.close()
