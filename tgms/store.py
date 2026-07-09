"""Store facade: public write API over (clock, write-ahead event log, adapter).

Single-writer assumption (spec §1): one ingestion process at a time.
Every public mutating call is one write batch: the batch is appended to the
event log first (write-ahead), then applied to the backend at the same tt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator

from tgms.core.clock import HybridLogicalClock
from tgms.core.errors import TgmsError
from tgms.core.model import OPEN_END, EntityRef, Props
from tgms.storage.base import StorageAdapter, make_op
from tgms.storage.eventlog import EventLog

INGEST_CHUNK = 50_000


class Store:
    def __init__(self, path: str | Path, backend: str = "duckdb", paranoid: bool = False) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.backend = backend
        self.eventlog = EventLog(self.path / "eventlog.jsonl")
        self.adapter = _make_adapter(backend, self.path)
        self.adapter.paranoid = paranoid
        self.clock = HybridLogicalClock(last_tt=self.eventlog.last_tt())
        self._memories: list[Any] = []  # EvolutionMemory hooks (spec v1.1 WP2.4)

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
        (apply is deterministic), so log and store never diverge."""
        tt = self.clock.tick()
        self.eventlog.append(tt, ops)
        self.adapter.begin()
        try:
            self.adapter.apply_ops(ops, tt)
        except TgmsError:
            self.adapter.rollback()
            raise
        self.adapter.commit()
        return tt

    # --- introspection ------------------------------------------------------ #

    def digest(self) -> str:
        return self.adapter.store_digest()

    def stats(self) -> dict[str, Any]:
        return self.adapter.stats()


def open(path: str | Path, backend: str = "duckdb", paranoid: bool = False) -> Store:
    return Store(path, backend=backend, paranoid=paranoid)


def _make_adapter(backend: str, path: Path) -> StorageAdapter:
    if backend == "duckdb":
        from tgms.storage.duckdb_adapter import DuckDBAdapter
        return DuckDBAdapter(path / "store.duckdb")
    if backend == "kuzu":
        from tgms.storage.kuzu_adapter import KuzuAdapter
        return KuzuAdapter(path / "store.kuzu")
    if backend == "memory":
        from tgms.storage.duckdb_adapter import DuckDBAdapter
        return DuckDBAdapter(":memory:")
    raise ValueError(f"unknown backend: {backend}")


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
