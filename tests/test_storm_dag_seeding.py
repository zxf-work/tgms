"""storm-v1 addendum-2's `--dag-seed-from-affected` mechanism (`tgms.eval.
storm_dag.cascade`'s new `extra_seeds` parameter).

Addendum-1's finding (confirmed by the coordinator, not a harness bug): a
declared-`parents`-edges cascade cannot reach an artifact that depends on
the corrected data only through its own query *footprint* — the registry-
wide false-safe oracle (`cascade`'s own docstring) is testing the right
thing, an edges-only walk just cannot answer it. Addendum-2 adds an
opt-in seeding mode: also start the walk from every artifact
`tgms.artifact.lookup.affected()` finds for the correction batch (the same
footprint-based pre-filter the `tgms-*`/`entity-touch`/`window-overlap`
storm arms already use), not just the corrected root.

Fixture idiom shared with `tests/test_storm_dag.py`: a small event-stream
store built once via the normal write API, copied per test so tests never
share mutable state.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path

import pytest

import tgms
from tgms.artifact.lookup import affected
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.storage.base import make_op
from tgms.storage.eventlog import extend_chain
from tgms.eval.storm_dag import _handle_for, _register_operator_artifact, build_dag, cascade

BACKEND = "native"


def _build_fixture(store_dir: Path, *, n_nodes: int = 24, n_events: int = 240,
                   seed: int = 1) -> None:
    """Restated verbatim from `tests/test_storm_dag.py::_build_fixture` (this
    file's own copy, the same "restated, not imported" reasoning that
    module's own docstring and `storm.py::_payload_of` give for theirs) —
    a small event-stream store built once via the normal write API, copied
    per test so tests never share mutable state."""
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
    base = tmp_path_factory.mktemp("storm-dag-seeding-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _build_abc_chain(store, registry, *, seed: int = 4):
    """Restated verbatim from `tests/test_storm_dag.py::_build_abc_chain`."""
    info = build_dag(registry, store, "chain", depth=3, fanout=1, seed=seed,
                     name_prefix="abc")
    a, b, c = info.nodes
    assert b.uid == a.uid and c.uid == a.uid  # a chain shares one uid throughout
    return a, b, c


def _correct_returning_batch(store, uid: str, *, vt_s: int = 0, vt_e: int = 50,
                             props: dict | None = None) -> dict:
    """`tests/test_storm_dag.py::_correct`'s own idiom, restated here only
    because this file's own tests need the logged batch dict itself (to
    call `affected(batch, registry)`, exactly as `scripts/
    bench_correction_storm.py::_correct_uid` now does for the real
    `--dag-seed-from-affected` path) -- `_correct` there discards it."""
    log = store.eventlog
    tt = log.last_tt() + 1
    ops = [make_op("correct", ref={"kind": "node", "uid": uid},
                   props=props or {"injected": "dag-seeding-test"}, vt_s=vt_s, vt_e=vt_e,
                   source="inject", provenance_ref=None)]
    batch_id, end_offset, record = log.append(tt, ops)
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
    return {"batch_id": batch_id, "tt": tt, "ops": ops}


def _build_chain_plus_footprint_only_artifact(store, registry, *, seed: int = 4):
    """A tiny DAG (the `abc` chain, A -> B -> C, one shared uid) plus one
    *non-DAG* artifact reading that same uid with `parents=()` -- no
    declared edge to the DAG at all, so `parent_recheck` from A can never
    reach it, but it shares A/B/C's uid in its own query footprint, so a
    correction to that uid changes it too."""
    a, b, c = _build_abc_chain(store, registry, seed=seed)
    extra = _register_operator_artifact(
        store, registry, "footprint-only-reader", "entity_history",
        {"uid": a.uid, "include_edges": True}, parents=())
    return a, b, c, extra


def test_without_the_flag_the_footprint_only_artifact_is_false_safe(
    pristine_store: Path,
) -> None:
    """v1 behaviour (no `extra_seeds` passed): the footprint-only artifact
    is never visited by the edges-only walk, and the registry-wide oracle
    correctly reports it as false-safe -- Addendum 1's finding, reproduced
    as a fast, deterministic unit test."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        a, b, c, extra = _build_chain_plus_footprint_only_artifact(store, registry)
        _correct_returning_batch(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        result = cascade(registry, store, a1.id, k=2)
        assert extra.name not in result.refreshed
        assert result.false_safe == (extra.name,)
        assert result.seeds == (a.name,)
        assert result.seeded_from == "root"
    finally:
        store.close()


def test_with_the_flag_the_footprint_only_artifact_is_seeded_and_refreshed(
    pristine_store: Path,
) -> None:
    """With `extra_seeds` populated from `affected()`'s own footprint-based
    answer for the correction batch (storm-v1 addendum-2's
    `--dag-seed-from-affected`, exactly as `scripts/
    bench_correction_storm.py` now wires it), the footprint-only artifact
    becomes a seed: it is refreshed, appears in `refreshed`/`seeds`, and is
    no longer false-safe.

    `affected()`'s footprint match is uid-based, not DAG-membership-based:
    B and C share A's uid too (the `abc` chain's own construction), so they
    are legitimate `affected()` survivors alongside the footprint-only
    artifact, not just an edges-only find -- `extra_seeds` and
    `result.seeds` include all three non-root names for exactly that
    reason. This is the honest behavior, not a test artifact: it is why
    Addendum 1's false-safe artifacts were never a DAG-structure question
    in the first place."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        a, b, c, extra = _build_chain_plus_footprint_only_artifact(store, registry)
        batch = _correct_returning_batch(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        lookup_result = affected(batch, registry)
        extra_seeds = [r for r in lookup_result.affected if r.name != a.name]
        assert extra.name in {r.name for r in extra_seeds}  # the pre-filter finds it
        assert {b.name, c.name}.issubset({r.name for r in extra_seeds})  # they share a.uid too

        result = cascade(registry, store, a1.id, k=2, extra_seeds=extra_seeds)
        assert extra.name in result.refreshed
        assert result.false_safe == ()
        assert set(result.seeds) == {a.name, b.name, c.name, extra.name}
        assert result.seeded_from == "affected"
        assert {b.name, c.name}.issubset(set(result.refreshed))
    finally:
        store.close()


def test_extra_seeds_empty_is_byte_identical_to_the_v1_call_shape(
    pristine_store: Path,
) -> None:
    """A caller that passes `extra_seeds=()` explicitly (the default) gets
    exactly what a v1 caller that never knew about the parameter got:
    `seeded_from == "root"`, `seeds == (seed_name,)`, and every other field
    computed by the same code path as `tests/test_storm_dag.py`'s own
    chain tests."""
    work = _copy(pristine_store)
    store = tgms.open(work, backend=BACKEND)
    registry = Registry(work)
    try:
        a, b, c = _build_abc_chain(store, registry)
        _correct_returning_batch(store, a.uid, props={"injected": "base-correction"})
        a_record = registry.current(a.name)
        a1 = refresh(a_record, _handle_for(a_record), store, registry)

        result = cascade(registry, store, a1.id, k=2, extra_seeds=())
        assert set(result.refreshed) == {b.name, c.name}
        assert result.false_safe == ()
        assert result.seeds == (a.name,)
        assert result.seeded_from == "root"
    finally:
        store.close()
