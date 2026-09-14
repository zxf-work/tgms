"""Unit tests for `tgms/tools/retry_io.py` (the EDQUOT-retry hardening for
campaign harnesses on the iTiger cluster's shared-quota /project
filesystem).

Two families:

  1. Fault-injection tests against the retry primitives themselves --
     a transient `EDQUOT`/`ENOSPC` is retried and eventually succeeds with
     the exact intended bytes and no partial file ever visible at the
     final path; a non-retryable errno propagates on the first attempt;
     exhaustion re-raises after the last attempt; `mkdir_with_retry` is
     idempotent and also retries transient failures.
  2. Byte-identity tests, one per harness write site touched by this
     hardening pass (`scripts/bench_correction_storm.py`,
     `scripts/eval_corruption.py`, `scripts/eval_diskfull.py`,
     `scripts/eval_durability.py`, `scripts/eval_trust_boundary_matrix.py`,
     `scripts/bench_overhead_ladder.py`) -- each reproduces that site's own
     `json.dumps(...)` call over a tiny sample record and asserts
     `write_bytes_with_retry` lands the identical bytes a plain
     `Path.write_text`/`open(...).write` would have, so swapping in the
     retry helper never changed a `result_digest` or a frozen record's
     shape.

All tests are host-independent (no ssh, no experiments, no real disk
pressure) and run in well under a second: every retry loop's `time.sleep`
is monkeypatched to a no-op.
"""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgms.tools import retry_io  # noqa: E402
from tgms.tools.retry_io import (  # noqa: E402
    atomic_write_json_with_retry,
    mkdir_with_retry,
    write_bytes_with_retry,
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this file is pure fault-injection -- never actually
    wait out a "backoff"."""
    monkeypatch.setattr(retry_io.time, "sleep", lambda _s: None)


def _flaky(n_failures: int, errno_value: int, real):
    """Returns a callable that raises `OSError(errno_value)` the first
    `n_failures` calls, then delegates to `real`."""
    calls = {"n": 0}

    def _fn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= n_failures:
            raise OSError(errno_value, "injected fault")
        return real(*args, **kwargs)

    _fn.calls = calls
    return _fn


# --------------------------------------------------------------------------- #
# write_bytes_with_retry: fault injection                                     #
# --------------------------------------------------------------------------- #

def test_transient_edquot_then_success_lands_exact_bytes(tmp_path, monkeypatch):
    real_replace = retry_io.os.replace
    fake = _flaky(3, errno.EDQUOT, real_replace)
    monkeypatch.setattr(retry_io.os, "replace", fake)

    dest = tmp_path / "record.json"
    payload = b'{"hello": "world"}\n'
    write_bytes_with_retry(dest, payload, attempts=12, delay_s=0.0)

    assert dest.read_bytes() == payload
    assert fake.calls["n"] == 4  # 3 failures + 1 success
    # no leftover temp files
    assert list(tmp_path.iterdir()) == [dest]


def test_transient_enospc_then_success_lands_exact_bytes(tmp_path, monkeypatch):
    real_replace = retry_io.os.replace
    fake = _flaky(2, errno.ENOSPC, real_replace)
    monkeypatch.setattr(retry_io.os, "replace", fake)

    dest = tmp_path / "record.json"
    payload = b"line one\nline two\n"
    write_bytes_with_retry(dest, payload, attempts=12, delay_s=0.0)

    assert dest.read_bytes() == payload


def test_no_partial_file_ever_visible_at_final_path(tmp_path, monkeypatch):
    """Every failing attempt must leave `dest` exactly as it was before the
    call (absent, here) -- the temp-file-then-rename discipline means a
    reader polling `dest.exists()` never observes a half-written record."""
    dest = tmp_path / "record.json"
    seen_dest_present_during_failure = []
    real_replace = retry_io.os.replace

    calls = {"n": 0}

    def fake_replace(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 5:
            seen_dest_present_during_failure.append(dest.exists())
            raise OSError(errno.EDQUOT, "injected fault")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(retry_io.os, "replace", fake_replace)
    write_bytes_with_retry(dest, b"payload", attempts=12, delay_s=0.0)

    assert not any(seen_dest_present_during_failure)
    assert dest.read_bytes() == b"payload"


def test_non_retryable_errno_raises_immediately(tmp_path, monkeypatch):
    real_replace = retry_io.os.replace
    fake = _flaky(99, errno.EACCES, real_replace)
    monkeypatch.setattr(retry_io.os, "replace", fake)

    dest = tmp_path / "record.json"
    with pytest.raises(OSError) as exc_info:
        write_bytes_with_retry(dest, b"x", attempts=12, delay_s=0.0)

    assert exc_info.value.errno == errno.EACCES
    assert fake.calls["n"] == 1  # no retry attempted
    assert not dest.exists()


def test_exhaustion_raises_after_attempts(tmp_path, monkeypatch):
    def always_fails(*args, **kwargs):
        raise OSError(errno.EDQUOT, "still failing")

    monkeypatch.setattr(retry_io.os, "replace", always_fails)

    dest = tmp_path / "record.json"
    with pytest.raises(OSError) as exc_info:
        write_bytes_with_retry(dest, b"x", attempts=4, delay_s=0.0)

    assert exc_info.value.errno == errno.EDQUOT
    assert not dest.exists()
    # exhausted attempts leave no stray temp files behind either
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# mkdir_with_retry                                                             #
# --------------------------------------------------------------------------- #

def test_mkdir_with_retry_idempotent(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    mkdir_with_retry(target)
    assert target.is_dir()
    mkdir_with_retry(target)  # second call: still success, no error
    assert target.is_dir()


def test_mkdir_with_retry_retries_transient_edquot(tmp_path, monkeypatch):
    # parent pre-exists so the real pathlib implementation never needs to
    # recurse into `self.parent.mkdir(...)` -- keeps the injected failure
    # count on `target` exact and independent of pathlib's own internals.
    parent = tmp_path / "records"
    parent.mkdir()
    target = parent / "task-0"

    real_mkdir = Path.mkdir
    calls = {"n": 0}

    def fake_mkdir(self, *args, **kwargs):
        if self == target:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError(errno.EDQUOT, "injected fault")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    mkdir_with_retry(target, attempts=12, delay_s=0.0)
    assert target.is_dir()
    assert calls["n"] == 3


def test_mkdir_with_retry_non_retryable_raises_immediately(tmp_path, monkeypatch):
    def fake_mkdir(self, *args, **kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    target = tmp_path / "x"
    with pytest.raises(OSError) as exc_info:
        mkdir_with_retry(target, attempts=12, delay_s=0.0)
    assert exc_info.value.errno == errno.EACCES
    assert not target.exists()


# --------------------------------------------------------------------------- #
# atomic_write_json_with_retry                                                #
# --------------------------------------------------------------------------- #

def test_atomic_write_json_with_retry_canonical_shape(tmp_path):
    dest = tmp_path / "out.json"
    obj = {"b": 2, "a": 1}
    atomic_write_json_with_retry(dest, obj)
    assert dest.read_text() == json.dumps(obj, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Byte-identity, one per harness write site                                   #
# --------------------------------------------------------------------------- #
# Each reproduces that site's own json.dumps(...) call (indent/sort_keys/
# separators/trailing-newline all as they were before this hardening pass)
# and checks write_bytes_with_retry lands exactly those bytes.

def test_bench_correction_storm_manifest_bytes_unchanged(tmp_path):
    manifest = {"schema_version": "1.0.0", "result_digest": "deadbeef",
               "config": {"seed": 0, "arms": ["c1"]}, "summary": {"n": 3}}
    expected = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    dest = tmp_path / "storm-store-0.json"
    write_bytes_with_retry(dest, expected)
    assert dest.read_bytes() == expected
    # and it matches the literal expression the harness uses at that site
    assert dest.read_bytes() == (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_bench_correction_storm_rows_jsonl_bytes_unchanged(tmp_path):
    rows = [{"batch": 0, "arm": "c1"}, {"batch": 1, "arm": "c2"}]
    expected = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)

    dest = tmp_path / "storm-store-0-rows.jsonl"
    write_bytes_with_retry(dest, expected.encode("utf-8"))

    # the pre-hardening code wrote line by line with a plain text handle;
    # reproduce that exactly and compare.
    direct = tmp_path / "direct-rows.jsonl"
    with open(direct, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    assert dest.read_bytes() == direct.read_bytes()


def test_eval_corruption_manifest_bytes_unchanged(tmp_path):
    manifest = {"schema_version": "1.0.0", "result_digest": "cafef00d",
               "results": [{"class": "segment_byte_flip", "verdict": "DETECTED"}]}
    expected = (json.dumps(manifest, indent=1) + "\n").encode("utf-8")

    dest = tmp_path / "corruption-task-0.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(manifest, indent=1) + "\n")
    assert dest.read_bytes() == direct.read_bytes()


def test_eval_diskfull_manifest_bytes_unchanged(tmp_path):
    manifest = {"schema_version": "1.0.0", "result_digest": "0ff1ce",
               "results": [{"mode": "fsync_enospc", "fault_fired": True}]}
    expected = (json.dumps(manifest, indent=1) + "\n").encode("utf-8")

    dest = tmp_path / "diskfull-task-0.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(manifest, indent=1) + "\n")
    assert dest.read_bytes() == direct.read_bytes()


def test_eval_durability_recovery_crash_bytes_unchanged(tmp_path):
    payload = {"results": [{"write_boundary": "after_seal", "wall_s": 1.2}],
              "manifest": {"commit": "abc123", "seed": 7, "mode": "recovery-crash"}}
    expected = (json.dumps(payload, indent=1) + "\n").encode("utf-8")

    dest = tmp_path / "crash-task-0.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(payload, indent=1) + "\n")
    assert dest.read_bytes() == direct.read_bytes()


def test_eval_durability_main_bytes_unchanged(tmp_path):
    payload = {"results": [{"write_boundary": "after_seal", "wall_s": 1.2}],
              "manifest": {"commit": "abc123", "seed": 7}}
    expected = (json.dumps(payload, indent=1) + "\n").encode("utf-8")

    dest = tmp_path / "crash-task-1.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(payload, indent=1) + "\n")
    assert dest.read_bytes() == direct.read_bytes()


def test_eval_trust_boundary_matrix_manifest_bytes_unchanged(tmp_path):
    from tgms.core.model import canonical_json

    manifest = {"schema_version": "1.0.0", "cell": "F1-1",
               "trials": [{"gold_mismatch": False}]}
    expected = canonical_json(manifest).encode("utf-8")

    dest = tmp_path / "F1-1-collegemsg.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(canonical_json(manifest))
    assert dest.read_bytes() == direct.read_bytes()


def test_bench_overhead_ladder_manifest_bytes_unchanged(tmp_path):
    manifest = {"schema_version": "1.0.0", "result_digest": "abc123",
               "rows": [{"rung": 1, "plan": "p1"}]}
    expected = json.dumps(manifest, indent=1, sort_keys=True, default=str).encode("utf-8")

    dest = tmp_path / "overhead-ladder-store.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(manifest, indent=1, sort_keys=True, default=str))
    assert dest.read_bytes() == direct.read_bytes()


def test_bench_overhead_ladder_single_rung_bytes_unchanged(tmp_path):
    result = {"rung": 3, "outcome": "OK", "trace_bytes": 4096}
    expected = json.dumps(result, indent=1, default=str).encode("utf-8")

    dest = tmp_path / "single-rung.json"
    write_bytes_with_retry(dest, expected)

    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(result, indent=1, default=str))
    assert dest.read_bytes() == direct.read_bytes()
