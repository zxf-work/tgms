"""Group commit: concurrent single writes share one durable generation.

A commit publishes a durable generation, which costs four fsynced file writes
(segment, dictionary tail, manifest, `CURRENT`) — about 34 ms on the measured
host, near enough independent of how many rows the batch carries. That is the
design, not waste: `engine_lessons.md` §7 is explicit that the lever is
batching at the layer that decides what a batch *is*, never relaxing what a
batch *means*.

Bulk paths already batch. The workload with no answer was the one where the
rows arrive from callers that cannot batch on their own: several threads, each
holding one row, each calling `assert_edge`. Every one of those was its own
generation, so N concurrent single-row writers cost N times the fsync floor
and got nothing for it.

This coalesces them. Submitters hand their op to a queue and block; one
committer thread drains whatever is queued, applies it as a single batch, and
publishes one generation for all of it.

**The durability contract is unchanged.** A submit returns only after the
commit containing it has published — its generation is on disk, fsyncs and
all. Nothing here weakens, defers or batches an fsync that was not already
going to happen; it reduces how many commits there are, not what a commit
costs.

**The replay contract (D-042) is unchanged.** A coalesced batch is one
event-log record holding its submissions in queue order, exactly like a
chunk of `ingest_events`, so it replays as one batch and advances the cursor
once. If the coalesced apply fails, the group is rolled back and each
submission is retried on its own — the coalesced record stays in the log as a
failed batch, which replay re-fails and skips deterministically, and the
individual records that follow replay exactly as they ran. One caller's bad
op therefore fails one caller, never the group.

By default the committer does not linger: it takes whatever is already queued
and goes. Coalescing then comes free from the commit's own duration — while
one commit is in flight, everything submitted behind it piles up for the next
— so a single writer pays only a queue hop and never waits on a timer.
`max_delay_s` exists for the case where you would rather wait than commit
alone; it trades latency for generations and is off unless you ask.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from tgms.core.errors import StateError, TgmsError
from tgms.core.model import OPEN_END, EntityRef, Props
from tgms.storage.base import make_op
from tgms.store import Store, _ref_json


class _Submission:
    __slots__ = ("op", "done", "tt", "error")

    def __init__(self, op: dict[str, Any]) -> None:
        self.op = op
        self.done = threading.Event()
        self.tt: int | None = None
        self.error: BaseException | None = None


class GroupCommitWriter:
    """Coalescing front end for one `Store`. One per store, one writer.

    Only the committer thread touches the store, so the store itself needs no
    locking and keeps its single-writer contract exactly. Submitting threads
    touch nothing but a queue.
    """

    def __init__(self, store: Store, max_delay_s: float = 0.0,
                 max_batch: int = 1000) -> None:
        if store.read_only:
            raise StateError("a read-only store cannot be written through "
                             "GroupCommitWriter")
        self.store = store
        self.max_delay_s = float(max_delay_s)
        self.max_batch = int(max_batch)
        self._q: queue.Queue[_Submission | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        #: How the coalescing actually went, so "it batched" is a measurement
        #: rather than an assumption: commits made, submissions served, and
        #: the largest group one commit carried.
        self.commits = 0
        self.submissions = 0
        self.max_group = 0
        self.solo_fallbacks = 0

    # --- lifecycle ------------------------------------------------------- #

    def start(self) -> GroupCommitWriter:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="tgms-group-commit",
                                        daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Drain everything queued, then stop. Submissions already accepted
        are committed — closing is not a way to lose a durable promise."""
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._q.put(None)
            self._thread.join()
            self._thread = None

    def __enter__(self) -> GroupCommitWriter:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- write API (mirrors Store, one op per call) ---------------------- #

    def assert_node(self, uid: str, label: str, props: Props | None = None,
                    vt_s: int = 0, vt_e: int = OPEN_END) -> int:
        return self.submit(make_op(
            "assert_node", uid=uid, label=label, props=props or {},
            vt_s=vt_s, vt_e=vt_e, source="ingest", provenance_ref=None))

    def assert_edge(self, src: str, dst: str, rel_type: str,
                    props: Props | None = None, vt_s: int = 0,
                    vt_e: int = OPEN_END, disc: str = "") -> int:
        return self.submit(make_op(
            "assert_edge", src=src, dst=dst, rel_type=rel_type,
            props=props or {}, vt_s=vt_s, vt_e=vt_e, disc=disc,
            source="ingest", provenance_ref=None))

    def retract(self, ref: EntityRef, t: int) -> int:
        return self.submit(make_op("retract", ref=_ref_json(ref), t=t,
                                   source="ingest", provenance_ref=None))

    def correct(self, ref: EntityRef, new_props: Props, vt_s: int = 0,
                vt_e: int = OPEN_END) -> int:
        return self.submit(make_op("correct", ref=_ref_json(ref),
                                   props=new_props, vt_s=vt_s, vt_e=vt_e,
                                   source="ingest", provenance_ref=None))

    def submit(self, op: dict[str, Any]) -> int:
        """Queue one op and block until the generation containing it is on
        disk. Returns that generation's transaction time."""
        if self._closed or self._thread is None:
            raise StateError("GroupCommitWriter is not running; call start()")
        s = _Submission(op)
        self._q.put(s)
        s.done.wait()
        if s.error is not None:
            raise s.error
        assert s.tt is not None
        return s.tt

    # --- the committer --------------------------------------------------- #

    def _run(self) -> None:
        while True:
            first = self._q.get()
            if first is None:
                return
            group = [first]
            self._fill(group)
            self._commit_group(group)

    def _fill(self, group: list[_Submission]) -> None:
        """Take whatever is already queued (and, if asked, linger for more).

        With `max_delay_s == 0` this never waits: the coalescing comes from
        the commit's own duration, during which submissions pile up behind
        the queue. A writer that is alone therefore pays a queue hop and no
        timer, which is what keeps this from being a latency tax on the
        workload it cannot help.
        """
        deadline = None
        if self.max_delay_s > 0:
            import time
            deadline = time.perf_counter() + self.max_delay_s
        while len(group) < self.max_batch:
            try:
                if deadline is None:
                    nxt = self._q.get_nowait()
                else:
                    import time
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        break
                    nxt = self._q.get(timeout=left)
            except queue.Empty:
                break
            if nxt is None:                 # close(): stop growing, but the
                self._q.put(None)           # group we have still commits
                break
            group.append(nxt)

    def _commit_group(self, group: list[_Submission]) -> None:
        ops = [s.op for s in group]
        try:
            tt = self.store._write(ops)
        except TgmsError:
            # one caller's bad op must not fail the others: the group rolled
            # back as a unit, so replay each submission on its own. The
            # coalesced record stays in the log as a failed batch, which
            # replay re-fails and skips exactly as this did (D-042).
            self.solo_fallbacks += 1
            for s in group:
                try:
                    s.tt = self.store._write([s.op])
                except BaseException as e:  # noqa: BLE001 — delivered, not swallowed
                    s.error = e
                self.commits += 1
                s.done.set()
            self.submissions += len(group)
            return
        except BaseException as e:          # noqa: BLE001
            for s in group:
                s.error = e
                s.done.set()
            return
        self.commits += 1
        self.submissions += len(group)
        self.max_group = max(self.max_group, len(group))
        for s in group:
            s.tt = tt
            s.done.set()

    def stats(self) -> dict[str, Any]:
        return {"commits": self.commits, "submissions": self.submissions,
                "max_group": self.max_group,
                "solo_fallbacks": self.solo_fallbacks,
                "rows_per_commit": round(self.submissions / self.commits, 2)
                if self.commits else 0.0}
