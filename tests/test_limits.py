"""[tests] Lane B5/F2 backpressure/limits: the bounded ingestion queue
(`tgms.write.GroupCommitWriter`) rejects at capacity rather than growing
unboundedly, and the service-surface caps (`tgms.tools.limits`) refuse a
call — never truncate a result — when a concurrency, row, or byte ceiling is
exceeded. Every refusal is a structured `E_LIMIT` payload with
`details.stage == "limit"`, distinct from a TGIR admission refusal's
`stage in {"plan", "node", "runtime"}` (`tgms.tgir.admission`).
"""

from __future__ import annotations

import threading
import time

import pytest

import tgms
from tgms.core.errors import LimitError
from tgms.tools.limits import ConcurrencyGate, Limits, check_result_limits
from tgms.tools.server import ToolRouter
from tgms.write import GroupCommitWriter, QueueFullError

pytest.importorskip("tgms._engine", reason="native engine extension not built")


def _store(tmp_path, name="s"):
    return tgms.open(tmp_path / name, backend="native")


# --------------------------------------------------------------------------- #
# 1. bounded ingestion queue                                                   #
# --------------------------------------------------------------------------- #

def test_default_queue_is_unbounded_like_before(tmp_path):
    """`max_queue=0` (the default) preserves today's behaviour exactly:
    `queue.Queue(maxsize=0)` never raises `queue.Full`."""
    store = _store(tmp_path)
    with GroupCommitWriter(store) as gc:
        assert gc.max_queue == 0
        assert gc._q.maxsize == 0
        for i in range(50):
            gc.assert_node(f"n{i}", "N")
    assert gc.rejected == 0


def test_bounded_queue_rejects_at_capacity_and_counts(tmp_path):
    """A queue at `max_queue` refuses a non-blocking `submit` immediately
    with `QueueFullError`, and the rejection is counted — the op that was
    refused never reaches the store."""
    store = _store(tmp_path)
    store.assert_node("seed", "N")

    committing = threading.Event()
    release = threading.Event()
    orig_write = store._write

    def slow_write(ops):
        committing.set()
        release.wait(timeout=10)
        return orig_write(ops)

    store._write = slow_write

    with GroupCommitWriter(store, max_queue=1) as gc:
        t_a = threading.Thread(target=lambda: gc.assert_node("a", "N"), daemon=True)
        t_a.start()
        # once the committer is inside the (now slow) commit for "a", the
        # queue itself is empty again -- "a" was already dequeued into the
        # group before `_commit_group` ever calls `store._write`.
        assert committing.wait(timeout=10), "committer never reached slow_write"
        deadline = time.time() + 10
        while gc._q.qsize() != 0 and time.time() < deadline:
            time.sleep(0.005)
        assert gc._q.qsize() == 0

        t_b = threading.Thread(target=lambda: gc.assert_node("b", "N"), daemon=True)
        t_b.start()
        deadline = time.time() + 10
        while gc._q.qsize() != 1 and time.time() < deadline:
            time.sleep(0.005)
        assert gc._q.qsize() == 1, "b's submission never filled the queue to capacity"

        # the queue is now at max_queue=1: a third, non-blocking submission
        # must refuse immediately, not wait, not silently drop.
        with pytest.raises(QueueFullError) as excinfo:
            gc.assert_node("c", "N")
        assert excinfo.value.code == "E_QUEUE_FULL"
        assert gc.rejected == 1

        release.set()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

    uids = {v.uid for v in store.adapter.all_node_versions()} \
        if hasattr(store.adapter, "all_node_versions") else None
    if uids is not None:
        assert {"seed", "a", "b"} <= uids
        assert "c" not in uids
    assert gc.rejected == 1
    assert gc.stats()["rejected"] == 1


def test_bounded_queue_submit_can_block_for_room(tmp_path):
    """`block=True` waits for room instead of refusing immediately, and
    with no artificial delay in the committer, a bounded queue this small
    still drains fast enough that many blocking submissions all succeed."""
    from tgms.core.model import OPEN_END
    from tgms.storage.base import make_op

    store = _store(tmp_path)
    with GroupCommitWriter(store, max_queue=1) as gc:
        for i in range(20):
            op = make_op("assert_node", uid=f"blocked{i}", label="N", props={},
                         vt_s=0, vt_e=OPEN_END, source="ingest", provenance_ref=None)
            tt = gc.submit(op, block=True, timeout=10)
            assert isinstance(tt, int)
    assert gc.rejected == 0


# --------------------------------------------------------------------------- #
# 2. Limits: structured refusal, never truncation                             #
# --------------------------------------------------------------------------- #

def _seeded_store(tmp_path):
    store = _store(tmp_path)
    store.assert_node("n0", "N")
    for i in range(5):
        store.assert_edge("n0", f"m{i}", "R", vt_s=i)
    return store


def test_limits_from_env(monkeypatch):
    monkeypatch.setenv("TGMS_MAX_ROWS", "10")
    monkeypatch.setenv("TGMS_MAX_BYTES", "1000")
    monkeypatch.setenv("TGMS_MAX_CONCURRENT", "4")
    monkeypatch.setenv("TGMS_MAX_WALL_S", "30")
    limits = Limits.from_env()
    assert limits == Limits(max_rows=10, max_bytes=1000, max_concurrent=4,
                            max_wall_s=30.0)


def test_limits_from_env_defaults_to_unenforced(monkeypatch):
    for name in ("TGMS_MAX_ROWS", "TGMS_MAX_BYTES", "TGMS_MAX_CONCURRENT",
                "TGMS_MAX_WALL_S"):
        monkeypatch.delenv(name, raising=False)
    assert Limits.from_env() == Limits()


def test_concurrency_gate_refuses_over_cap():
    gate = ConcurrencyGate(max_concurrent=1)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False, "a second slot must not be admitted"
    gate.release()
    assert gate.try_acquire() is True


def test_concurrency_gate_unbounded_when_none():
    gate = ConcurrencyGate(max_concurrent=None)
    for _ in range(1000):
        assert gate.try_acquire() is True


def test_check_result_limits_refuses_on_rows_never_truncates():
    envelope = {"op": "entity_history", "rows": [{"i": i} for i in range(5)],
               "rows_total": 5, "truncated": False}
    check_result_limits(envelope, Limits(max_rows=10), "entity_history")  # ok

    with pytest.raises(LimitError) as excinfo:
        check_result_limits(envelope, Limits(max_rows=3), "entity_history")
    assert excinfo.value.code == "E_LIMIT"
    assert excinfo.value.details["stage"] == "limit"
    assert excinfo.value.details["limit_kind"] == "rows"
    assert excinfo.value.details["observed"] == 5
    assert excinfo.value.details["ceiling"] == 3
    # the caller sees the exception, never a shrunken `rows` list: the
    # envelope this test built is untouched.
    assert len(envelope["rows"]) == 5


def test_check_result_limits_refuses_on_bytes():
    big_envelope = {"op": "x", "rows": ["y" * 100 for _ in range(50)]}
    with pytest.raises(LimitError) as excinfo:
        check_result_limits(big_envelope, Limits(max_bytes=100), "x")
    assert excinfo.value.details["stage"] == "limit"
    assert excinfo.value.details["limit_kind"] == "bytes"


def test_check_result_limits_noop_when_unset():
    envelope = {"rows": list(range(10_000))}
    check_result_limits(envelope, Limits(), "op")  # must not raise


def test_tool_router_refuses_concurrency_structured(tmp_path):
    store = _seeded_store(tmp_path)
    router = ToolRouter(store.adapter, tt_source=store,
                        limits=Limits(max_concurrent=1))
    assert router._gate.try_acquire() is True  # occupy the one slot
    try:
        env = router.call("entity_history", {"uid": "n0", "limit": 5})
        assert env["error"] == "E_LIMIT"
        assert env["details"]["stage"] == "limit"
        assert env["details"]["limit_kind"] == "concurrency"
        assert "request_id" in env["details"]
    finally:
        router._gate.release()

    # the slot is free again: a normal call succeeds and is not refused.
    ok = router.call("entity_history", {"uid": "n0", "limit": 5})
    assert "error" not in ok
    assert "request_id" in ok.get("annotations", {})


def test_tool_router_refuses_rows_structured_and_full_result_without_cap(tmp_path):
    store = _seeded_store(tmp_path)
    uncapped = ToolRouter(store.adapter, tt_source=store)
    full = uncapped.call("entity_history", {"uid": "n0", "limit": 50,
                                            "include_edges": True})
    assert "error" not in full
    assert len(full["rows"]) > 0

    capped = ToolRouter(store.adapter, tt_source=store,
                        limits=Limits(max_rows=0))
    refused = capped.call("entity_history", {"uid": "n0", "limit": 50,
                                             "include_edges": True})
    assert refused["error"] == "E_LIMIT"
    assert refused["details"]["stage"] == "limit"
    assert refused["details"]["limit_kind"] == "rows"


def test_tool_router_unlimited_by_default(tmp_path):
    """A bare `ToolRouter(adapter)` (every pre-B5 construction) enforces
    nothing -- the default must not change existing callers' behaviour."""
    store = _seeded_store(tmp_path)
    router = ToolRouter(store.adapter, tt_source=store)
    assert router.limits == Limits()
    env = router.call("entity_history", {"uid": "n0", "limit": 5})
    assert "error" not in env
