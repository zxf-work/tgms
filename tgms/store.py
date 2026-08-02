"""Store facade: public write API over (clock, write-ahead event log, adapter).

Single-writer assumption (spec §1): one ingestion process at a time.
Every public mutating call is one write batch: the batch is appended to the
event log first (write-ahead), then applied to the backend at the same tt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator

from tgms.core.clock import HybridLogicalClock
from tgms.core.errors import StateError, TgmsError
from tgms.core.model import OPEN_END, EntityRef, Props
from tgms.storage.base import StorageAdapter, make_op
from tgms.storage.eventlog import EventLog, extend_chain

INGEST_CHUNK = 50_000


class Store:
    def __init__(self, path: str | Path, backend: str | None = None,
                 paranoid: bool = False) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        backend = backend or detect_backend(self.path)
        self.backend = backend
        self.eventlog = EventLog(self.path / "eventlog.jsonl")
        self.adapter = _make_adapter(backend, self.path)
        self.adapter.paranoid = paranoid
        #: Rolling chain over the log prefix the backend has applied; None on
        #: backends without a cursor and on legacy stores until their next
        #: write (which pays a one-time full-prefix hash to start the chain).
        self._chain: str | None = None
        self._recover()
        self.clock = HybridLogicalClock(last_tt=self.eventlog.last_tt())
        self._memories: list[Any] = []  # EvolutionMemory hooks (spec v1.1 WP2.4)

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
            self.adapter.note_event_cursor(end, self._chain)
            self.adapter.commit()

    def close(self) -> None:
        self.adapter.close()

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
                      node_label: str = "Node") -> int:
        """Bulk event-stream ingestion, chunked into write batches.
        Returns the tt of the last batch."""
        tt = self.clock.last_tt
        offset = 0
        for chunk in _chunks(events, INGEST_CHUNK):
            tt = self._write([make_op("ingest_events", events=chunk, offset=offset,
                                      node_label=node_label,
                                      source="ingest", provenance_ref=None)])
            offset += len(chunk)
        return tt

    def _write(self, ops: list[dict[str, Any]]) -> int:
        """Write-ahead: the batch is logged before it is applied. If apply
        fails, the backend rolls back; replay skips the batch identically
        (apply is deterministic), so log and store never diverge.

        On cursor-keeping backends the commit also records how far into the
        log this batch reaches (offset past its newline, rolling chain), so
        a crash after the append recovers by suffix replay (`_recover`)."""
        tt = self.clock.tick()
        _batch_id, end_offset, record = self.eventlog.append(tt, ops)
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
        self.adapter.commit()
        return tt

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


def open(path: str | Path, backend: str | None = None, paranoid: bool = False) -> Store:
    """Open (or create) a store. `backend` defaults to the existing store's
    layout, or `DEFAULT_BACKEND` for a new one."""
    return Store(path, backend=backend, paranoid=paranoid)


def _make_adapter(backend: str, path: Path) -> StorageAdapter:
    if backend == "native":
        from tgms.storage.native import NativeAdapter
        return NativeAdapter(path / "native")
    if backend in ("duckdb", "memory"):
        DuckDBAdapter = _optional_backend("duckdb")
        return DuckDBAdapter(":memory:" if backend == "memory" else path / "store.duckdb")
    if backend == "kuzu":
        return _optional_backend("kuzu")(path / "store.kuzu")
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
