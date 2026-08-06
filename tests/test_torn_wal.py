"""A torn final WAL record is a crash artifact, not corruption (D-086).

Injected-crash trials (scripts/eval_durability.py) found the one boundary
where recovery failed: a power cut mid-append leaves a torn final record,
and reopen raised StateError instead of recovering. The torn record was
never acknowledged — the append never returned, so no caller was promised
anything — and everything before it chain-verifies. The correct behaviour
is to truncate the torn tail and recover; refusing to open turns a routine
crash into an outage that needs manual surgery.

The boundary of the fix matters as much as the fix: only the FINAL record
may be treated as torn. Three torn signatures at end-of-file — unparseable
bytes, a parseable record with no terminating newline, and the subtle one,
a torn record whose prefix still parses but whose `batch_id` no longer
matches the hash of its own content. Damage anywhere before the final
record stays what it always was: corruption, refused loudly.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.core.errors import StateError


def _store_with_writes(path, n=4):
    store = tgms.open(path, backend="native")
    for i in range(n):
        store.assert_node(f"a{i}", "N", {"i": i}, vt_s=0, vt_e=100)
    store.close()
    return path / "eventlog.jsonl"


def _reopen_and_check(path, n=4):
    store = tgms.open(path, backend="native")
    for i in range(n):
        got = store.adapter.believed_node_versions(f"a{i}")
        assert len(got) == 1 and got[0].props["i"] == i, f"acked a{i} lost"
    # the store must remain writable, and the write must be durable
    store.assert_node("post", "N", {"i": 42}, vt_s=0, vt_e=100)
    store.close()
    store = tgms.open(path, backend="native")
    assert store.adapter.believed_node_versions("post")[0].props["i"] == 42
    store.close()


def test_unparseable_torn_tail_is_truncated_and_recovered(tmp_path):
    log = _store_with_writes(tmp_path / "s")
    with open(log, "ab") as f:
        f.write(b'{"batch_id":"deadbeef00000000","tt":99999,"ops":[{"op":"assert_')
    _reopen_and_check(tmp_path / "s")
    # the torn bytes are gone from the log itself
    last = open(log, "rb").read().splitlines()[-1]
    json.loads(last)


def test_parseable_record_without_newline_is_torn(tmp_path):
    log = _store_with_writes(tmp_path / "s")
    with open(log, "ab") as f:
        f.write(b'{"batch_id":"deadbeef00000000","tt":99999,"ops":[]}')  # no \n
    _reopen_and_check(tmp_path / "s")


def test_parseable_torn_prefix_is_caught_by_batch_id(tmp_path):
    """The subtle tear: the surviving prefix of the record is valid JSON with
    a trailing newline, indistinguishable by framing alone. The batch_id is a
    hash of {"tt", "ops"}, so a record whose id does not match its own
    content cannot be a record the append call produced."""
    log = _store_with_writes(tmp_path / "s")
    with open(log, "ab") as f:
        f.write(b'{"batch_id":"deadbeef00000000","tt":99999,"ops":[]}\n')
    _reopen_and_check(tmp_path / "s")
    # truncated, not silently applied: the forged record must be gone from
    # the log (the pre-fix behaviour replayed it as if it were real, which
    # made this test pass vacuously until this assertion existed)
    body = open(log, "rb").read()
    assert b"deadbeef00000000" not in body, (
        "the batch_id-mismatched tail record was replayed instead of "
        "truncated")


def test_torn_bytes_before_the_final_record_still_refuse(tmp_path):
    """Truncation must never eat committed history: damage that is not the
    file's tail is corruption, and corruption refuses loudly."""
    log = _store_with_writes(tmp_path / "s")
    lines = open(log, "rb").read().splitlines(keepends=True)
    assert len(lines) >= 4
    lines[2] = lines[2][: len(lines[2]) // 2]  # tear a MIDDLE record
    open(log, "wb").writelines(lines)
    with pytest.raises(StateError):
        store = tgms.open(tmp_path / "s", backend="native")
        # some backends may defer the read to first use
        store.adapter.believed_node_versions("a0")


def test_acknowledged_batch_id_mismatch_is_not_silently_truncated(tmp_path):
    """A final record the CURSOR already accounts for is applied history —
    if its bytes are wrong, that is corruption behind the cursor, not a torn
    tail, and truncating it would erase an acknowledged write."""
    log = _store_with_writes(tmp_path / "s")
    lines = open(log, "rb").read().splitlines(keepends=True)
    rec = json.loads(lines[-1])
    rec["ops"] = []          # falsify content behind the recorded cursor
    lines[-1] = (json.dumps(rec, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False) + "\n").encode()
    open(log, "wb").writelines(lines)
    with pytest.raises(StateError):
        tgms.open(tmp_path / "s", backend="native")
