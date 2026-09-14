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
from tgms.storage.crashpoint import crash_point

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

        Normally this is one `write()` of the whole record, which is what
        makes a live append atomic with respect to a concurrent reader
        opening the log mid-write (`test_concurrency.py`). Only when the
        durability-injection point is actually armed
        (`TGMS_CRASH_POINT=py_torn_wal_append`, D-086) does the call instead
        write a partial record, flush it, and die via `crash_point` before
        the rest, the newline, or the fsync — leaving exactly the torn tail
        `EventLog.trim_torn_tail` exists to recover from. This reproduces,
        from inside the product, the shape `scripts/eval_durability.py` used
        to produce with a harness-local monkeypatch of this method.
        """
        batch_id = sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16]
        record = canonical_json({"batch_id": batch_id, "tt": tt, "ops": ops})
        record_bytes = (record + "\n").encode("utf-8")
        with open(self.path, "ab") as f:
            if os.environ.get("TGMS_CRASH_POINT") == "py_torn_wal_append":
                half = max(1, len(record_bytes) // 2)
                f.write(record_bytes[:half])
                f.flush()
                crash_point("py_torn_wal_append")  # dies here; never returns
            f.write(record_bytes)
            f.flush()
            os.fsync(f.fileno())
            end_offset = f.tell()
        return batch_id, end_offset, record_bytes

    def size(self) -> int:
        return self.path.stat().st_size

    def batches(self, *, tolerate_torn_tail_from: int | None = None
               ) -> Iterator[dict[str, Any]]:
        for batch, _end, _raw in self.batches_from(
                0, tolerate_torn_tail_from=tolerate_torn_tail_from):
            yield batch

    def header(self) -> dict[str, Any]:
        """The first line — the header record, which is outside the chain."""
        with open(self.path, "r", encoding="utf-8") as f:
            return json.loads(f.readline())

    def first_batch(self) -> dict[str, Any] | None:
        """The log's genesis record, or None while the log holds no batches.

        Together with the header this is the store's identity (D13.2's
        `store`): the batch carries a content-addressed `batch_id` and this
        history's own `tt`, so the pair distinguishes two stores while staying
        **identical across replays of one history** — which `store_digest()`,
        being content-dependent, does not.
        """
        for batch, _end, _raw in self.batches_from(0):
            return batch
        return None


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

    def batches_from(self, offset: int, *, tolerate_torn_tail_from: int | None = None
                     ) -> Iterator[tuple[dict[str, Any], int, bytes]]:
        """Batches whose records start at or after `offset`, as
        `(batch, end_offset, record_bytes)`.

        `offset` 0 means "from the first batch" (the header line is
        skipped); any other value must be a record boundary a cursor
        recorded — landing mid-record is corruption, and the JSON parse
        below says so rather than resynchronizing silently.

        `tolerate_torn_tail_from` (invariant 1.5, extended to readers): when
        a record fails to parse or is missing its terminating newline, its
        bytes run all the way to the file's current size ("current size" is
        checked fresh — `seek(0, 2)` at the moment the defect is found,
        rather than inferred from this read alone, since the writer may
        finish the record between the read that found the defect and this
        check), *and* the record's own start offset is at or past
        `tolerate_torn_tail_from`, treat it as an in-flight write that has
        not committed yet rather than corruption — stop iterating *before*
        it, so the caller's cursor lands at the start of that record and a
        later re-read observes the completed one once the writer's
        `append()` (one `write()` call, fsynced before it returns)
        finishes. `tolerate_torn_tail_from` is meant to be the manifest's
        own applied event-log offset — the same value
        `trim_torn_tail(applied_offset)` uses for a writer — so a torn
        record starting *before* it is damage to a record some generation
        has already applied, never forgiven regardless of where the file
        now ends: an attacker (or a corruption sweep) that truncates
        trailing bytes after tampering with an old record cannot fake an
        in-flight tail merely by making the damage land at the new
        end-of-file. `None` (the default) withholds tolerance entirely, so
        any tail defect raises — the pre-extension, strict reading. Only
        `Store.__init__`'s read-only path ever passes a non-`None` value,
        and only while a writer could plausibly still be appending (see
        `Store._reader_torn_tail_floor`) — a writer trims a genuinely torn
        tail during `_recover` (`trim_torn_tail`, D-086) before ever
        reaching a live `batches_from` call, so anything still torn there
        is corruption, not an in-flight write, and must keep raising.
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
                start = f.tell()
                raw = f.readline()
                if not raw:
                    return
                if not raw.strip():
                    continue
                end = f.tell()
                parse_error: json.JSONDecodeError | None = None
                batch: dict[str, Any] | None = None
                if raw.endswith(b"\n"):
                    try:
                        batch = json.loads(raw)
                    except json.JSONDecodeError as e:
                        parse_error = e
                if parse_error is not None or not raw.endswith(b"\n"):
                    if (tolerate_torn_tail_from is not None
                            and start >= tolerate_torn_tail_from):
                        size = f.seek(0, 2)
                        if end >= size:
                            return  # in-flight write, not yet committed
                    if parse_error is not None:
                        raise StateError(
                            f"event log {self.path} is not readable at offset "
                            f"{start}: {parse_error} — the replay cursor may "
                            f"not be on a record boundary"
                        ) from None
                    raise StateError(
                        f"event log {self.path} record at offset {start} has "
                        f"no terminating newline and is not the log's last "
                        f"record — the replay cursor may not be on a record "
                        f"boundary"
                    )
                assert batch is not None
                yield batch, end, raw

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

    def last_tt(self, *, tolerate_torn_tail_from: int | None = None) -> int:
        """Transaction time of the last batch (0 if empty).

        Linear scan; fine at research scale. TODO(phase3): tail-seek.

        `tolerate_torn_tail_from`: forwarded to `batches_from` — see there.
        Only `Store.__init__`'s read-only path passes a non-`None` value.
        """
        last = 0
        for batch in self.batches(tolerate_torn_tail_from=tolerate_torn_tail_from):
            last = batch["tt"]
        return last

    # --- read-only inspection (A3: `tgms check`) --------------------------- #
    #
    # `batches_from` raises on the first thing it cannot parse, which is right
    # for recovery and for replay — those callers must not proceed past
    # damage. An integrity checker needs the opposite: keep walking, and
    # report each defect with its offset, because the operator's question is
    # "what is wrong with this log", not "may I read record 4". `walk` is that
    # second reading of the same bytes. Neither function writes, truncates or
    # recovers: a torn tail is a *finding* here, never a repair.

    def walk(self) -> Iterator[tuple[int, int, bytes, dict[str, Any] | None, str | None]]:
        """Every record after the header, as
        `(start, end, raw, parsed_or_None, defect_or_None)`.

        `defect` is `None` for a sound record, and otherwise one of:

        * `"unparseable"` — the bytes are not JSON, or not an object with the
          fields a batch record carries;
        * `"unterminated"` — the record does not end in a newline, so the
          writer died mid-append;
        * `"id-mismatch"` — it parses, but its `batch_id` is not the hash of
          its own `(tt, ops)`. `append` computes that id from the content, so
          a disagreement means the content changed after it was written.

        Nothing is raised. A caller that wants the strict reading still has
        `batches_from`.
        """
        with open(self.path, "rb") as f:
            f.readline()  # the header record, outside the chain
            while True:
                start = f.tell()
                raw = f.readline()
                if not raw:
                    return
                if not raw.strip():
                    continue
                end = f.tell()
                if not raw.endswith(b"\n"):
                    yield start, end, raw, None, "unterminated"
                    continue
                try:
                    batch = json.loads(raw)
                    expect = sha256_hex(canonical_json(
                        {"tt": batch["tt"], "ops": batch["ops"]}))[:16]
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                    yield start, end, raw, None, "unparseable"
                    continue
                if not isinstance(batch, dict):
                    yield start, end, raw, None, "unparseable"
                    continue
                defect = None if batch.get("batch_id") == expect else "id-mismatch"
                yield start, end, raw, batch, defect

    def check_chain(self, applied_offset: int | None = None,
                    applied_chain: str | None = None) -> list[dict[str, Any]]:
        """Walk the log and report every framing or chain defect, read-only.

        Returns a list of `{kind, offset, detail}` dicts — the raw material
        for an integrity report's `eventlog` layer, kept free of that
        report's own schema so this module stays a log module.

        Three families of defect:

        * **framing**, per record, straight off `walk`. A defect whose bytes
          run to end-of-file is reported as `torn-tail` rather than
          `record-*`: that is the shape a crash mid-append leaves, it breaks
          no acknowledged write (`append` fsyncs and *then* returns), and
          `trim_torn_tail` exists to remove it. It is still a finding —
          verify reports what it found and repairs nothing — but naming it
          precisely is what lets the operator tell a survivable crash from
          damage in the middle of the history.
        * **tt monotonicity** across records, the same rule `replay`
          enforces.
        * **the applied-prefix cursor**, when the caller passes the manifest's
          `(offset, chain)`. The offset must land exactly on a record
          boundary and at or before end-of-file, and the rolling chain over
          the records ending at or before it must equal what the manifest
          recorded. A rewritten record in the *middle* of the applied prefix
          fails this even when every later record is intact — which is
          precisely the tamper the per-record `batch_id` alone would let
          through if the rewrite also fixed the id.
        """
        out: list[dict[str, Any]] = []
        size = self.size()
        try:
            head = self.header()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            out.append({"kind": "header-unreadable", "offset": 0,
                        "detail": f"the event log's header record is unreadable: {e}"})
            return out
        if head.get("format") != HEADER["format"]:
            out.append({"kind": "header-unreadable", "offset": 0,
                        "detail": f"not a tgms event log: header says {head.get('format')!r}"})
            return out

        boundaries: dict[int, str] = {0: SEED_CHAIN}
        chain = SEED_CHAIN
        prev_tt = 0
        sound = True
        for start, end, raw, batch, defect in self.walk():
            if defect is not None:
                sound = False
                if end >= size:
                    out.append({
                        "kind": "torn-tail", "offset": start,
                        "detail": f"the last record at offset {start} is {defect}: a "
                                  f"crash mid-append left a tail that was never "
                                  f"acknowledged"})
                else:
                    out.append({
                        "kind": f"record-{defect}", "offset": start,
                        "detail": f"the record at offset {start} is {defect}, and it is "
                                  f"not the log's last record"})
                continue
            chain = extend_chain(chain, raw)
            boundaries[end] = chain
            tt = batch["tt"]
            if tt <= prev_tt:
                out.append({"kind": "tt-non-monotonic", "offset": start,
                            "detail": f"the record at offset {start} carries tt {tt} "
                                      f"after {prev_tt}"})
            prev_tt = tt

        if applied_offset is None:
            return out
        applied_offset = int(applied_offset)
        if applied_offset > size:
            out.append({"kind": "cursor-past-end", "offset": applied_offset,
                        "detail": f"the manifest's applied cursor is at offset "
                                  f"{applied_offset}, past the log's {size} bytes"})
            return out
        if applied_offset not in boundaries:
            # A cursor that misses a boundary because an earlier record was
            # torn is a consequence of that, not a second defect.
            if sound:
                out.append({"kind": "cursor-not-on-boundary", "offset": applied_offset,
                            "detail": f"the manifest's applied cursor {applied_offset} is "
                                      f"not a record boundary of this log"})
            return out
        if applied_chain and boundaries[applied_offset] != applied_chain:
            out.append({
                "kind": "chain-mismatch", "offset": applied_offset,
                "detail": f"the log prefix applied at offset {applied_offset} hashes to "
                          f"{boundaries[applied_offset]} but the manifest records "
                          f"{applied_chain} — a record inside the applied prefix has "
                          f"been rewritten"})
        return out


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
