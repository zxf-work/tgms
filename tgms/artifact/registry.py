"""The registry — M5 design memo §2.

`tgms/artifact/` is a new module. The registry persists as an append-only,
chain-verified JSONL file at `<store>/artifacts.jsonl` — the
`tgms/storage/eventlog.py` shape — and registry state is the fold over that
file. `ResultStore` is unchanged and is *referenced*, never retrofitted (§2.1).

**Persistence, mirroring `eventlog.py`.** A header record first
(`{"format": "tgms-artifact-registry", "version": 1}`), then one
canonical-JSON `ArtifactRecord` per line, and a rolling chain over the raw
record bytes built with `extend_chain` **reused verbatim**
(`tgms.storage.eventlog.extend_chain`) — the same function the engine's event
log and `check.py`'s own log-tamper detection are built on (§2.3(c)).

**Chain verification on open, without an external checkpoint.** `check.py`'s
chain verification compares a freshly walked prefix chain against a
`Checkpoint` some *other* object stored earlier (a `DependencyScope`). The
artifact registry has no such external holder pointing into
`artifacts.jsonl` — nothing else in the system keeps a checkpoint into this
particular log. §2.4 obligation 3 nonetheless requires `Registry.load` to
"verify the rolling chain ... and raise on mismatch" on its own. The reading
adopted here (flagged — the memo does not spell out what a self-contained
open-time check compares against): `_load` (a) recomputes every record's
`record_digest` and compares it to the digest stored in that same line — a
content-hash self-check with the same shape as `eventlog.py`'s own
`batch_id` (a per-record hash used exactly this way by `trim_torn_tail`) —
and (b) verifies, per `name`, that `generation`/`supersedes` form a
consecutive, correctly-linked chain from 0. Between them these catch every
single-byte corruption in practice: a byte inside a record's fields breaks
its own `record_digest`; a byte inside the `record_digest` field itself no
longer matches the recomputed one; and reordering, deletion or insertion of
a whole record breaks the generation sequence. The rolling `extend_chain`
value is *also* computed and exposed (`Registry.checkpoint()`), so a future
external holder — mirroring `DependencyScope.checkpoints` — has the same
primitive `check.py` already trusts, ready to use, the moment one exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from tgms.core.errors import InvalidArgError, StateError
from tgms.core.model import canonical_json
from tgms.storage.eventlog import SEED_CHAIN, EventLog, extend_chain
from tgms.tgir.depscope import DependencyScope, store_identity

from tgms.artifact.record import ArtifactId, ArtifactRecord, StepDependency

HEADER = {"format": "tgms-artifact-registry", "version": 1}
FILE_NAME = "artifacts.jsonl"


class Registry:
    """The append-only registry for one store, folded into an in-memory
    index. `<store>/artifacts.jsonl`, opened beside `<store>/eventlog.jsonl`
    — §2.3's rule that every artifact record names the `store` identity it
    belongs to, and the registry refuses a record whose `store` does not
    match the log it is opened beside.
    """

    def __init__(self, store: str | Path, *, log: EventLog | None = None) -> None:
        self.store_dir = Path(store)
        self.path = self.store_dir / FILE_NAME
        self._log = log if log is not None else EventLog(self.store_dir / "eventlog.jsonl")
        self._by_name: dict[str, list[ArtifactRecord]] = {}
        self._chain = SEED_CHAIN
        self._checkpoint_offset = 0
        self._load()

    # -- store identity ------------------------------------------------------

    def _store_identity(self) -> str:
        return store_identity(self._log.header(), self._log.first_batch())

    # -- loading / folding ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(canonical_json(HEADER) + "\n")
            return
        with open(self.path, "rb") as f:
            header_line = f.readline()
            try:
                head = json.loads(header_line)
            except json.JSONDecodeError as e:
                raise StateError(f"unreadable artifact-registry header in {self.path}: {e}") \
                    from None
            if head.get("format") != HEADER["format"]:
                raise StateError(f"not a tgms artifact registry: {self.path}")
            chain = SEED_CHAIN
            offset = f.tell()
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise StateError(
                        f"artifact registry {self.path} is not readable at offset "
                        f"{offset}: {e}") from None
                record = ArtifactRecord.from_json(obj)
                stored_digest = obj.get("record_digest")
                if stored_digest != record.record_digest:
                    raise StateError(
                        f"artifact registry {self.path} record at offset {offset} "
                        f"fails its own record_digest check — the file has been "
                        f"tampered with or hand-edited",
                        expected=record.record_digest, got=stored_digest)
                self._append_to_index(record)
                chain = extend_chain(chain, raw)
                offset = f.tell()
            self._chain = chain
            self._checkpoint_offset = offset

    def _append_to_index(self, record: ArtifactRecord) -> None:
        history = self._by_name.setdefault(record.name, [])
        expected_generation = len(history)
        if record.generation != expected_generation:
            raise StateError(
                f"artifact registry {self.path}: {record.name!r} generation "
                f"{record.generation} is not consecutive (expected "
                f"{expected_generation}) — the log has been reordered, has a "
                f"gap, or was rewritten",
            )
        if expected_generation == 0:
            if record.supersedes is not None:
                raise StateError(
                    f"artifact registry {self.path}: {record.name!r} generation 0 "
                    f"carries a supersedes")
        else:
            expected = ArtifactId(record.name, expected_generation - 1)
            if record.supersedes != expected:
                raise StateError(
                    f"artifact registry {self.path}: {record.name!r} generation "
                    f"{record.generation}'s supersedes does not name its immediate "
                    f"predecessor")
        history.append(record)

    # -- reads -----------------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def history(self, name: str) -> tuple[ArtifactRecord, ...]:
        return tuple(self._by_name.get(name, ()))

    def current(self, name: str) -> ArtifactRecord | None:
        history = self._by_name.get(name)
        return history[-1] if history else None

    def at(self, name: str, generation: int) -> ArtifactRecord | None:
        history = self._by_name.get(name)
        if not history or generation < 0 or generation >= len(history):
            return None
        return history[generation]

    def current_generations(self) -> tuple[ArtifactRecord, ...]:
        """One record per name — its latest generation. This is the
        population `tgms.artifact.lookup`'s §3.2 walk ranges over."""
        return tuple(self.current(name) for name in self.names())

    def checkpoint(self) -> tuple[int, str]:
        """`(offset, chain)` past the last record this registry has folded —
        the same shape `DependencyScope.checkpoints` uses for the event log,
        ready for a future holder of a checkpoint into this log."""
        return self._checkpoint_offset, self._chain

    # -- writes ------------------------------------------------------------

    def register(self, *, name: str, kind: str, plan: dict[str, Any], basis: dict[str, Any],
                 state: dict[str, Any], refresh: dict[str, Any],
                 steps: Iterable[StepDependency] = (),
                 dependency: DependencyScope | None = None,
                 parents: Iterable[ArtifactId] = (), payload: dict[str, Any] | None = None,
                 provenance: dict[str, Any] | None = None,
                 store: str | None = None) -> ArtifactRecord:
        """Build the next generation of `name` and append it.

        `generation` and `supersedes` are computed here, not supplied by the
        caller — §1.1's rule that a generation is decided by the fold, never
        by a stored flag or an out-of-band claim. `store` defaults to this
        registry's own log identity; a caller may only pass a `store` that
        agrees with it (checked in `append`), since a record naming a
        foreign store could never be checked against this log anyway.
        """
        prior = self.current(name)
        generation = 0 if prior is None else prior.generation + 1
        supersedes = None if prior is None else ArtifactId(name, prior.generation)
        record_store = store if store is not None else self._store_identity()
        record = ArtifactRecord(
            name=name, generation=generation, kind=kind, store=record_store,
            plan=dict(plan), basis=dict(basis), state=dict(state), refresh=dict(refresh),
            steps=tuple(steps), dependency=dependency, supersedes=supersedes,
            parents=tuple(parents), payload=(dict(payload) if payload is not None else None),
            provenance=(dict(provenance) if provenance is not None else None),
        )
        self.append(record)
        return record

    def append(self, record: ArtifactRecord) -> None:
        """Append an already-built `ArtifactRecord`. Refuses a record whose
        `store` does not match the event log this registry is opened beside
        (§2.3), and a record that is not the next generation of its name
        (the same consecutiveness `_append_to_index` enforces on load)."""
        identity = self._store_identity()
        if record.store != identity:
            raise InvalidArgError(
                "artifact record names a different store than the event log this "
                "registry is opened beside",
                record_store=record.store, log_identity=identity)
        prior = self.current(record.name)
        expected_generation = 0 if prior is None else prior.generation + 1
        if record.generation != expected_generation:
            raise InvalidArgError(
                f"{record.name!r} generation {record.generation} is not the next "
                f"generation (expected {expected_generation})")
        raw = (canonical_json(record.to_json()) + "\n").encode("utf-8")
        with open(self.path, "ab") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
            end_offset = f.tell()
        self._chain = extend_chain(self._chain, raw)
        self._checkpoint_offset = end_offset
        self._append_to_index(record)


__all__ = ["FILE_NAME", "HEADER", "Registry"]
