"""A read-only open must not confuse an in-flight write for corruption
(invariant 1.5, extended to readers; CI-observed race, run 34852755086,
2026-09-14, `test_concurrency.py::
test_readers_opening_throughout_a_write_run_never_damage_the_store`).

`EventLog.batches_from` and `Registry._load` used to raise `StateError` on
any parse failure at the tail of their file — right for a cursor landing
mid-record (corruption), wrong for a reader that opens while the writer is
still inside `append()`'s single `write()` call and sees the not-yet-
committed final line. Recovery already forgave exactly this shape for a
*writer* (`trim_torn_tail`, D-086); a reader never runs recovery by design
(`Store.read_only`, D-049), so `Store.__init__(read_only=True)`'s own scan of
the log (`_seed_frontier` -> `_tt_at_offset`) used to hit the writer's torn
tail and raise instead of simply stopping before it. This file checks the
fix: an explicit `tolerate_torn_tail` parameter, passed only by the
read-only path, that treats a torn *final* record as uncommitted rather than
corrupt, while damage anywhere else in the file still raises exactly as
before — for both a reader and a writer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tgms
from tgms.artifact.record import StepDependency
from tgms.artifact.registry import Registry
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


def test_read_only_open_tolerates_a_torn_final_record(tmp_path: Path) -> None:
    """(1) N committed records + a torn partial final line (no newline):
    `Store(read_only=True)` opens, sees all N records, raises nothing."""
    log = _store_with_writes(tmp_path / "s", n=4)
    with open(log, "ab") as f:
        f.write(TORN_TAIL)  # no trailing newline: an in-flight append

    reader = tgms.open(tmp_path / "s", backend="native", read_only=True)
    for i in range(4):
        got = reader.adapter.believed_node_versions(f"a{i}")
        assert len(got) == 1 and got[0].props["i"] == i, f"reader lost a{i}"
    reader.close()
    # a reader never trims — the torn bytes are exactly as a writer left them
    assert log.read_bytes().endswith(TORN_TAIL)


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
