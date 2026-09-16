"""D-086-reader-torn-tail-race, second race (CI run 35036538129, Python 3.11
only, `tests/test_concurrency.py::
test_readers_opening_throughout_a_write_run_never_damage_the_store`,
`eventlog.jsonl` offset 4076): `EventLog.batches_from`'s reader-side
tolerance for a torn *final* record (see `tests/test_reader_torn_tail.py`
for the two-gate rule itself) checks the record's own snapshot (`raw`,
`end` — captured at the moment `readline()` first meets the defect)
against a **freshly re-stat'd** file size (`size = f.seek(0, 2)`,
`tgms/storage/eventlog.py:258`). Between those two reads, the writer's own
`append()` (one `write()`, then `fsync()`, then return — the write-ahead
guarantee `EventLog.append`'s docstring describes) can *finish the very
record the reader is looking at*: the file grows past the reader's stale
`end`, so the `end >= size` gate (`eventlog.py:259`) now reads False, the
`writer_active()` probe is skipped entirely (never reaches the one call
site the D-086 fix relies on), and the record — now a complete, sound,
newline-terminated record sitting a few bytes further along in the very
same file — is reported as loud damage instead of re-read and found fine.

This is not the JSONDecodeError-shaped race D-086-reader-torn-tail-race
already covers (a torn record with no forgiveness gate at all); it is the
"no terminating newline and is not the log's last record" message
(`eventlog.py:270-275`), raised precisely because the gate's *inputs* are
stale rather than because tolerance was refused correctly. Fixed by
`EventLog.batches_from`'s one-retry rule: when a torn/unparseable
candidate's stale `end` is behind a freshly re-stat'd `size`, it seeks
back to the record's `start` and `readline()`s once more before trusting
a "not last" verdict — see the ledger entry (D-086-reader-stale-tail-
snapshot-race) and the docstring of `batches_from` for the mechanism.
This test was `xfail(strict=True)` until that fix landed; the marker is
gone and it now asserts the fix directly.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from tgms.core.model import canonical_json, sha256_hex
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
import tgms.storage.eventlog as eventlog_mod

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)


class _CompleteRecordOnFinalSeek:
    """Wraps a real file handle open on the event log; the first time
    `batches_from` asks "where does the file currently end?" (its
    `f.seek(0, 2)` re-stat of the torn tail's own last-record check), this
    finishes the torn record on disk first — modelling the writer's
    `append()` (write, fsync, return) landing in that exact window."""

    def __init__(self, real: Any, on_final_seek) -> None:
        self._real = real
        self._on_final_seek = on_final_seek
        self._fired = False

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 2 and offset == 0 and not self._fired:
            self._fired = True
            self._on_final_seek()
        return self._real.seek(offset, whence)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __enter__(self) -> "_CompleteRecordOnFinalSeek":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._real.__exit__(*exc)


def test_writer_completing_the_torn_record_between_read_and_size_check_is_not_damage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "eventlog.jsonl"
    log = EventLog(log_path)
    log.append(10, [NODE_A])  # one clean, committed record ahead of the target

    # Build the record the "writer" is mid-append on: valid content, split
    # into what the reader's readline() sees first (torn: no newline) and
    # what the writer's append() finishes it with.
    tt, ops = 20, [NODE_A]
    batch_id = sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16]
    full_record = (canonical_json(
        {"batch_id": batch_id, "tt": tt, "ops": ops}) + "\n").encode("utf-8")
    split = len(full_record) // 2
    torn_prefix, remainder = full_record[:split], full_record[split:]
    assert not torn_prefix.endswith(b"\n")

    with open(log_path, "ab") as f:
        f.write(torn_prefix)  # the writer is "mid-append": no newline yet

    real_open = builtins.open

    def _complete_the_record() -> None:
        # models EventLog.append: write the rest, flush, fsync, return —
        # landing exactly between the reader's torn read and its
        # "is this still the last record?" size check.
        with real_open(log_path, "ab") as w:
            w.write(remainder)
            w.flush()

    def _wrapped_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        f = real_open(path, mode, *args, **kwargs)
        if Path(path) == log_path and mode == "rb":
            return _CompleteRecordOnFinalSeek(f, _complete_the_record)
        return f

    monkeypatch.setattr(eventlog_mod, "open", _wrapped_open, raising=False)

    # writer_active=True always: if the (buggy) code even reached the
    # probe, tolerance would be granted — the bug is that it never gets
    # there, because `end >= size` is already False by the time it asks.
    batches = list(EventLog(log_path).batches_from(
        0, tolerate_torn_tail_from=0, writer_active=lambda: True))

    assert [batch["tt"] for batch, _end, _raw in batches] == [10, 20], (
        "the reader should see BOTH records once the writer's append() "
        "actually completed the torn one — re-reading a torn tail before "
        "trusting a stale 'not last' verdict is what the fix must add")
