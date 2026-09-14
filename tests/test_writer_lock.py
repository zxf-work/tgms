"""[tests] The OS-level single-writer lock (Lane B5/F2): `Store.__init__`
with `read_only=False` takes `fcntl.flock(LOCK_EX | LOCK_NB)` on
`<store>/writer.lock`, so a second concurrent writer fails fast and by name
instead of racing `_recover`/`_write` silently (the failure mode
`docs/eval_concurrency.md` §19 already documents for a reader opening
mid-commit — this closes the same class of hole for a second writer).
Readers never take the lock and are unaffected either way.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time

import pytest

import tgms
from tgms.store import WriterLockedError

pytest.importorskip("tgms._engine", reason="native engine extension not built")


def test_writer_lock_file_is_created_and_names_the_holder(tmp_path):
    store = tgms.open(tmp_path / "s", backend="native")
    lock_path = tmp_path / "s" / "writer.lock"
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())
    store.close()


def test_second_writer_in_same_process_fails_fast(tmp_path):
    """`flock` locks are per open file description, not per process: a
    second `Store(read_only=False)` on the same path — even from the same
    process — is refused exactly like a second process would be, which is
    the conservative (correct) reading of "one writer"."""
    path = tmp_path / "s"
    w1 = tgms.open(path, backend="native")
    try:
        with pytest.raises(WriterLockedError) as excinfo:
            tgms.open(path, backend="native")
        assert str(os.getpid()) in str(excinfo.value)
        assert "writer.lock" in str(excinfo.value) or str(path) in str(excinfo.value)
    finally:
        w1.close()

    # released on close: a fresh writer now succeeds
    w2 = tgms.open(path, backend="native")
    w2.close()


def test_readers_are_unaffected_by_a_live_writer(tmp_path):
    path = tmp_path / "s"
    w = tgms.open(path, backend="native")
    w.assert_node("n0", "N")
    try:
        # readers never call `_acquire_writer_lock`; any number may open
        # concurrently with the live writer.
        readers = [tgms.open(path, backend="native", read_only=True)
                  for _ in range(5)]
        for r in readers:
            assert r.adapter.generation == w.adapter.generation
            r.close()
    finally:
        w.close()


def test_readers_hold_no_writer_lock_themselves(tmp_path):
    """A reader opening first must not block a writer from opening after
    it — the lock is writer-only, both ways."""
    path = tmp_path / "s"
    w0 = tgms.open(path, backend="native")
    w0.close()

    r = tgms.open(path, backend="native", read_only=True)
    w = tgms.open(path, backend="native")  # must not raise
    w.close()
    r.close()


def _hold_writer(path: str, ready: "mp.synchronize.Event",
                 release: "mp.synchronize.Event") -> None:
    import tgms as _tgms
    store = _tgms.open(path, backend="native")
    ready.set()
    release.wait(timeout=30)
    store.close()


def test_two_writer_processes_cannot_coexist(tmp_path):
    """The literal claim: two OS *processes* each trying to be the writer
    for the same store — the second fails fast, naming the first's pid."""
    path = str(tmp_path / "s")
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_writer, args=(path, ready, release))
    holder.start()
    try:
        assert ready.wait(timeout=30), "holder process never opened as writer"
        # give the OS a moment to publish the flock before we contend for it
        time.sleep(0.2)
        with pytest.raises(WriterLockedError) as excinfo:
            tgms.open(path, backend="native")
        assert str(holder.pid) in str(excinfo.value)
    finally:
        release.set()
        holder.join(timeout=30)

    # the holder exited (closed, or the OS reclaimed the flock on process
    # death either way) -- a fresh writer now succeeds.
    w = tgms.open(path, backend="native")
    w.close()
