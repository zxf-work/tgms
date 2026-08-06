"""Append-only JSONL write-ahead provenance log (WP1.1).

Every write batch is appended *before* it is applied to the backend.
Record: {"batch_id", "tt", "ops": [...]}. First line is a header record.
Purposes: provenance, crash recovery (replay), backend migration.

Replay cursor (suffix recovery): the native manifest records `(offset,
chain)` per generation — `offset` is the absolute file position immediately
past the newline of the last applied record, and `chain` is a rolling hash
over the record bytes up to that offset (`chain_0 = sha256("")[:16]`,
`chain_n = sha256(chain_{n-1} as ASCII hex || record_bytes)[:16]`, matching
the engine's `EventLogRef`). Reopening after a crash verifies the chain of
the applied prefix and replays only the un-applied suffix; a cursor the log
cannot account for is corruption and says so, never silence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

from tgms.core.errors import StateError
from tgms.core.model import canonical_json, sha256_hex

HEADER = {"format": "tgms-eventlog", "version": 1}

#: Truncation shared with the engine's manifest digests (manifest.rs).
CHAIN_HEX_LEN = 16

#: The chain value of an empty log — the seed generation 0 records.
SEED_CHAIN = hashlib.sha256(b"").hexdigest()[:CHAIN_HEX_LEN]


def extend_chain(prev: str, record_bytes: bytes) -> str:
    """Fold one raw record (newline included) into the rolling chain.

    Must match `EventLogRef::extend_chain` in the engine byte for byte —
    the manifest stores what Rust computes for its own tests, and recovery
    compares against what this computes.
    """
    return hashlib.sha256(prev.encode("ascii") + record_bytes).hexdigest()[
        :CHAIN_HEX_LEN
    ]


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(canonical_json(HEADER) + "\n")
        else:
            with open(self.path, "r", encoding="utf-8") as f:
                head = json.loads(f.readline())
            if head.get("format") != HEADER["format"]:
                raise StateError(f"not a tgms event log: {self.path}")

    def append(self, tt: int, ops: list[dict[str, Any]]) -> tuple[str, int, bytes]:
        """Append one batch; fsync before returning (write-ahead guarantee).

        Returns `(batch_id, end_offset, record_bytes)`: the offset points
        immediately past the record's newline and, with the record bytes,
        lets the caller advance its replay cursor without re-reading the log.
        """
        batch_id = sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16]
        record = canonical_json({"batch_id": batch_id, "tt": tt, "ops": ops})
        record_bytes = (record + "\n").encode("utf-8")
        with open(self.path, "ab") as f:
            f.write(record_bytes)
            f.flush()
            os.fsync(f.fileno())
            end_offset = f.tell()
        return batch_id, end_offset, record_bytes

    def size(self) -> int:
        return self.path.stat().st_size

    def batches(self) -> Iterator[dict[str, Any]]:
        for batch, _end, _raw in self.batches_from(0):
            yield batch


    def trim_torn_tail(self, applied_offset: int) -> int | None:
        """Truncate a torn *final* record left by a crash mid-append (D-086).

        A record the append call produced ends in a newline and carries a
        `batch_id` that is the hash of its own content, so three signatures
        at end-of-file mark a tail the crash tore: bytes that do not parse,
        a record with no terminating newline, and a parseable record whose
        id does not match its content. Such a record was never acknowledged
        — `append` fsyncs and *then* returns — so truncating it breaks no
        promise a caller ever received.

        The boundary is as important as the trim: only defects whose bytes
        run to end-of-file qualify. Damage with records after it, and any
        defect at or before `applied_offset` (the cursor's applied prefix),
        is corruption and stays an error for the callers that read those
        ranges. Returns the truncation offset, or None if the tail is sound.
        """
        size = self.size()
        with open(self.path, "rb") as f:
            f.seek(applied_offset or len(f.readline()))
            while True:
                start = f.tell()
                raw = f.readline()
                if not raw:
                    return None
                if not raw.strip():
                    continue
                torn = False
                if not raw.endswith(b"\n"):
                    torn = True
                else:
                    try:
                        batch = json.loads(raw)
                        expect = sha256_hex(canonical_json(
                            {"tt": batch["tt"], "ops": batch["ops"]}))[:16]
                        if batch.get("batch_id") != expect:
                            torn = True
                    except (json.JSONDecodeError, KeyError, TypeError):
                        torn = True
                if torn:
                    if f.tell() != size:
                        return None  # not the tail: leave it for the loud path
                    with open(self.path, "r+b") as w:
                        w.truncate(start)
                        w.flush()
                        os.fsync(w.fileno())
                    return start

    def batches_from(self, offset: int) -> Iterator[tuple[dict[str, Any], int, bytes]]:
        """Batches whose records start at or after `offset`, as
        `(batch, end_offset, record_bytes)`.

        `offset` 0 means "from the first batch" (the header line is
        skipped); any other value must be a record boundary a cursor
        recorded — landing mid-record is corruption, and the JSON parse
        below says so rather than resynchronizing silently.
        """
        with open(self.path, "rb") as f:
            header = f.readline()  # header record, outside the chain
            if offset:
                if offset < len(header):
                    raise StateError(
                        f"event-log cursor {offset} points inside the header "
                        f"of {self.path}"
                    )
                f.seek(offset)
            while True:
                raw = f.readline()
                if not raw:
                    return
                if not raw.strip():
                    continue
                try:
                    batch = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise StateError(
                        f"event log {self.path} is not readable at offset "
                        f"{f.tell() - len(raw)}: {e} — the replay cursor may "
                        f"not be on a record boundary"
                    ) from None
                yield batch, f.tell(), raw

    def chain_of_prefix(self, offset: int) -> str:
        """The rolling chain over every record ending at or before `offset`.

        Walking must land exactly on `offset`; overshooting means the
        cursor is not on a record boundary, which is corruption.
        """
        chain = SEED_CHAIN
        if offset == 0:
            return chain
        pos = None
        for _batch, end, raw in self.batches_from(0):
            if end > offset:
                break
            chain = extend_chain(chain, raw)
            pos = end
            if end == offset:
                return chain
        raise StateError(
            f"event-log cursor {offset} is not a record boundary of "
            f"{self.path} (records end at {pos})"
        )

    def last_tt(self) -> int:
        """Transaction time of the last batch (0 if empty).

        Linear scan; fine at research scale. TODO(phase3): tail-seek.
        """
        last = 0
        for batch in self.batches():
            last = batch["tt"]
        return last


def replay(eventlog_path: str | Path, adapter: Any, *,
           thread_cursor: bool = False) -> int:
    """Replay a log into a fresh adapter; returns number of batches applied.

    Applies each batch at its recorded tt, so the resulting store content
    (and store_digest) is identical to the original, on any backend.

    `thread_cursor=True` additionally records the replay cursor per batch on
    adapters that keep one (the native engine), so the rebuilt store carries
    valid suffix-recovery state. Only pass it when the store's own
    `eventlog.jsonl` is (or will be, before the store is next opened) a
    byte-identical copy of `eventlog_path` — the cursor names offsets into
    that file, and recovery verifies them loudly. `tgms replay` copies the
    log into place first and then threads the cursor; a caller replaying a
    foreign log into a throwaway store must not.
    """
    from tgms.core.errors import TgmsError

    log = EventLog(eventlog_path)
    note_cursor = getattr(adapter, "note_event_cursor", None) if thread_cursor \
        else None
    chain = SEED_CHAIN
    n = 0
    prev_tt = 0
    for batch, end, raw in log.batches_from(0):
        tt = batch["tt"]
        if tt <= prev_tt:
            raise StateError(f"non-monotonic tt in event log: {tt} after {prev_tt}")
        chain = extend_chain(chain, raw)
        adapter.begin()
        try:
            adapter.apply_ops(batch["ops"], tt)
        except TgmsError:
            # a batch that failed on the live path fails identically here
            # (apply is deterministic); skip it, exactly as the writer did
            adapter.rollback()
            prev_tt = tt
            continue
        if note_cursor is not None:
            note_cursor(end, chain)
        adapter.commit()
        prev_tt = tt
        n += 1
    return n
