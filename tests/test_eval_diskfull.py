"""Unit tests for `scripts/eval_diskfull.py` (Lane A task A5).

Two layers: the injector's own mechanics (does it count and fire correctly,
in isolation — no product code involved), and the end-to-end child/reopen
path (does an injected ENOSPC during the one reachable call site,
`EventLog.append`'s `os.fsync`, surface as a clean typed error and leave a
store that still passes Q1-Q3 on reopen).
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from scripts import eval_diskfull as ed  # noqa: E402


@pytest.fixture()
def restore_os_patches():
    """`install_injector` patches `os.write`/`fsync`/`rename`/`replace` for
    the rest of the process — correct for the short-lived subprocess it is
    designed to run in, but a direct in-process call (to test the injector's
    mechanics without the cost of a subprocess) must restore them, or every
    later test in this process inherits a patched `os` module."""
    saved = (os.write, os.fsync, os.rename, os.replace)
    yield
    os.write, os.fsync, os.rename, os.replace = saved


def test_injector_fires_enospc_at_the_counted_fsync_call(tmp_path, monkeypatch, restore_os_patches):
    monkeypatch.setenv("TGMS_DISKFULL_AT", "2")
    monkeypatch.delenv("TGMS_SHORT_WRITE_AT", raising=False)
    status = tmp_path / "status.json"
    ed.install_injector(status)

    fd = os.open(str(tmp_path / "f"), os.O_RDWR | os.O_CREAT)
    try:
        os.fsync(fd)  # call #1: passthrough
        with pytest.raises(OSError) as ei:
            os.fsync(fd)  # call #2: injected
        assert ei.value.errno == errno.ENOSPC
    finally:
        os.close(fd)

    data = json.loads(status.read_text())
    assert data["fired_at"] == 2
    assert data["fired_kind"] == "enospc@os.fsync"


def test_injector_fires_enospc_at_the_counted_replace_call(tmp_path, monkeypatch, restore_os_patches):
    """`os.replace` is the one other real call site in this codebase
    (`tgms/storage/tcsr.py::save_permutation`) — confirmed reachable by the
    injector independent of whether a given trial's workload ever exercises
    it."""
    monkeypatch.setenv("TGMS_DISKFULL_AT", "1")
    monkeypatch.delenv("TGMS_SHORT_WRITE_AT", raising=False)
    status = tmp_path / "status.json"
    ed.install_injector(status)

    src = tmp_path / "a"
    dst = tmp_path / "b"
    src.write_text("x")
    with pytest.raises(OSError) as ei:
        os.replace(str(src), str(dst))
    assert ei.value.errno == errno.ENOSPC
    assert src.exists() and not dst.exists(), "the injected call must never actually run"

    data = json.loads(status.read_text())
    assert data["fired_kind"] == "enospc@os.replace"


def test_injector_short_write_returns_partial_count_once(tmp_path, monkeypatch, restore_os_patches):
    monkeypatch.delenv("TGMS_DISKFULL_AT", raising=False)
    monkeypatch.setenv("TGMS_SHORT_WRITE_AT", "1")
    status = tmp_path / "status.json"
    ed.install_injector(status)

    fd = os.open(str(tmp_path / "f"), os.O_RDWR | os.O_CREAT)
    try:
        n1 = os.write(fd, b"0123456789")
        assert 0 < n1 < 10, "the counted call must return fewer bytes than requested"
        n2 = os.write(fd, b"0123456789")
        assert n2 == 10, "the short write must fire only once"
    finally:
        os.close(fd)

    data = json.loads(status.read_text())
    assert data["fired_kind"] == "short_write@os.write"


def test_uninstalled_injector_is_a_no_op(tmp_path, monkeypatch, restore_os_patches):
    monkeypatch.delenv("TGMS_DISKFULL_AT", raising=False)
    monkeypatch.delenv("TGMS_SHORT_WRITE_AT", raising=False)
    before = (os.write, os.fsync, os.rename, os.replace)
    ed.install_injector(tmp_path / "status.json")
    assert (os.write, os.fsync, os.rename, os.replace) == before, (
        "with neither env var set, install_injector must not touch os.*")


def test_diskfull_at_first_fsync_is_a_clean_error_and_recovers(tmp_path):
    """End to end, via the real subprocess entry point
    (`eval_diskfull.py --child`): TGMS_DISKFULL_AT=1 hits the very first
    `EventLog.append` fsync (the seed write `a0`), which must surface as a
    clean typed OSError (exit code 2, never a hang), and the reopened store
    — recovery replays the unapplied suffix, since the record's bytes were
    already `write()`+`flush()`ed to the real filesystem before the
    injected fsync failure, D-086's write-ahead design working exactly as
    it does for the process-kill boundaries in eval_durability.py — must
    still pass Q1 and Q3, with Q2 (clean-replay determinism) holding too."""
    store_dir = tmp_path / "store"
    acked = tmp_path / "acked.txt"
    acked.touch()
    status = tmp_path / "status.json"
    env = {**os.environ, "TGMS_DISKFULL_AT": "1"}
    env.pop("TGMS_SHORT_WRITE_AT", None)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_diskfull.py"), "--child",
         str(store_dir), "777", str(acked), str(status)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

    data = json.loads(status.read_text())
    assert data["fired_kind"] == "enospc@os.fsync"
    assert data["fired_at"] == 1
    assert "ENOSPC" in data["error"]

    fields, problems = ed._q1_q3_q4(store_dir, acked)
    assert fields["q1_acked_survive"], problems
    assert fields["q3_single_generation"], problems
    d2 = ed.clean_replay_digest(store_dir / "eventlog.jsonl")
    assert fields["digest"] == d2, "recovered digest must equal a clean replay of its own log"


def test_diskfull_never_reached_is_a_harmless_no_fire_trial(tmp_path):
    """A trial whose injection point exceeds the total number of calls the
    workload makes must run to completion normally — not an error, just an
    uninteresting trial for the disk-full contract."""
    store_dir = tmp_path / "store"
    acked = tmp_path / "acked.txt"
    acked.touch()
    status = tmp_path / "status.json"
    env = {**os.environ, "TGMS_DISKFULL_AT": "999999"}
    env.pop("TGMS_SHORT_WRITE_AT", None)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_diskfull.py"), "--child",
         str(store_dir), "778", str(acked), str(status)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    data = json.loads(status.read_text())
    assert data["fired_at"] is None
    assert acked.read_text().strip() != "", "the seed write should have been acked"


def test_run_trial_smoke_is_always_ok_and_json_conforms_to_schema(tmp_path, monkeypatch):
    import jsonschema

    results = [ed.run_trial(t, seed=4242) for t in range(3)]
    for r in results:
        assert not r.get("hang"), r
        assert ed.is_ok(r), r

    out = tmp_path / "diskfull.json"
    monkeypatch.setattr(sys, "argv",
                        ["eval_diskfull.py", "--trials", "3", "--seed", "4242", "--json", str(out)])
    rc = ed.main()
    assert rc == 0
    schema = json.loads((ROOT / "benchmarks" / "schema" / "result_manifest.schema.json").read_text())
    jsonschema.validate(json.loads(out.read_text()), schema)
