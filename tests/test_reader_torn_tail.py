"""A read-only open must not confuse an in-flight write for corruption
(invariant 1.5, extended to readers; CI-observed race, run 34852755086,
2026-09-14, `test_concurrency.py::
test_readers_opening_throughout_a_write_run_never_damage_the_store`) —
*without* also swallowing a corruption sweep's own torn-tail injection onto
a store nothing is writing to any more
(`tests/test_eval_corruption.py::test_torn_event_log_tail_is_detected_even_read_only`,
which this fix must leave passing unchanged).

`EventLog.batches_from` and `Registry._load` used to raise `StateError` on
any parse failure at the tail of their file — right for a cursor landing
mid-record (corruption), wrong for a reader that opens while the writer is
still inside `append()`'s single `write()` call and sees the not-yet-
committed final line. Recovery already forgave exactly this shape for a
*writer* (`trim_torn_tail`, D-086); a reader never runs recovery by design
(`Store.read_only`, D-049), so `Store.__init__(read_only=True)`'s own scan of
the log (`_seed_frontier` -> `_tt_at_offset`) used to hit the writer's torn
tail and raise instead of simply stopping before it.

The first fix (an unconditional `tolerate_torn_tail: bool`) went too far: a
torn tail appended to a *closed* store — exactly what the corruption
sweep's `event_log_tail`/`append_garbage` mutation does — is byte-for-byte
indistinguishable from a live writer's not-yet-finished record. Two more
signals are needed, both required before a torn tail is ever forgiven
(`Store._compute_reader_torn_tail_floor`):

1. **the record's start offset is at or past the manifest's own applied
   event-log offset** (`EventLog.batches_from`'s `tolerate_torn_tail_from`,
   the same value `trim_torn_tail(applied_offset)` uses for a writer) — a
   torn record starting *before* it is damage to already-applied history,
   never forgiven regardless of where the file now ends;
2. **some process actually holds `writer.lock` right now**
   (`Store._writer_lock_is_held`, a non-blocking `flock` probe) — a torn
   tail found with the lock free cannot be an in-flight write, since a
   reader never holds this lock and nothing else could still be appending.

Both conditions hold in the real race; neither holds for the corruption
sweep's closed-store injection. This file checks all three: the two gates
together (tolerated), either one failing alone (refused), and the artifact
registry's read path (which has no analogous applied-offset concept, so it
keeps unconditional tolerance for a torn final line — but `verify()` must
still flag it, so the corruption classifier's `verify_problems` path still
reaches DETECTED for it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tgms
from tgms.artifact.record import StepDependency
from tgms.artifact.registry import Registry
from tgms.artifact.registry import verify as registry_verify
from tgms.core.errors import StateError
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

TORN_TAIL = b'{"batch_id":"deadbeef00000000","tt":99999,"ops":[{"op":"assert_'


# --------------------------------------------------------------------------- #
# 1-3. the event log: Store(read_only=True) vs. the writer                    #
# --------------------------------------------------------------------------- #


def _store_with_writes(path: Path, n: int = 4) -> Path:
    store = tgms.open(path, backend="native")
    for i in range(n):
        store.assert_node(f"a{i}", "N", {"i": i}, vt_s=0, vt_e=100)
    store.close()
    return path / "eventlog.jsonl"


def test_read_only_open_tolerates_a_torn_final_record_while_writer_is_active(
        tmp_path: Path) -> None:
    """(1)/(2b) N committed records, a torn partial final line (no
    newline) starting at/above the applied offset, appended while the
    writer handle is still open (so `writer.lock` is held): `Store(
    read_only=True)` opens, sees all N records, raises nothing — the two
    gates invariant 1.5's reader clause requires are both satisfied."""
    store_dir = tmp_path / "s"
    writer = tgms.open(store_dir, backend="native")
    for i in range(4):
        writer.assert_node(f"a{i}", "N", {"i": i}, vt_s=0, vt_e=100)

    log = store_dir / "eventlog.jsonl"
    with open(log, "ab") as f:
        f.write(TORN_TAIL)  # no trailing newline: an in-flight append

    # writer.lock is still held: `writer` has not closed yet
    reader = tgms.open(store_dir, backend="native", read_only=True)
    for i in range(4):
        got = reader.adapter.believed_node_versions(f"a{i}")
        assert len(got) == 1 and got[0].props["i"] == i, f"reader lost a{i}"
    reader.close()
    writer.close()
    # a reader never trims — the torn bytes are exactly as left them
    assert log.read_bytes().endswith(TORN_TAIL)


def test_torn_final_record_is_not_tolerated_once_the_writer_closes(
        tmp_path: Path) -> None:
    """(c)-shaped unit check, mirroring `tests/test_eval_corruption.py::
    test_torn_event_log_tail_is_detected_even_read_only`: the identical
    torn bytes at the identical offset, appended to a *closed* store (no
    process holds `writer.lock` any more), are refused — the record's
    start offset alone cannot tell an in-flight write apart from a
    corruption sweep's injection onto dead history; only the lock can."""
    log = _store_with_writes(tmp_path / "s", n=4)
    with open(log, "ab") as f:
        f.write(TORN_TAIL)

    with pytest.raises(StateError):
        tgms.open(tmp_path / "s", backend="native", read_only=True)


def test_torn_record_below_applied_offset_raises_even_with_an_active_writer(
        tmp_path: Path) -> None:
    """(2a) A torn record starting BELOW the manifest's applied offset is
    corruption of already-applied history, never forgiven — even while a
    writer is genuinely active (the one signal that gates tolerance at
    all), and even though truncating everything after it fakes the shape
    of an in-flight tail (unparseable, runs to the file's new end)."""
    store_dir = tmp_path / "s"
    writer = tgms.open(store_dir, backend="native")
    for i in range(4):
        writer.assert_node(f"a{i}", "N", {"i": i}, vt_s=0, vt_e=100)
    applied_offset, _chain = writer.adapter.event_cursor()

    log = store_dir / "eventlog.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 5  # header + 4 records
    # tear record index 1 — an already-applied, historical record — and
    # drop everything after it, faking "this is now the file's last byte"
    torn = lines[1][: len(lines[1]) // 2]
    truncated = lines[0] + torn
    log.write_bytes(truncated)
    assert len(truncated) < applied_offset, (
        "fixture bug: the fake tail must land before the applied offset")

    # writer.lock is still held throughout — the offset check alone must
    # be what refuses this, not the absence of a live writer
    with pytest.raises(StateError):
        tgms.open(store_dir, backend="native", read_only=True)
    writer.close()


def test_writer_open_still_trims_a_torn_final_record(tmp_path: Path) -> None:
    """(2) A writer opening the same directory still recovers via
    `trim_torn_tail` exactly as today (D-086) — tolerance is a reader-only
    behaviour, not a relaxation of the writer's contract."""
    log = _store_with_writes(tmp_path / "s", n=4)
    with open(log, "ab") as f:
        f.write(TORN_TAIL)

    writer = tgms.open(tmp_path / "s", backend="native")
    for i in range(4):
        got = writer.adapter.believed_node_versions(f"a{i}")
        assert len(got) == 1 and got[0].props["i"] == i, f"writer lost a{i}"
    # the store must remain durable and writable after trimming
    writer.assert_node("post", "N", {"i": 42}, vt_s=0, vt_e=100)
    writer.close()

    body = log.read_bytes()
    assert b"deadbeef00000000" not in body, "the torn tail was not trimmed"
    json.loads(body.splitlines()[-1])  # the log now ends on a sound record


def test_torn_record_not_at_eof_still_raises_for_reader_and_writer(tmp_path: Path) -> None:
    """(3) A torn line that is NOT the file's last record is corruption,
    regardless of who opens it."""
    log = _store_with_writes(tmp_path / "s", n=4)
    lines = log.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 4
    lines[2] = lines[2][: len(lines[2]) // 2]  # tear a MIDDLE record
    log.write_bytes(b"".join(lines))

    with pytest.raises(StateError):
        reader = tgms.open(tmp_path / "s", backend="native", read_only=True)
        reader.adapter.believed_node_versions("a0")  # some backends defer the read
    with pytest.raises(StateError):
        tgms.open(tmp_path / "s", backend="native")


# --------------------------------------------------------------------------- #
# 4. the same trio for the artifact registry's read path                      #
# --------------------------------------------------------------------------- #

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)


def _registered_store(store: Path, n: int = 3) -> Path:
    """A store with `n` generations of one artifact, `art`, registered."""
    log = EventLog(store / "eventlog.jsonl")
    log.append(10, [NODE_A])
    identity = store_identity(log.header(), log.first_batch())
    scope = DependencyScope(store=identity, tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("A",))),))
    reg = Registry(store)
    for _ in range(n):
        reg.register(
            name="art", kind="query_result", store=identity,
            plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1},
            basis={"tt_q": 10, "pinned": False, "clamped": False,
                   "tt_q_verified": True},
            state={"completeness": "complete", "exactness": "exact", "refusal": None},
            refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
            steps=[StepDependency("s1", scope)],
        )
    return store / "artifacts.jsonl"


def test_registry_read_only_tolerates_a_torn_final_record(tmp_path: Path) -> None:
    """(4a) A read-only registry open tolerates a torn final record, the
    same shape as the event log's own reader tolerance."""
    path = _registered_store(tmp_path, n=3)
    with open(path, "ab") as f:
        f.write(b'{"name":"art","generation":3,"kind":"query_r')  # no newline

    reg = Registry(tmp_path, read_only=True)
    assert reg.names() == ("art",)
    assert len(reg.history("art")) == 3
    assert reg.current("art").generation == 2
    # nothing was written — a read-only registry never trims or repairs
    assert path.read_bytes().endswith(b'"kind":"query_r')


def test_registry_default_open_still_raises_on_a_torn_final_record(tmp_path: Path) -> None:
    """(4b) A default (writer-shaped) open has no trim mechanism for the
    registry — unlike the event log, this behaviour is unchanged by the fix:
    it keeps raising on a torn tail exactly as before."""
    path = _registered_store(tmp_path, n=3)
    with open(path, "ab") as f:
        f.write(b'{"name":"art","generation":3,"kind":"query_r')

    with pytest.raises(StateError):
        Registry(tmp_path)


def test_registry_torn_record_not_at_eof_raises_for_reader_and_default_open(
        tmp_path: Path) -> None:
    """(4c) A torn line that is NOT the registry's last record is
    corruption for both a read-only and a default open.

    Four generations, torn one record short of the tail: truncating a
    record's newline merges it with the record after it into one
    unparseable line that itself ends in a newline — a *fourth* record
    (untouched) still follows, so that merged line is not the file's last
    bytes and must stay corruption regardless of `tolerate_torn_tail`.
    Tearing the second-to-last record instead would merge straight through
    to true end-of-file and get mistaken for an in-flight write."""
    path = _registered_store(tmp_path, n=4)
    lines = path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 5  # header + 4 records
    lines[2] = lines[2][: len(lines[2]) // 2]  # tear generation 1 of 4
    path.write_bytes(b"".join(lines))

    with pytest.raises(StateError):
        Registry(tmp_path, read_only=True)
    with pytest.raises(StateError):
        Registry(tmp_path)


def test_registry_torn_final_record_is_still_a_verify_finding(tmp_path: Path) -> None:
    """(4d) The registry has no applied-offset concept to gate tolerance
    with — every record it ever writes is folded synchronously under
    `_lock_path`, so there is no "log ahead of backend" gap the way the
    event log has — so a read-only open keeps unconditional tolerance for
    a torn final line (above). That must not blind the corruption
    classifier: `Registry.verify()` (the read-only walk `tgms store
    verify` and `NativeStoreAdapter.verify(mode="full")` use, feeding
    `scripts/eval_corruption.py::classify`'s `verify_problems` check)
    never trims or tolerates anything, torn tail or not — its own
    docstring: "a torn tail is a *finding* here, never a repair" — so it
    must still report this one."""
    path = _registered_store(tmp_path, n=3)
    with open(path, "ab") as f:
        f.write(b'{"name":"art","generation":3,"kind":"query_r')  # no newline

    problems = registry_verify(tmp_path)
    assert problems, "a torn final registry record must still be a verify() finding"
