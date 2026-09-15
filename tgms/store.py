"""Store facade: public write API over (clock, write-ahead event log, adapter).

Single-writer assumption (spec §1): one ingestion process at a time.
Every public mutating call is one write batch: the batch is appended to the
event log first (write-ahead), then applied to the backend at the same tt.
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from tgms.core.clock import HybridLogicalClock
from tgms.core.errors import StateError, TgmsError
from tgms.core.model import OPEN_END, EntityRef, Props
from tgms.storage.base import StorageAdapter, make_op
from tgms.storage.crashpoint import crash_point
from tgms.storage.eventlog import EventLog, extend_chain

INGEST_CHUNK = 50_000

#: B5/F2: the single-writer rule (spec §1) was a convention nowhere enforced
#: — a second writer process previously raced `_recover`/`_write` silently,
#: which is exactly the failure `docs/eval_concurrency.md` §19 fixed for
#: *readers* opening mid-commit but never closed for a second *writer*. This
#: file, held with `fcntl.flock(LOCK_EX | LOCK_NB)` for the process's whole
#: writer lifetime, makes a second writer fail fast and by name instead.
WRITER_LOCK_NAME = "writer.lock"

#: `_acquire_writer_lock`'s bounded retry (invariant 1.5's reader clause):
#: 20 x 5ms = at most 95ms of sleeping past the first failed attempt,
#: comfortably longer than a reader's own momentary `_writer_lock_is_held`
#: probe can plausibly hold this lock, while still refusing a genuine
#: second writer within a fraction of a second, not silently.
_WRITER_LOCK_RETRY_ATTEMPTS = 20
_WRITER_LOCK_RETRY_DELAY_S = 0.005


class WriterLockedError(StateError):
    """Another process already holds `<store>/writer.lock`."""


class Store:
    def __init__(self, path: str | Path, backend: str | None = None,
                 paranoid: bool = False, read_only: bool = False) -> None:
        self.path = Path(path)
        #: Read-only handles never publish a generation: no recovery, no
        #: writes. This is the mode a *reader* process must use against a
        #: store some other process is writing — see `open`.
        self.read_only = read_only
        if read_only and not self.path.exists():
            raise StateError(f"no store to open read-only at {self.path}")
        self.path.mkdir(parents=True, exist_ok=True)
        backend = backend or detect_backend(self.path)
        self.backend = backend
        self.eventlog = EventLog(self.path / "eventlog.jsonl")
        self.adapter = _make_adapter(backend, self.path, self.read_only)
        self.adapter.paranoid = paranoid
        #: Rolling chain over the log prefix the backend has applied; None on
        #: backends without a cursor and on legacy stores until their next
        #: write (which pays a one-time full-prefix hash to start the chain).
        self._chain: str | None = None
        #: One write batch at a time. The engine is single-writer by design
        #: (D-028) and says so by refusing a nested batch, but two *threads*
        #: in one process used to interleave `_write` and surface that as
        #: "transaction time must advance ... this is an engine bug", losing
        #: writes to a message that blames the wrong layer. Serializing here
        #: costs an uncontended lock against a ~34 ms commit; callers that
        #: want concurrency without the serialization want
        #: `tgms.write.GroupCommitWriter`, which coalesces instead.
        self._write_lock = threading.Lock()
        #: The OS-level single-writer lock's held file handle, or None for a
        #: reader (readers never take this lock) or before it is acquired.
        self._writer_lock_fh: Any | None = None
        if not read_only:
            # Acquired *before* `_recover()`, which is a writer's act (see
            # `_recover`'s own docstring) and must never run concurrently in
            # two processes — the lock is what makes that "must never" true
            # rather than merely documented.
            self._acquire_writer_lock()
            self._recover()
        #: Invariant 1.5, extended to readers — computed once, up front
        #: (cheap: reads the already-loaded manifest cursor, no lock probe),
        #: and reused by every full-log scan this `__init__` runs before it
        #: ever reaches `_seed_frontier`. See `_compute_reader_torn_tail_
        #: floor`'s own docstring for what this offset means and why it is
        #: `None` far more often than "any read-only handle".
        self._reader_torn_tail_floor: int | None = self._compute_reader_torn_tail_floor()
        self.clock = HybridLogicalClock(
            last_tt=self.eventlog.last_tt(
                tolerate_torn_tail_from=self._reader_torn_tail_floor,
                writer_active=self._writer_lock_is_held))
        #: False when this handle could not establish its frontier against the
        #: **applied** prefix — see `_seed_frontier`. Rides into the dependency
        #: scope as `tt_q_verified`, never as a flat envelope key.
        self.frontier_verified = True
        self._store_identity: str | None = None
        self._seed_frontier()
        self._memories: list[Any] = []  # EvolutionMemory hooks (spec v1.1 WP2.4)
        #: `ingest_events`'s default-`disc` offset base, carried **across**
        #: top-level calls on this instance (never reset per call) — see
        #: `ingest_events`'s own docstring for the collision this prevents.
        self._ingest_offset_base = 0

    # --- the read basis (M2.1; FRESHNESS_SEMANTICS D13.16) ---------------- #

    def _seed_frontier(self) -> None:
        """Tell the adapter which transaction times it is already serving.

        `apply_ops` maintains the frontier for everything this process applies,
        but a store opened on existing state has applied nothing yet, so the
        frontier has to be seeded — and **not** from `self.clock.last_tt`.
        The clock is seeded from the log's tail, the log is fsynced *before* the
        batch is applied, and a read-only handle therefore inherits a frontier
        strictly ahead of what it is being served. That is `tt_q` rounded **up**,
        which FRESHNESS_SEMANTICS D13.17 forbids by name as a false-freshness
        hazard.

        So the seed is the **applied** prefix's own tt, read Python-side from
        the backend's event cursor: `(offset, chain)` names the log prefix the
        current generation applied, and the last record ending at or before
        `offset` carries the tt that prefix reaches. Exact, and no PyO3 accessor
        (`Manifest.created_tt` is still not exposed — deferred to M4).

        A backend that keeps no cursor — DuckDB, Kuzu, or a native store written
        before cursors existed, which reports chain `""` — cannot answer the
        question at all. Such a handle falls back to the log's tail and records
        `frontier_verified = False`, which travels as `tt_q_verified: false`
        inside the dependency scope so a reader knows the value was not rounded
        down against anything.
        """
        cursor = getattr(self.adapter, "event_cursor", None)
        if cursor is not None:
            offset, chain = cursor()
            if chain:
                self.adapter.note_frontier_tt(self._tt_at_offset(int(offset)))
                return
        self.frontier_verified = False
        self.adapter.note_frontier_tt(
            self.eventlog.last_tt(tolerate_torn_tail_from=self._reader_torn_tail_floor,
                                 writer_active=self._writer_lock_is_held))

    def _tt_at_offset(self, offset: int) -> int:
        """The tt of the last log record ending at or before `offset` (0 for an
        empty applied prefix).

        Walking from 0 to find it can read past the applied prefix itself
        (`end > offset` is only known once the next record's end is read) —
        for a read-only handle that next record can be one a live writer is
        mid-append on, which (invariant 1.5, extended to readers) is an
        in-flight write, not corruption, so `_reader_torn_tail_floor` and
        `_writer_lock_is_held` are threaded through. A writer reaches this
        only after `_recover()` has already trimmed any genuinely torn
        tail, so it keeps the strict reading (`_reader_torn_tail_floor` is
        `None` for a writer) — anything still torn there is corruption.
        """
        tt = 0
        for batch, end, _raw in self.eventlog.batches_from(
                0, tolerate_torn_tail_from=self._reader_torn_tail_floor,
                writer_active=self._writer_lock_is_held):
            if end > offset:
                break
            tt = batch["tt"]
        return tt

    def _compute_reader_torn_tail_floor(self) -> int | None:
        """The manifest's applied event-log offset — the same value
        `trim_torn_tail(applied_offset)` uses for a writer — for a
        *read-only* handle's torn-tail tolerance (invariant 1.5's reader
        clause). Passed to `EventLog.batches_from`'s `tolerate_torn_tail_from`
        by every full-log scan this handle runs; `None` withholds tolerance
        entirely, so any tail defect raises exactly as before this
        invariant was extended to readers. `None` in either of these cases:

        * a writer (`not self.read_only`) — recovery already trims a
          genuinely torn tail in `_recover()`, run above, before this is
          ever computed; anything still torn past that point is corruption;
        * no cursor to trust — a backend that keeps none, or a legacy store
          (`chain == ""`) that predates cursors; there is then no applied
          offset to compare against, so nothing is guessed.

        Deliberately cheap and lock-free: this reads only the manifest
        cursor this handle's adapter already loaded at open, never
        `writer.lock`. Whether a torn record found at/after this floor is
        actually forgiven is decided later, lazily, by `batches_from`
        itself calling `_writer_lock_is_held` — see that method's
        docstring for why the probe must not run here, unconditionally, on
        every open.
        """
        if not self.read_only:
            return None
        cursor = getattr(self.adapter, "event_cursor", None)
        if cursor is None:
            return None
        offset, chain = cursor()
        if not chain:
            return None
        return int(offset)

    def _writer_lock_is_held(self) -> bool:
        """Best-effort, non-blocking probe of `<store>/writer.lock`: True
        iff some *other* process currently holds the OS-level single-writer
        lock (`_acquire_writer_lock`). A reader never takes this lock
        itself (only `read_only=False` does, above), so a probe that
        acquires it uncontended proves no writer is running right now —
        released immediately, since this handle has no business holding
        it — and one that fails proves a writer is.

        **Called lazily, not at open.** Passed to `EventLog.batches_from`
        as its `writer_active` callback and invoked at most once per scan,
        and only when a torn record is actually found at or past
        `_reader_torn_tail_floor` and running to the file's true end — the
        one moment the answer matters. Calling this unconditionally on
        every read-only open (the shape this invariant's fix originally
        shipped in) opened a real hazard: the soak's reader pool reopens
        every few minutes and the OSV daily queries loop opening readers,
        so a writer's own `_acquire_writer_lock` (`LOCK_EX | LOCK_NB`,
        no retry at the time) could land its trylock in the same instant a
        reader's probe held the lock for a moment and see a spurious
        `WriterLockedError` — one process refusing to become the writer
        because of a lock a mere *reader* was, correctly but coincidentally,
        also holding for a heartbeat. Confining the probe to actual
        torn-tail encounters — rare, since almost no open ever meets one —
        shrinks that window from "every read-only open" to "the same open
        already mid-recovery-adjacent bookkeeping a torn tail forces
        anyway"; `_acquire_writer_lock`'s own bounded retry closes the
        remaining sliver.
        """
        lock_path = self.path / WRITER_LOCK_NAME
        if not lock_path.exists():
            return False
        import fcntl

        fh = io.open(lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            fh.close()

    def frontier_tt(self) -> int:
        """The belief-time frontier this handle serves reads from."""
        return self.adapter.frontier_tt()

    @property
    def store_identity(self) -> str:
        """D13.2's `store` — the identity `⊎` refuses to union across.

        `digest(header record ‖ first batch record)`: stable across every replay
        of one history, distinct between stores, and available without reading
        the backend. A log with no batches yet has no identity to state and
        takes the `"unanchored"` sentinel until its first write, so the value is
        re-derived while it is still unanchored.
        """
        from tgms.tgir.depscope import UNANCHORED, store_identity  # local: import cost

        if self._store_identity in (None, UNANCHORED):
            self._store_identity = store_identity(
                self.eventlog.header(), self.eventlog.first_batch())
        return self._store_identity

    def _recover(self) -> None:
        """Apply the event-log suffix the backend has not seen (D-042).

        The write path is write-ahead: a crash between the log fsync and the
        backend commit leaves a durable record with no store state. Backends
        that record a replay cursor (the native engine) recover here by
        replaying exactly the un-applied suffix. The rules, in order of
        distrust: a cursor the log cannot account for (past its end, off a
        record boundary, or with a chain mismatch) is corruption and raises
        loudly; a *missing* cursor (legacy store, chain "") recovers nothing
        — it cannot know what was applied — and upgrades at the next write;
        an accounted cursor short of the log replays forward, re-failing
        failed batches deterministically, exactly like full replay.

        **Recovery is a writer's act**, which is why `read_only` skips it
        entirely. A live writer is *always* in the state this reads as a
        crash — the log is fsynced before the batch is applied, so for the
        whole duration of every commit the log is ahead of the manifest.
        A reader opening in that window used to replay the suffix and publish
        a generation of its own, concurrently with the writer publishing the
        same generation number: two writers, overwriting each other's segment
        files under the mmap of anyone already reading them.

        **Killable while it runs (Lane A EXP-A2):** this method is itself on
        the crash-injection surface — `py_recover_after_trim`,
        `py_recover_before_cursor_publish`, `py_recover_after_cursor_publish`,
        and `py_recover_mid_replay` (`tgms/storage/crashpoint.py`, armed only
        under `TGMS_CRASH_POINT`) mark points inside the loop below. The
        contract under test is convergence, not just survival: killing
        recovery itself, restarting, and recovering again — repeatedly, at
        fresh random points — must still land on exactly the state a single
        uninterrupted recovery (equivalently, a clean replay of the same log
        into a fresh store) would produce. See `docs/eval_durability.md`
        (`--recovery-crash`) and `tests/test_crash_during_recovery.py`.
        """
        cursor = getattr(self.adapter, "event_cursor", None)
        if cursor is None:
            return  # backend keeps no cursor; recovery stays `tgms replay`
        offset, chain = cursor()
        if chain == "":
            return  # legacy store: no cursor was ever recorded (see above)
        size = self.eventlog.size()
        if offset > size:
            raise StateError(
                f"replay cursor is ahead of the event log: the manifest says "
                f"{offset} bytes were applied but {self.eventlog.path} holds "
                f"{size} — the log was truncated or belongs to a different "
                f"store; refusing to guess. Restore the full log or rebuild "
                f"the store with `tgms replay`."
            )
        # verify the applied prefix is the prefix the cursor was cut from;
        # chain_of_prefix also rejects an offset that is no record boundary
        got = self.eventlog.chain_of_prefix(offset)
        if got != chain:
            raise StateError(
                f"replay cursor chain mismatch at offset {offset} of "
                f"{self.eventlog.path}: manifest records {chain}, log yields "
                f"{got} — the applied prefix was rewritten; refusing to "
                f"replay onto it. Rebuild the store with `tgms replay`."
            )
        self._chain = chain
        # a crash mid-append can leave a torn final record; it was never
        # acknowledged, so recovery trims it before replaying the suffix
        # (D-086). Anything torn that is *not* the tail keeps failing loudly
        # in the replay loop below.
        trimmed = self.eventlog.trim_torn_tail(offset)
        if trimmed is not None:
            size = self.eventlog.size()
        # recovery-crash injection point (Lane A EXP-A2): the tail is now
        # sound (trimmed or never torn) but nothing in the suffix has been
        # replayed yet. Killing here and reopening must re-derive exactly
        # this same starting point — same trim decision, same offset — since
        # nothing about it was durable to begin with.
        crash_point("py_recover_after_trim")
        if offset == size:
            return  # clean shutdown: nothing to do
        for batch, end, raw in self.eventlog.batches_from(offset):
            self._chain = extend_chain(self._chain, raw)
            self.adapter.begin()
            try:
                self.adapter.apply_ops(batch["ops"], batch["tt"])
            except TgmsError:
                # failed on the live path, fails identically here; the next
                # successful commit's cursor covers the skipped record
                self.adapter.rollback()
                continue
            # recovery-crash injection points (Lane A EXP-A2): the native
            # engine stages the cursor in-memory (`note_event_cursor` ->
            # `NativeStore.set_event_cursor`, PyO3) and only makes it (and
            # the replayed rows) durable in the single atomic manifest swap
            # `commit()` performs — there is no separate "cursor durable"
            # moment from "batch durable"; both land in one generation. So
            # `before`/`after_cursor_publish` bracket the staging call
            # itself, entirely before that commit: a crash at either one
            # leaves the on-disk manifest completely untouched (the engine's
            # `pending_cursor` is process memory, discarded on death), so the
            # next recovery attempt redoes this exact batch from the same
            # starting cursor — identically to a crash before `apply_ops`
            # ever ran.
            crash_point("py_recover_before_cursor_publish")
            self.adapter.note_event_cursor(end, self._chain)
            crash_point("py_recover_after_cursor_publish")
            self.adapter.commit()
            # `py_recover_mid_replay` fires after the atomic commit above has
            # made this batch *and* its cursor durable as a new generation,
            # but before the loop advances to whatever the suffix holds
            # next. A restart here must resume cleanly from the new cursor:
            # either recovery is a no-op (this was the last un-applied
            # batch) or it replays the remaining suffix — never re-applying
            # what this commit already published.
            crash_point("py_recover_mid_replay")

    def close(self) -> None:
        self.adapter.close()
        self._release_writer_lock()

    # --- OS-level single-writer lock (B5/F2) ------------------------------- #

    def _acquire_writer_lock(self) -> None:
        """`fcntl.flock(LOCK_EX | LOCK_NB)` on `<store>/writer.lock`, retried
        briefly before refusing.

        Advisory and tied to the open file description, not a lock file
        whose mere *presence* is checked: a crashed writer's lock is
        released by the OS the moment its process exits (or the fd is
        otherwise closed), so there is no stale-lock cleanup step and no
        window where a dead writer's lock file wrongly blocks a new one.
        Two `Store` objects racing for it *within* one process are refused
        exactly like two processes would be — `flock` locks are per open
        file description, not per process — which is the conservative
        (and correct) reading of "one writer."

        The retry exists for a narrower reason than contention with a real
        second writer: a read-only handle's `_writer_lock_is_held` probe
        (invariant 1.5's reader clause, `EventLog.batches_from`'s
        `writer_active` callback) also trylocks this same file, for a
        moment, non-blocking on its own side. Without a retry here, a
        writer opening in that instant would see `WriterLockedError` from a
        process that was never going to write anything — a soak with eight
        readers reopening every few minutes, or the OSV daily queries
        looping reader opens, makes that instant likely enough to hit in
        practice, not merely a theoretical race. `_RETRY_ATTEMPTS` tries at
        `_RETRY_DELAY_S` apart bound the total added latency to a genuinely
        contended open (`(_RETRY_ATTEMPTS - 1) * _RETRY_DELAY_S`, comfortably
        under a reader probe's own hold time by orders of magnitude) while
        still failing a *real* second writer within that same short window,
        never silently.
        """
        import fcntl

        # `io.open`, not the bare builtin: this module's own `open()`
        # (the store-opening function below) shadows the builtin at module
        # scope, and a bare `open(lock_path, "a+")` here would resolve to
        # *that* — this is not hypothetical, it broke exactly this way
        # during development.
        lock_path = self.path / WRITER_LOCK_NAME
        fh = io.open(lock_path, "a+")
        for attempt in range(_WRITER_LOCK_RETRY_ATTEMPTS):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if attempt + 1 < _WRITER_LOCK_RETRY_ATTEMPTS:
                    time.sleep(_WRITER_LOCK_RETRY_DELAY_S)
                    continue
                fh.seek(0)
                holder = fh.read().strip() or "unknown pid (race with the holder " \
                                              "writing it)"
                fh.close()
                raise WriterLockedError(
                    f"another process is already the writer for {self.path} "
                    f"(writer.lock held by pid {holder}); a second concurrent "
                    f"writer is undefined (spec §1). Open with read_only=True "
                    f"instead, or wait for that process to exit."
                ) from None
            else:
                break
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._writer_lock_fh = fh

    def _release_writer_lock(self) -> None:
        if self._writer_lock_fh is None:
            return
        import fcntl

        try:
            fcntl.flock(self._writer_lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._writer_lock_fh.close()
            self._writer_lock_fh = None

    def attach_memory(self, memory: Any) -> None:
        """Register an EvolutionMemory for staleness invalidation: correct()
        and retract() quarantine notes overlapping the affected vt extent."""
        self._memories.append(memory)

    def _invalidate_memories(self, vt_a: int, vt_e: int) -> None:
        for m in self._memories:
            m.mark_stale(vt_a, vt_e)

    # --- write API (WP1.2) ------------------------------------------------ #

    def assert_node(self, uid: str, label: str, props: Props | None = None,
                    vt_s: int = 0, vt_e: int = OPEN_END) -> int:
        return self._write([make_op("assert_node", uid=uid, label=label,
                                    props=props or {}, vt_s=vt_s, vt_e=vt_e,
                                    source="ingest", provenance_ref=None)])

    def assert_edge(self, src: str, dst: str, rel_type: str, props: Props | None = None,
                    vt_s: int = 0, vt_e: int = OPEN_END, disc: str = "") -> int:
        return self._write([make_op("assert_edge", src=src, dst=dst, rel_type=rel_type,
                                    props=props or {}, vt_s=vt_s, vt_e=vt_e, disc=disc,
                                    source="ingest", provenance_ref=None)])

    def retract(self, ref: EntityRef, t: int) -> int:
        tt = self._write([make_op("retract", ref=_ref_json(ref), t=t,
                                  source="ingest", provenance_ref=None)])
        # belief about [t, OPEN_END) changed: quarantine overlapping notes
        self._invalidate_memories(t, OPEN_END)
        return tt

    def correct(self, ref: EntityRef, new_props: Props,
                vt_s: int = 0, vt_e: int = OPEN_END) -> int:
        tt = self._write([make_op("correct", ref=_ref_json(ref), props=new_props,
                                  vt_s=vt_s, vt_e=vt_e,
                                  source="ingest", provenance_ref=None)])
        self._invalidate_memories(vt_s, vt_e)
        return tt

    def ingest_events(self, events: Iterable[dict[str, Any]],
                      node_label: str = "Node",
                      nodes: Iterable[dict[str, Any]] | None = None) -> int:
        """Bulk event-stream ingestion, chunked into write batches.
        Returns the tt of the last batch.

        `nodes` is the optional structured half: `{uid, label, props?, vt_s,
        vt_e?}` records that become real node versions with their own labels
        and properties, instead of the bare auto-created endpoints the event
        stream implies. It exists so a labelled, propertied load — LDBC SNB's
        eight node types, say — rides this path rather than
        `assert_node`-per-node, which writes one batch and one manifest each
        and costs O(N²) in both time and bytes.

        Nodes are written **before** events, in their own chunked batches: the
        event batches then see them as already known and skip auto-creating
        bare versions for the same uids, and a collision refuses loudly rather
        than at the end of a long load.

        **Default `disc`.** An event without an explicit `disc` gets one
        derived from its position in the bulk stream (`tgms/storage/base.py::
        _ingest_events`: `f"#{offset + i}"`), so it becomes its own logical
        edge. That position counter (`offset`) is kept on **this Store
        instance** — `self._ingest_offset_base` — and only ever advances, so
        it is stable across separate top-level `ingest_events` calls, not just
        across this call's own internal `INGEST_CHUNK` chunks. Before this
        counter existed, `offset` restarted at 0 on every top-level call: a
        caller that split one logical bulk load across several `ingest_events`
        calls (batching) got the same default `disc` values from each call,
        so distinct edges from different calls could collide into the same
        edge identity whenever they shared `(src, dst, rel_type)` — see
        `docs/STABILITY.md`'s dated note and `ops/failure_ledger.jsonl` for
        the incident this fixed. A single top-level call's own digest is
        unaffected: this instance's counter starts at 0, exactly the old
        per-call `offset`, so a store's first (or only) `ingest_events` call
        assigns identical `disc` values either way.
        """
        tt = self.clock.last_tt
        if nodes is not None:
            for chunk in _chunks(nodes, INGEST_CHUNK):
                tt = self._write([make_op("ingest_events", events=[], nodes=chunk,
                                          node_label=node_label,
                                          source="ingest", provenance_ref=None)])
        for chunk in _chunks(events, INGEST_CHUNK):
            tt = self._write([make_op("ingest_events", events=chunk,
                                      offset=self._ingest_offset_base,
                                      node_label=node_label,
                                      source="ingest", provenance_ref=None)])
            self._ingest_offset_base += len(chunk)
        return tt

    def _write(self, ops: list[dict[str, Any]]) -> int:
        """Write-ahead: the batch is logged before it is applied. If apply
        fails, the backend rolls back; replay skips the batch identically
        (apply is deterministic), so log and store never diverge.

        On cursor-keeping backends the commit also records how far into the
        log this batch reaches (offset past its newline, rolling chain), so
        a crash after the append recovers by suffix replay (`_recover`)."""
        if self.read_only:
            raise StateError(
                f"{self.path} is open read-only: this handle cannot write. "
                f"Reopen without read_only=True — and only from the single "
                f"writer process, since a second writer is undefined."
            )
        with self._write_lock:
            return self._write_locked(ops)

    def _write_locked(self, ops: list[dict[str, Any]]) -> int:
        tt = self.clock.tick()
        _batch_id, end_offset, record = self.eventlog.append(tt, ops)
        # durability-injection point (D-086): fires only under
        # TGMS_CRASH_POINT=py_after_wal_fsync — the log record above is
        # fsynced and durable, but nothing has been applied to the backend
        # yet, so recovery must resurrect this batch by suffix replay alone.
        crash_point("py_after_wal_fsync")
        note_cursor = getattr(self.adapter, "note_event_cursor", None)
        if note_cursor is not None:
            if self._chain is None:
                # legacy store's first write since cursors exist: start the
                # chain by hashing the whole applied prefix once (everything
                # before this record — the store predates cursor recording,
                # so its manifest vouches for the prefix, not the chain)
                self._chain = self.eventlog.chain_of_prefix(
                    end_offset - len(record))
            # the chain covers log bytes, failed batches included — extend
            # unconditionally; the cursor is staged only on success below
            self._chain = extend_chain(self._chain, record)
        self.adapter.begin()
        try:
            self.adapter.apply_ops(ops, tt)
        except TgmsError:
            self.adapter.rollback()
            raise
        if note_cursor is not None:
            note_cursor(end_offset, self._chain)
        # durability-injection point (D-086): fires only under
        # TGMS_CRASH_POINT=py_before_engine_commit — apply_ops has mutated
        # the backend's in-memory/staged state but the engine commit that
        # would make it durable never runs.
        crash_point("py_before_engine_commit")
        self.adapter.commit()
        # this handle applied the batch itself, so its frontier is established
        # by observation from here on, whatever it could establish at open
        self.frontier_verified = True
        return tt

    # --- freshness (M4.4; FRESHNESS_SEMANTICS D13.24) ----------------------- #

    def check_scope(self, scope: Any, *, tt_now: int = OPEN_END,
                    chain_cache: Any = None) -> Any:
        """*"Could anything written since have changed this?"* — asked of a
        stored `DependencyScope`, answered without recomputing anything.

        Thin by design: it supplies this store's event log and forwards. **No
        new state, no persistence, and no envelope key** — a verdict is computed
        on demand and stored nowhere, which is what keeps every comparator's
        shape and every frozen digest untouched by this milestone.

        `tt_now` defaults to `OPEN_END`, i.e. scan the whole suffix. The
        rounding direction is the *opposite* of `tt_q`'s (D-M4a): the log is
        fsynced before apply, so it leads the frontier, and passing this
        store's frontier here would exclude batches every recomputing reader
        can already see. A caller passing a smaller `tt_now` is asking an
        "as of" question and owns it.

        Accepts a `DependencyScope` or the JSON object off an envelope's
        `dependency` key.
        """
        from tgms.tgir.check import check

        return check(scope, self.eventlog, tt_now, chain_cache=chain_cache)

    def check_result(self, envelope: dict[str, Any], *, tt_now: int = OPEN_END,
                     chain_cache: Any = None) -> Any:
        """The same question asked of a result envelope, which carries its own
        scope on `dependency` (D13.19).

        An envelope with no `dependency` is one produced before M2.1 placed the
        key, or by a path that bypassed `envelope_metadata`. There is no basis
        to compare against, so it refuses — never `FRESH`.
        """
        from tgms.tgir.check import UNDECIDABLE

        scope = envelope.get("dependency")
        if not isinstance(scope, dict):
            return UNDECIDABLE("no-tt_q")
        return self.check_scope(scope, tt_now=tt_now, chain_cache=chain_cache)

    def check_trace(self, record: dict[str, Any], *, tt_now: int = OPEN_END,
                    chain_cache: Any = None) -> Any:
        """A saved plan/trace record — where the interesting answer is.

        Each step is checked against **its own scope and its own `tt_q`** and
        the results fold (D-M4e), so the verdict carries per-step attribution
        alongside the one bit D5.4 says a plan reports.
        """
        from tgms.tgir.check import check_trace

        return check_trace(record, self.eventlog, tt_now, chain_cache=chain_cache)

    # --- introspection ------------------------------------------------------ #

    def digest(self) -> str:
        return self.adapter.store_digest()

    def stats(self) -> dict[str, Any]:
        return self.adapter.stats()


#: Backend used for new stores (D-028). Existing stores keep the backend they
#: were written with — see `detect_backend`.
DEFAULT_BACKEND = "native"


def detect_backend(path: Path) -> str:
    """Which backend an existing store at `path` uses, else the default.

    Flipping the default to the native engine must not strand data that is
    already on disk: opening an existing DuckDB store without this check would
    silently create an empty native store beside it and look like data loss.
    Layout is self-identifying, so no migration or marker file is needed —
    pass `backend=` explicitly to override.
    """
    if (path / "store.duckdb").exists():
        return "duckdb"
    if (path / "store.kuzu").exists():
        return "kuzu"
    return DEFAULT_BACKEND


def open(path: str | Path, backend: str | None = None, paranoid: bool = False,
         read_only: bool = False) -> Store:
    """Open (or create) a store. `backend` defaults to the existing store's
    layout, or `DEFAULT_BACKEND` for a new one.

    `read_only=True` is the mode for a **reader process**: it skips crash
    recovery and refuses the write API, so the handle never publishes a
    generation. Use it for every process that is not the single writer.
    A default (read-write) handle recovers the event-log suffix on open,
    which is correct for the writer and is a second writer for anyone else —
    a live writer spends every commit in the state recovery reads as a crash.

    A read-only handle still answers every query at full speed and still
    pins its manifest generation for as long as it is open; it simply never
    advances one. It may still write the derived TCSR permutation cache
    (`index/`, saved atomically and disposable), which is not store state.
    """
    return Store(path, backend=backend, paranoid=paranoid, read_only=read_only)


def _make_adapter(backend: str, path: Path, read_only: bool = False) -> StorageAdapter:
    if backend == "native":
        # No OS-level lock to contend with: the native engine's on-disk
        # format is immutable segments plus an atomic manifest swap, so a
        # second reader never blocks on a first (verified empirically — see
        # test_concurrency.py). `NativeAdapter` takes no read_only parameter;
        # Store's own write refusal (read_only) is what makes this handle a
        # reader.
        from tgms.storage.native import NativeAdapter
        return NativeAdapter(path / "native")
    if backend in ("duckdb", "memory"):
        DuckDBAdapter = _optional_backend("duckdb")
        if backend == "memory":
            # DuckDB cannot open `:memory:` read-only at all; Store-level
            # write refusal still applies to a read-only in-memory handle.
            return DuckDBAdapter(":memory:")
        return DuckDBAdapter(path / "store.duckdb", read_only=read_only)
    if backend == "kuzu":
        return _optional_backend("kuzu")(path / "store.kuzu", read_only=read_only)
    raise ValueError(f"unknown backend: {backend}")


#: Backends that ship as optional extras, not runtime dependencies (D-029).
_OPTIONAL_BACKENDS = {
    "duckdb": ("tgms.storage.duckdb_adapter", "DuckDBAdapter"),
    "kuzu": ("tgms.storage.kuzu_adapter", "KuzuAdapter"),
}


def _optional_backend(name: str):
    """Import a backend that ships as an optional extra.

    Neither third-party engine is a runtime dependency any more (D-029): the
    native engine is the default and needs nothing beyond the wheel. An
    existing store on one of them still opens — `detect_backend` reads its
    layout — but only if the extra is installed, so say so plainly instead of
    surfacing a bare ImportError from deep in an adapter.
    """
    import importlib

    module, cls = _OPTIONAL_BACKENDS[name]
    try:
        return getattr(importlib.import_module(module), cls)
    except ImportError as e:  # pragma: no cover - depends on the install
        raise ImportError(
            f"this store uses the {name} backend, which is now an optional "
            f"extra: install it with `pip install tgms[{name}]`, or migrate "
            f"the store to the native engine with `tgms replay`."
        ) from e


def _ref_json(ref: EntityRef) -> dict[str, Any]:
    if ref.kind == "node":
        return {"kind": "node", "uid": ref.uid}
    return {"kind": "edge", "src": ref.src, "dst": ref.dst,
            "rel_type": ref.rel_type, "disc": ref.disc}


def _chunks(it: Iterable[Any], n: int) -> Iterator[list[Any]]:
    buf: list[Any] = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
