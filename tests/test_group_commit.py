"""Group commit must be invisible except in the number of generations.

`tgms.write.GroupCommitWriter` exists to make N concurrent single-row writers
cost one durable generation instead of N. Everything else about them has to
be unchanged, and "everything else" is the interesting part:

* the rows that land, and the store digest they produce;
* the durability promise — a submit returns only once its generation is on
  disk, so a store killed right after a submit still holds that row;
* the replay contract (D-042) — the log still rebuilds exactly this store;
* the failure contract — one caller's bad op fails one caller, not the group.

The last is the one a coalescing layer is most likely to get wrong, because
the batch it rolls back belongs to several callers.
"""

from __future__ import annotations

import threading

import pytest

import tgms
from tgms.core.errors import StateError, TgmsError
from tgms.storage.eventlog import replay
from tgms.write import GroupCommitWriter

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402


def _store(tmp_path, name="s"):
    return tgms.open(tmp_path / name, backend="native")


def _edges(store) -> set[tuple[str, str]]:
    return {(v.src, v.dst) for v in store.adapter.all_edge_versions()}


def test_concurrent_writers_land_every_row_in_fewer_generations(tmp_path):
    """The whole point, stated as a conservation law: nothing is lost, and
    the generation count falls below the submission count."""
    store = _store(tmp_path)
    store.assert_node("seed", "N")
    gen0 = store.adapter.generation
    writers, per = 8, 25

    with GroupCommitWriter(store) as gc:
        def one(w: int) -> None:
            for i in range(per):
                gc.assert_edge(f"a{w}_{i}", f"b{w}_{i}", "R", vt_s=w * per + i)

        threads = [threading.Thread(target=one, args=(w,)) for w in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = gc.stats()

    expected = {(f"a{w}_{i}", f"b{w}_{i}") for w in range(writers) for i in range(per)}
    assert _edges(store) == expected, "coalescing lost or invented rows"
    assert stats["submissions"] == writers * per
    used = store.adapter.generation - gen0
    assert used == stats["commits"]
    assert used < writers * per, (
        f"{used} generations for {writers * per} submissions is no coalescing")
    assert stats["max_group"] > 1, "no group ever held more than one submission"


def test_the_log_still_replays_to_the_same_store(tmp_path):
    """A coalesced batch is one log record; replay must still reproduce the
    store byte for byte (D-042)."""
    store = _store(tmp_path)
    with GroupCommitWriter(store) as gc:
        def one(w: int) -> None:
            for i in range(20):
                gc.assert_edge(f"u{w}", f"v{i}", "R", {"w": w},
                               vt_s=w * 100 + i, vt_e=w * 100 + i + 1)

        threads = [threading.Thread(target=one, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    digest = store.digest()
    store.close()

    fresh = NativeAdapter(tmp_path / "rebuilt")
    assert replay(tmp_path / "s" / "eventlog.jsonl", fresh) > 0
    assert fresh.store_digest() == digest
    fresh.close()


def test_a_submit_that_returned_is_durable(tmp_path):
    """The contract group commit must not weaken: a submit returns only after
    the generation containing it has published. Reopening from disk — with no
    help from the log, since recovery would hide the failure — must show it."""
    store = _store(tmp_path)
    with GroupCommitWriter(store) as gc:
        gc.assert_edge("durable", "row", "R", vt_s=1, vt_e=2)
        # no close, no flush, no cooperation from the writer: the row is
        # already on disk or the promise was a lie
        reader = tgms.open(tmp_path / "s", backend="native", read_only=True)
        assert ("durable", "row") in _edges(reader)
        reader.close()


def test_one_callers_bad_op_fails_only_that_caller(tmp_path):
    """The group rolls back as a unit, so the retry has to be per submission.

    A retraction of something that was never asserted is the cheapest
    reliable failure: it raises inside `apply_ops`, which rolls the whole
    batch back — including its innocent neighbours.
    """
    from tgms.storage.base import make_op

    store = _store(tmp_path)
    store.assert_node("real", "N", vt_s=0, vt_e=100)

    results: dict[str, object] = {}
    barrier = threading.Barrier(3)

    def good(name: str) -> None:
        barrier.wait()
        try:
            results[name] = gc.assert_edge(name, "dst", "R", vt_s=1, vt_e=2)
        except BaseException as e:  # noqa: BLE001 — the outcome is the result
            results[name] = e

    def bad() -> None:
        barrier.wait()
        try:
            results["bad"] = gc.submit(make_op(
                "retract", ref={"kind": "node", "uid": "never-existed"}, t=1,
                source="ingest", provenance_ref=None))
        except BaseException as e:  # noqa: BLE001
            results["bad"] = e

    with GroupCommitWriter(store, max_delay_s=0.05) as gc:
        threads = [threading.Thread(target=good, args=("g1",)),
                   threading.Thread(target=good, args=("g2",)),
                   threading.Thread(target=bad)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = gc.stats()

    # the test is only about anything if the bad op really did share a batch
    assert stats["solo_fallbacks"] > 0, (
        "the bad op never landed in a group, so the rollback path was not "
        "exercised: this test proved nothing")
    assert isinstance(results["bad"], TgmsError), (
        f"the bad op must be delivered to its own caller, got {results['bad']!r}")
    for name in ("g1", "g2"):
        assert isinstance(results[name], int), (
            f"{name} was failed by someone else's bad op: {results[name]!r}")
    landed = _edges(store)
    assert ("g1", "dst") in landed and ("g2", "dst") in landed

    # and the store still describes itself: the failed coalesced record and
    # the per-submission retries replay to exactly this
    digest = store.digest()
    store.close()
    fresh = NativeAdapter(tmp_path / "rebuilt")
    replay(tmp_path / "s" / "eventlog.jsonl", fresh)
    assert fresh.store_digest() == digest, (
        "a failed group left the log describing a different store")
    fresh.close()


def test_close_drains_what_it_accepted(tmp_path):
    store = _store(tmp_path)
    gc = GroupCommitWriter(store).start()
    for i in range(10):
        gc.assert_edge(f"c{i}", f"d{i}", "R", vt_s=i, vt_e=i + 1)
    gc.close()
    assert len(_edges(store)) == 10
    with pytest.raises(StateError):
        gc.assert_edge("after", "close", "R")


def test_a_read_only_store_cannot_be_coalesced_into(tmp_path):
    _store(tmp_path).close()
    ro = tgms.open(tmp_path / "s", backend="native", read_only=True)
    with pytest.raises(StateError):
        GroupCommitWriter(ro)
    ro.close()
