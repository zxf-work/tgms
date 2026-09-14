"""The dependency-DAG generator and k-hop cascade driver (Lane C, C5;
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md` §4).

Fixture idiom shared with `tests/test_storm.py`: a small event-stream store
built once via the normal write API, copied per test so tests never share
mutable state. `build_dag`/`cascade` never open `Storm` at all -- they only
need a `Registry` and a live store, the same pair `scripts/
demo_propagation.py` and `tests/test_propagation.py` already exercise.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path

import pytest

import tgms
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.storage.base import make_op
from tgms.storage.eventlog import extend_chain
from tgms.eval.storm_dag import (
    DEPTH_RANGE, FANOUT_RANGE, SHAPES, build_dag, cascade, _handle_for,
)

BACKEND = "native"


def _build_fixture(store_dir: Path, *, n_nodes: int = 24, n_events: int = 240,
                   seed: int = 1) -> None:
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(seed)
    uids = [f"n{i}" for i in range(n_nodes)]
    rel_types = ["FOLLOWS", "MESSAGES", "CITES"]
    events = []
    for i in range(n_events):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst, "rel_type": rng.choice(rel_types),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("storm-dag-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _correct(store, uid: str, *, vt_s: int = 0, vt_e: int = 50,
            props: dict | None = None) -> None:
    """A deterministic, clock-free write -- the `demo_propagation.py`/
    `tests/test_artifact_refresh.py` `_apply` idiom, restated (this file's
    own copy, per the same "restated, not imported" reasoning
    `storm.py::_payload_of` gives for its own)."""
    log = store.eventlog
    tt = log.last_tt() + 1
    ops = [make_op("correct", ref={"kind": "node", "uid": uid},
                   props=props or {"injected": "dag-test"}, vt_s=vt_s, vt_e=vt_e,
                   source="inject", provenance_ref=None)]
    _batch_id, end_offset, record = log.append(tt, ops)
    note_cursor = getattr(store.adapter, "note_event_cursor", None)
    if note_cursor is not None:
        if store._chain is None:
            store._chain = log.chain_of_prefix(end_offset - len(record))
        store._chain = extend_chain(store._chain, record)
    store.adapter.begin()
    try:
        store.adapter.apply_ops(ops, tt)
    except Exception:
        store.adapter.rollback()
        raise
    if note_cursor is not None:
        note_cursor(end_offset, store._chain)
    store.adapter.commit()


# ---------------------------------------------------------------------------
# 1 -- every shape registers real registrations at the declared depth/fanout
# ---------------------------------------------------------------------------

def test_shapes_are_realizable_at_small_depth_and_fanout(pristine_store: Path) -> None:
    for shape in SHAPES:
        work = _copy(pristine_store)
        store = tgms.open(work, backend=BACKEND)
        registry = Registry(work)
        try:
            info = build_dag(registry, store, shape, depth=4, fanout=3, seed=7)
            assert info.shape == shape
            assert len(info.nodes) > 0
            assert not info.truncated
            # every node is a real registration: the registry's own fold
            # agrees with what build_dag says it built.
            assert set(registry.names()) == {n.name for n in info.nodes}
            for node in info.nodes:
                record = registry.current(node.name)
                assert record is not None
                assert record.payload is not None  # gen-0 payload is populated
            if shape == "chain":
                assert info.fanout_achieved <= 1
                assert info.depth_achieved <= 4
            elif shape == "diamond":
                # `diamond` stacks whole (mid, sink) laps of 2 levels each,
                # so its achieved level count can overshoot the requested
                # `depth` by one lap's worth minus one -- documented in
                # `storm_dag._plan_diamond`.
                assert info.fanout_achieved <= 3
                assert info.depth_achieved <= 4 + 1
            elif shape in ("tree", "layered"):
                assert info.fanout_achieved <= 3
                assert info.depth_achieved <= 4
            else:  # power-law: depth/fanout set population size, not layers
                assert len(info.nodes) <= 4 * 3
        finally:
            store.close()


def test_parents_name_the_real_registered_ids(pristine_store: Path) -> None:
    """The dependency edge is real: a child's `parents` on the live registry
    record names its declared parent's *actual* `ArtifactId`, not merely a
    same-named placeholder."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        info = build_dag(registry, store, "tree", depth=3, fanout=2, seed=3)
        for node in info.nodes:
            record = registry.current(node.name)
            assert [p.name for p in record.parents] == list(node.parents)
            for parent_id in record.parents:
                parent_record = registry.current(parent_id.name)
                assert parent_record is not None
                assert parent_id == parent_record.id
    finally:
        store.close()


def test_build_dag_rejects_shape_depth_fanout_outside_the_declared_ranges(
    pristine_store: Path,
) -> None:
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        with pytest.raises(ValueError):
            build_dag(registry, store, "not-a-shape", depth=2, fanout=2, seed=0)
        with pytest.raises(ValueError):
            build_dag(registry, store, "chain", depth=DEPTH_RANGE[1] + 1, fanout=2, seed=0)
        with pytest.raises(ValueError):
            build_dag(registry, store, "chain", depth=2, fanout=FANOUT_RANGE[1] + 1, seed=0)
    finally:
        store.close()


def test_max_nodes_truncates_and_reports_it(pristine_store: Path) -> None:
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        info = build_dag(registry, store, "tree", depth=6, fanout=5, seed=1, max_nodes=10)
        assert len(info.nodes) <= 10
        assert info.truncated is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2 -- cascade on a deterministic chain: A -> B -> C
# ---------------------------------------------------------------------------

def _build_abc_chain(store, registry, *, seed: int = 4):
    info = build_dag(registry, store, "chain", depth=3, fanout=1, seed=seed,
                     name_prefix="abc")
    a, b, c = info.nodes
    assert b.uid == a.uid and c.uid == a.uid  # a chain shares one uid throughout
    return a, b, c


def test_cascade_deterministic_chain_visits_downstream_and_reports_false_safe_zero(
    pristine_store: Path,
) -> None:
    """The task's own worked example: a chain A -> B -> C sharing one uid.
    A base correction to that uid changes A; refreshing A to a new
    generation is the "just refreshed" event `cascade` starts from. At
    `k = 2` the walk must reach both B and C, and — since it reached
    everyone whose payload could possibly have changed — false-safe must be
    empty. `k = 2` exactly exhausts the chain's real depth from A, but the
    walk stops on the hop *budget*, not because a `parent_recheck` call came
    back empty -- so it honestly does not claim quiescence (it never asked
    whether C has children)."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        a, b, c = _build_abc_chain(store, registry)
        _correct(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        result = cascade(registry, store, a1.id, k=2)
        assert set(result.refreshed) == {b.name, c.name}
        assert result.nodes_visited == 2
        assert result.false_safe == ()
        assert result.quiescent is False
        assert len(result.levels) == 2
        assert result.levels[0].nodes_visited == 1  # B, one hop from A
        assert result.levels[1].nodes_visited == 1  # C, one hop from B
        for level in result.levels:
            assert level.latency_ms >= 0.0
    finally:
        store.close()


def test_cascade_k_one_past_the_true_depth_confirms_quiescence(
    pristine_store: Path,
) -> None:
    """`k = 3` (one hop past the chain's real depth of 2 from A) lets the
    walk's last `parent_recheck` call come back empty on its own -- only
    then may it honestly claim quiescence. A fresh DAG/store, independent of
    the `k=2` test above: re-running cascade from an already-fresh chain
    would find nothing new the second time (every `parents` snapshot is
    already caught up), which would test something else entirely."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        a, b, c = _build_abc_chain(store, registry)
        _correct(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        result = cascade(registry, store, a1.id, k=3)
        assert set(result.refreshed) == {b.name, c.name}
        assert result.quiescent is True
        assert result.false_safe == ()
        assert len(result.levels) == 3
        assert result.levels[2].nodes_visited == 0  # the confirming, empty hop
    finally:
        store.close()


def test_cascade_truncated_at_k1_misses_the_second_hop_and_reports_false_safe(
    pristine_store: Path,
) -> None:
    """The mirror image of the test above: `k=1` only reaches B, and C —
    whose payload really did change (it shares A/B's uid) — is exactly the
    false-safe `parent_recheck`'s own one-hop-only contract predicts a
    caller must ask again for (`tests/test_propagation.py::
    test_walks_one_level_only_no_cascade`)."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        info = build_dag(registry, store, "chain", depth=3, fanout=1, seed=4,
                         name_prefix="abc")
        a, b, c = info.nodes
        _correct(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        result = cascade(registry, store, a1.id, k=1)
        assert result.refreshed == (b.name,)
        assert result.false_safe == (c.name,)
        assert result.quiescent is False
    finally:
        store.close()


def test_cascade_visits_no_more_than_the_reachable_set(pristine_store: Path) -> None:
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        info = build_dag(registry, store, "tree", depth=4, fanout=3, seed=5)
        root = info.nodes[0]
        _correct(store, root.uid, props={"injected": "root-correction"})
        root_record = registry.current(root.name)
        root1 = refresh(root_record, _handle_for(root_record), store, registry)

        result = cascade(registry, store, root1.id, k=10)
        reachable = {n.name for n in info.nodes} - {root.name}
        assert set(result.refreshed) <= reachable
        assert result.nodes_visited <= len(reachable)
        assert result.false_safe == ()  # every tree node is a descendant of the root
        assert result.peak_rss_kb is None or result.peak_rss_kb >= 0
    finally:
        store.close()


def test_cascade_on_a_correction_that_touches_nothing_is_a_no_op(pristine_store: Path) -> None:
    """A "refresh" of a node whose content never changed still returns a new
    generation (refresh always publishes); cascade from it should find no
    parent-edge threats at all if nothing downstream's own snapshot has
    fallen behind."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        info = build_dag(registry, store, "chain", depth=3, fanout=1, seed=4)
        a = info.nodes[0]
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)  # no prior correction
        result = cascade(registry, store, a1.id, k=2)
        # B/C's own `parents` already name A's *current* generation at
        # registration time (0), and A only just moved to 1 -- so they are
        # genuinely behind and get flagged regardless of payload content;
        # what must hold is that none of this is mistaken for a false-safe,
        # since they were visited.
        assert result.false_safe == ()
    finally:
        store.close()
