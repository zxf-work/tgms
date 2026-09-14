"""Crash-injection *inside* recovery itself (Lane A EXP-A2, D-086 follow-on).

`tests/test_crash_points.py` and `tests/test_suffix_replay.py` establish that
a crash during the *write* path recovers correctly and deterministically.
This file asks the next question: what if the crash happens *during
recovery's own replay of the un-applied suffix*? `Store._recover`
(`tgms/store.py`) carries four crash points on exactly that path
(`tgms/storage/crashpoint.py` protocol — a no-op unless
`TGMS_CRASH_POINT` names it):

  * `py_recover_after_trim` — the torn tail (if any) has been trimmed;
    nothing in the suffix has been replayed yet.
  * `py_recover_before_cursor_publish` — the first un-applied batch's
    `apply_ops` has staged its rows in the engine, but nothing about the
    cursor has been touched.
  * `py_recover_after_cursor_publish` — the cursor is staged
    (`note_event_cursor`, in-process memory only) but the atomic commit
    that would make batch-plus-cursor durable has not run.
  * `py_recover_mid_replay` — that commit has landed (batch and cursor
    durable together, in one manifest generation); the loop has not yet
    moved on to whatever the suffix holds next.

None of these are separable into "batch durable, cursor not yet" or the
reverse: the native engine stages the cursor in Python-process memory
(`NativeStore.set_event_cursor`) and only writes it — atomically, with the
replayed rows — in the single manifest swap `commit()` performs. So a crash
at either of the first three points leaves the on-disk manifest completely
untouched (recovery redoes the same batch from the same starting cursor
next time), and only `py_recover_mid_replay` observes a new generation.

The contract under test at every point: killing the process mid-recovery and
reopening (letting recovery finish, possibly after more than one restart)
must reach the exact same state a single, uninterrupted recovery would —
`verify()` clean, and the digest equal to a clean replay of the (now sound)
log into a fresh store. `tests/test_crash_during_recovery.py`'s sibling
`scripts/eval_durability.py --recovery-crash` mode stresses this across many
random restarts and boundaries; this file pins one deterministic subprocess
case per crash point, the same shape `tests/test_crash_points.py` uses for
the write-path points.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tgms

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.eventlog import replay  # noqa: E402
from tgms.storage.native import NativeAdapter  # noqa: E402

RECOVERY_CRASH_POINTS = [
    "py_recover_after_trim",
    "py_recover_before_cursor_publish",
    "py_recover_after_cursor_publish",
    "py_recover_mid_replay",
]

#: Builds a normal store with one acked batch, closes it, then appends a
#: *second* batch directly to the log — bypassing the adapter, exactly like
#: tests/test_suffix_replay.py's `test_unapplied_suffix_is_replayed_on_open`
#: — so the next open has a genuine un-applied suffix for recovery to
#: replay, and the named recovery crash point has real work to interrupt.
_CHILD = """
import os
import sys

import tgms
from tgms.storage.eventlog import EventLog

path, boundary = sys.argv[1], sys.argv[2]
store = tgms.open(path, backend="native")
store.assert_node("seed", "N", {"i": 0}, vt_s=0, vt_e=100)
tt = store.clock.last_tt
store.close()

EventLog(path + "/eventlog.jsonl").append(
    tt + 10, [{"op": "assert_node", "uid": "crash", "label": "N",
               "props": {"i": 999}, "vt_s": 0, "vt_e": 100,
               "source": "ingest", "provenance_ref": None}])

os.environ["TGMS_CRASH_POINT"] = boundary
tgms.open(path, backend="native")
print("UNREACHABLE: crash_point did not fire", flush=True)
"""

_TCSR_CHILD = """
import os
import sys

import tgms

path = sys.argv[1]
store = tgms.open(path, backend="native")
os.environ["TGMS_CRASH_POINT"] = "py_tcsr_mid_rebuild"
store.adapter.tcsr()
print("UNREACHABLE: crash_point did not fire", flush=True)
"""


def _run(tmp_path: Path, script: str, args: list[str]) -> subprocess.CompletedProcess:
    path = tmp_path / "child.py"
    path.write_text(script)
    env = dict(os.environ)
    # PYTHONPATH so the subprocess sees this editable checkout, not whatever
    # tgms (if any) is installed on the interpreter (matches
    # tests/test_crash_points.py's fix for the same reason).
    env["PYTHONPATH"] = str(Path(tgms.__file__).resolve().parent.parent) + \
        os.pathsep + env.get("PYTHONPATH", "")
    env["TGMS_CRASH_POINT"] = ""  # armed inside the child, not inherited
    return subprocess.run(
        [sys.executable, str(path), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def replay_reference_digest(root: Path, tmp_path: Path) -> str:
    """What a full replay of the (now-recovered, sound) log produces."""
    fresh = NativeAdapter(tmp_path / "replay-reference" / "native")
    replay(root / "eventlog.jsonl", fresh)
    digest = fresh.store_digest()
    fresh.close()
    return digest


@pytest.mark.parametrize("boundary", RECOVERY_CRASH_POINTS)
def test_crash_during_recovery_then_second_open_completes(tmp_path, boundary):
    store_path = tmp_path / "s"
    proc = _run(tmp_path, _CHILD, [str(store_path), boundary])
    assert proc.returncode == 137, (
        f"child did not crash at {boundary}: exit={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

    # a second, uninterrupted open must finish recovery (possibly picking up
    # exactly where the first attempt left off) without raising.
    store = tgms.open(store_path, backend="native")
    seed = store.adapter.believed_node_versions("seed")
    assert len(seed) == 1 and seed[0].props["i"] == 0
    crash = store.adapter.believed_node_versions("crash")
    assert len(crash) == 1 and crash[0].props["i"] == 999, (
        f"the un-applied batch must survive recovery interrupted at "
        f"{boundary}: got {crash}")

    v = store.adapter.verify()
    assert not v.get("problems"), v["problems"]

    d1 = store.digest()
    store.close()
    d2 = replay_reference_digest(store_path, tmp_path)
    assert d1 == d2, (
        f"recovery interrupted at {boundary} must converge to exactly what "
        f"a clean replay of the same log produces")

    # and the store stays writable afterwards
    reopened = tgms.open(store_path, backend="native")
    reopened.assert_node("post", "N", {"i": 2}, vt_s=0, vt_e=100)
    reopened.close()
    again = tgms.open(store_path, backend="native")
    assert again.adapter.believed_node_versions("post")[0].props["i"] == 2
    again.close()


def test_py_tcsr_mid_rebuild_crash_leaves_store_serving_correct_answers(tmp_path):
    """`py_tcsr_mid_rebuild` fires inside `save_permutation`, after the
    permutation is fully computed but before its generation/manifest_sha
    stamp is written (`tgms/storage/tcsr.py`). It cannot interrupt
    `Store._recover` — nothing about open()/recovery calls `adapter.tcsr()`
    — so unlike the four points above it gets its own scenario: a crash
    mid-save must leave no persisted (or a merely stale, never wrong)
    index, and the very next open must still answer traversal queries
    correctly by rebuilding live.
    """
    store_path = tmp_path / "s"
    store = tgms.open(store_path, backend="native")
    for i in range(6):
        store.assert_edge(f"n{i}", f"n{i + 1}", "R", {"w": i}, vt_s=i, vt_e=i + 10)
    store.close()

    idx_path = store_path / "native" / "index" / "tcsr.npz"
    assert not idx_path.exists(), "nothing should have built the index yet"

    proc = _run(tmp_path, _TCSR_CHILD, [str(store_path)])
    assert proc.returncode == 137, (
        f"child did not crash at py_tcsr_mid_rebuild: exit={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
    # the crash lands before `np.savez` ever opens the tmp file (tmp+rename
    # atomicity guards the rest): nothing was written at all.
    assert not idx_path.exists(), (
        "a crash before the stamp is written must leave no persisted index")

    # the store itself is untouched (tcsr() never opens a batch to compute
    # this) and the very next open must still serve correct traversal
    # answers, rebuilding the index live.
    reopened = tgms.open(store_path, backend="native")
    csr, _cols = reopened.adapter.tcsr()
    src_id = int(reopened.adapter.dense_ids(["n0"])[0])
    nbr, _vt_s, _vt_e, _row = csr.neighbors(src_id, direction="out")
    dst_uid = reopened.adapter.uids_for([int(nbr[0])])[0]
    assert len(nbr) == 1 and dst_uid == "n1", (
        f"TCSR must answer correctly after a crashed mid-rebuild save: "
        f"neighbors(n0) -> {dst_uid!r}")
    # and this open's own rebuild did persist successfully
    assert idx_path.exists()
    reopened.close()
