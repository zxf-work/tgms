"""Product-side durability-injection hooks (D-086 P0.7).

`scripts/eval_durability.py`'s Python boundaries used to be harness-local
monkeypatches of `EventLog.append`/`adapter.commit`. They are now real
`crash_point(name)` call sites in `tgms/storage/eventlog.py::append` (torn
mid-append) and `tgms/store.py::_write_locked` (after WAL fsync, before
engine commit) — the same env-var protocol the native engine's own
`crash_point` uses (see `crates/tgms-engine-core/src/store.rs`), so a single
`TGMS_CRASH_POINT` selects an engine or a Python boundary identically.

These tests exercise the hooks directly with a real killed subprocess (not
just the harness), the same shape `scripts/eval_durability.py` uses: a child
writes one acknowledged batch, arms `TGMS_CRASH_POINT`, and dies mid write;
the parent reopens and checks recovery.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tgms

pytest.importorskip("tgms._engine", reason="native engine extension not built")

#: The crash write only starts after the seed write returns, and
#: TGMS_CRASH_POINT is armed only then — a torn/killed *seed* write would
#: make this test exercise the wrong batch.
_CHILD = """
import os
import sys

import tgms

path, boundary = sys.argv[1], sys.argv[2]
store = tgms.open(path, backend="native")
store.assert_node("seed", "N", {"i": 0}, vt_s=0, vt_e=100)
os.environ["TGMS_CRASH_POINT"] = boundary
store.assert_node("crash", "N", {"i": 999}, vt_s=0, vt_e=100)
print("UNREACHABLE: crash_point did not fire", flush=True)
"""


def _run_child(tmp_path: Path, store_path: Path, boundary: str) -> subprocess.CompletedProcess:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    env = dict(os.environ)
    # PYTHONPATH so the subprocess sees this editable checkout, not whatever
    # tgms (if any) is installed on the interpreter (test_concurrency.py's
    # DuckDB subprocess test uses the same fix for the same reason).
    env["PYTHONPATH"] = str(Path(tgms.__file__).resolve().parent.parent) + \
        os.pathsep + env.get("PYTHONPATH", "")
    env["TGMS_CRASH_POINT"] = ""  # armed inside the child, not inherited
    return subprocess.run(
        [sys.executable, str(script), str(store_path), boundary],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_py_after_wal_fsync_recovers_by_suffix_replay(tmp_path):
    """A crash right after the WAL fsync, before `apply_ops`, leaves the
    batch durable in the log but unapplied to the backend — write-ahead
    recovery must resurrect it by suffix replay, and verify() stays clean."""
    store_path = tmp_path / "s"
    proc = _run_child(tmp_path, store_path, "py_after_wal_fsync")
    assert proc.returncode == 137, (
        f"child did not crash at the hook: exit={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

    store = tgms.open(store_path, backend="native")
    seed = store.adapter.believed_node_versions("seed")
    assert len(seed) == 1 and seed[0].props["i"] == 0
    crash = store.adapter.believed_node_versions("crash")
    assert len(crash) == 1 and crash[0].props["i"] == 999, (
        "the WAL-fsynced batch must survive recovery even though the "
        f"caller never saw success: got {crash}")
    v = store.adapter.verify()
    assert not v.get("problems"), v["problems"]
    store.close()


def test_py_before_engine_commit_recovers_by_suffix_replay(tmp_path):
    """A crash after `apply_ops` but before the engine commit: nothing
    durable changed backend-side, but the WAL record is already fsynced, so
    recovery resurrects the batch the same way as `py_after_wal_fsync`."""
    store_path = tmp_path / "s"
    proc = _run_child(tmp_path, store_path, "py_before_engine_commit")
    assert proc.returncode == 137, (
        f"child did not crash at the hook: exit={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

    store = tgms.open(store_path, backend="native")
    crash = store.adapter.believed_node_versions("crash")
    assert len(crash) == 1 and crash[0].props["i"] == 999
    v = store.adapter.verify()
    assert not v.get("problems"), v["problems"]
    store.close()


def test_py_torn_wal_append_trims_and_recovers(tmp_path):
    """A crash mid-append leaves a torn final record with no closing
    newline. Recovery must trim it (`EventLog.trim_torn_tail`) rather than
    refuse to open, and the torn batch must NOT appear — `append` fsyncs and
    only then returns, so nothing was ever acknowledged (D-086)."""
    store_path = tmp_path / "s"
    proc = _run_child(tmp_path, store_path, "py_torn_wal_append")
    assert proc.returncode == 137, (
        f"child did not crash at the hook: exit={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

    log = store_path / "eventlog.jsonl"
    last_line = log.read_bytes().splitlines()[-1]
    assert last_line, "the log should still hold at least the header/seed records"

    store = tgms.open(store_path, backend="native")
    seed = store.adapter.believed_node_versions("seed")
    assert len(seed) == 1 and seed[0].props["i"] == 0
    crash = store.adapter.believed_node_versions("crash")
    assert not crash, f"a torn, un-acknowledged write must not survive: {crash}"
    v = store.adapter.verify()
    assert not v.get("problems"), v["problems"]
    # the store must remain writable and durable after the trim
    store.assert_node("post", "N", {"i": 1}, vt_s=0, vt_e=100)
    store.close()
    store = tgms.open(store_path, backend="native")
    assert store.adapter.believed_node_versions("post")[0].props["i"] == 1
    store.close()
