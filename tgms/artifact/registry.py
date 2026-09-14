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

**Cross-process append lock (OSDI27 fault-matrix memo §7 F4-2).** `append`
used to be an unlocked check-then-act: it read `prior = self.current(name)`
from this object's own in-memory fold, computed `expected_generation =
prior.generation + 1`, and only then appended. Two processes each holding a
`Registry` opened on the same directory can both read a `prior` neither has
seen the other's write to yet, both pass the check, and both append a
record at the same `generation` — the on-disk chain then has two lines
claiming one generation, which `_append_to_index` catches only later, on
the next `Registry(path)` open, as a `StateError` (`{name!r} generation ...
is not consecutive`), long after the silent duplicate write already
happened. The fix is an OS-level exclusive lock — `fcntl.flock` on a
sidecar `<path>.lock` file, never `artifacts.jsonl` itself, so a reader
opening the registry read-only is never blocked by a writer — held across
the *entire* check-then-act: a re-read of the on-disk tail past this
object's own `_checkpoint_offset` (`_reload_tail_locked`, so this object's
fold catches up on whatever another process appended, not only what it
appended itself), the generation check, the `ab` append and its `fsync`,
and the in-memory index update. A losing writer's `expected_generation` no
longer matches its already-built `record.generation` once the tail is
reread, and it raises `InvalidArgError` — never silently appends. A
post-append re-read (`_verify_append_landed`) is kept as belt-and-braces: it
reopens the file and compares the bytes at the offset this process just
wrote against what it holds in memory, so a lock that silently failed to
exclude (e.g. an `flock`-hostile filesystem) is a loud `StateError` naming
the mismatch, never a duplicate generation that only surfaces on the next
open.
"""

from __future__ import annotations

import fcntl
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
        #: Sidecar lock for `append` (never the registry file itself, so a
        #: read-only fold — `Registry(path)`'s own `_load`, or a second
        #: `Registry` opened only to read — never contends with a writer).
        self._lock_path = self.path.with_name(self.path.name + ".lock")
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
            header_bytes = (canonical_json(HEADER) + "\n").encode("utf-8")
            with open(self.path, "wb") as f:
                f.write(header_bytes)
            # Past the header, not 0 — otherwise the first `append()`'s
            # `_reload_tail_locked` would try to re-fold the header line
            # itself as a record.
            self._checkpoint_offset = len(header_bytes)
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
            offset = f.tell()
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                self._consume_record_line(raw, offset)
                offset = f.tell()
            self._checkpoint_offset = offset

    def _consume_record_line(self, raw: bytes, offset_for_error: int) -> None:
        """Parse, digest-verify and fold one on-disk registry record line
        into this object's in-memory index and chain. The single
        implementation `_load` (walking the whole file from byte 0 at open
        time) and `_reload_tail_locked` (walking only the tail past this
        object's own `_checkpoint_offset`, to pick up what another process
        appended) both call, so the two folds can never drift apart. Caller
        is responsible for advancing `self._checkpoint_offset` past `raw` —
        this only folds the record itself."""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StateError(
                f"artifact registry {self.path} is not readable at offset "
                f"{offset_for_error}: {e}") from None
        record = ArtifactRecord.from_json(obj)
        stored_digest = obj.get("record_digest")
        if stored_digest != record.record_digest:
            raise StateError(
                f"artifact registry {self.path} record at offset {offset_for_error} "
                f"fails its own record_digest check — the file has been "
                f"tampered with or hand-edited",
                expected=record.record_digest, got=stored_digest)
        self._append_to_index(record)
        self._chain = extend_chain(self._chain, raw)

    def _reload_tail_locked(self) -> None:
        """Catch this object's in-memory fold up with whatever is on disk
        past its own `_checkpoint_offset` — in particular, records another
        process appended to this same `artifacts.jsonl` that this object's
        `_load()` (or an earlier `append()`) never saw. Must be called only
        while holding the append lock: reading a tail concurrently with
        another process's `ab` write could read a torn line."""
        with open(self.path, "rb") as f:
            f.seek(self._checkpoint_offset)
            offset = self._checkpoint_offset
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                self._consume_record_line(raw, offset)
                offset = f.tell()
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
        (the same consecutiveness `_append_to_index` enforces on load) —
        checked *again*, under an OS-level lock, after re-reading whatever
        another process has appended since this object last looked
        (`_reload_tail_locked`), so two processes racing to append `name`'s
        next generation can never both succeed (module docstring, "Cross-
        process append lock")."""
        identity = self._store_identity()
        if record.store != identity:
            raise InvalidArgError(
                "artifact record names a different store than the event log this "
                "registry is opened beside",
                record_store=record.store, log_identity=identity)
        with open(self._lock_path, "a+b") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                self._append_locked(record)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    def _append_locked(self, record: ArtifactRecord) -> None:
        """The check-then-act, run while `append` holds the exclusive lock.
        Not called directly — only through `append`, which is what actually
        acquires the lock this method's safety depends on."""
        self._reload_tail_locked()
        prior = self.current(record.name)
        expected_generation = 0 if prior is None else prior.generation + 1
        if record.generation != expected_generation:
            raise InvalidArgError(
                f"{record.name!r} generation {record.generation} is not the next "
                f"generation (expected {expected_generation}) — another writer "
                f"already appended it",
                name=record.name, attempted=record.generation,
                expected=expected_generation)
        raw = (canonical_json(record.to_json()) + "\n").encode("utf-8")
        start_offset = self._checkpoint_offset
        with open(self.path, "ab") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
            end_offset = f.tell()
        self._chain = extend_chain(self._chain, raw)
        self._checkpoint_offset = end_offset
        self._append_to_index(record)
        self._verify_append_landed(record, raw, start_offset, end_offset)

    def _verify_append_landed(self, record: ArtifactRecord, raw: bytes,
                              start_offset: int, end_offset: int) -> None:
        """Belt-and-braces alongside the lock: re-open the file and read back
        exactly the bytes at the offset this process just wrote, rather than
        trusting the `raw` already held in memory. Under a correctly-held
        `fcntl.flock` this can never fire — nothing else can have written
        between this process's own write and this read — so a mismatch means
        the lock itself failed to exclude (e.g. a filesystem that does not
        honor `flock`), and that must be a loud, named failure, never a
        silently duplicated generation that only surfaces on the next
        `Registry(path)` open."""
        with open(self.path, "rb") as f:
            f.seek(start_offset)
            on_disk = f.read(end_offset - start_offset)
        if on_disk != raw:
            raise StateError(
                f"artifact registry {self.path}: the record just appended for "
                f"{record.name!r} generation {record.generation} does not match "
                f"what is on disk at offset {start_offset} — another writer "
                f"appended concurrently despite the lock",
                name=record.name, generation=record.generation, offset=start_offset)


def verify(store: str | Path) -> list[dict[str, Any]]:
    """Walk `<store>/artifacts.jsonl` read-only and report what is wrong.

    `Registry(store)` already checks all of this — and *raises* on the first
    defect, which is right for every caller that is about to use the fold and
    exactly wrong for an integrity checker. `_load` would refuse to build an
    index at all, so nothing past the first bad line would ever be looked at,
    and the operator would peel the onion a record at a time. This walks the
    same bytes with the same rules and collects instead.

    Returns `{kind, offset, detail}` dicts, deliberately free of the
    integrity report's own schema (the caller adds `layer`/`generation`):

    * `registry-header` — the first line is not this format's header;
    * `record-unparseable` / `record-malformed` — the line is not JSON, or
      not a well-formed `ArtifactRecord`;
    * `record-digest-mismatch` — the record fails its own `record_digest`,
      i.e. it was hand-edited or tampered with (§2.4 obligation 3);
    * `generation-not-consecutive` / `supersedes-mismatch` — the per-name
      generation chain has a gap, a duplicate, a reordering, or a link that
      does not name its immediate predecessor. These are what catch a whole
      record being deleted or inserted, which no per-record digest can see;
    * `blob-missing` / `blob-digest-mismatch` — a `refresh.ref`,
      `plan.plan_ref` or `payload.result_ref` that names a file the store
      does not hold, or a result blob whose own `result_digest` is not the
      one the record recorded.

    Nothing is opened for writing, and — unlike `Registry.__init__` — no
    missing registry is created: a store with no `artifacts.jsonl` has no
    artifacts, which is not a defect.
    """
    store_dir = Path(store)
    path = store_dir / FILE_NAME
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out

    with open(path, "rb") as f:
        header_line = f.readline()
        try:
            head = json.loads(header_line)
            ok = head.get("format") == HEADER["format"]
        except json.JSONDecodeError:
            ok = False
        if not ok:
            out.append({"kind": "registry-header", "offset": 0,
                        "detail": f"{path} does not begin with a tgms artifact-registry "
                                  f"header"})
            return out
        offset = f.tell()
        seen: dict[str, int] = {}
        while True:
            raw = f.readline()
            if not raw:
                break
            start, offset = offset, f.tell()
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                out.append({"kind": "record-unparseable", "offset": start,
                            "detail": f"the record at offset {start} is not JSON: {e}"})
                continue
            try:
                record = ArtifactRecord.from_json(obj)
            except (InvalidArgError, KeyError, TypeError, ValueError) as e:
                out.append({"kind": "record-malformed", "offset": start,
                            "detail": f"the record at offset {start} is not a well-formed "
                                      f"artifact record: {e}"})
                continue
            if obj.get("record_digest") != record.record_digest:
                out.append({
                    "kind": "record-digest-mismatch", "offset": start,
                    "detail": f"{record.name!r} generation {record.generation} at offset "
                              f"{start} fails its own record_digest (recomputes to "
                              f"{record.record_digest}, carries "
                              f"{obj.get('record_digest')!r}) — the file has been "
                              f"tampered with or hand-edited"})
            expected = seen.get(record.name, 0)
            if record.generation != expected:
                out.append({
                    "kind": "generation-not-consecutive", "offset": start,
                    "detail": f"{record.name!r} generation {record.generation} is not "
                              f"consecutive (expected {expected}) — the log has been "
                              f"reordered, has a gap, or was rewritten"})
            elif expected and record.supersedes != ArtifactId(record.name, expected - 1):
                out.append({
                    "kind": "supersedes-mismatch", "offset": start,
                    "detail": f"{record.name!r} generation {record.generation} does not "
                              f"name its immediate predecessor"})
            seen[record.name] = max(expected, record.generation) + 1
            out.extend(_blob_defects(store_dir, record, start))
    return out


def _blob_defects(store_dir: Path, record: ArtifactRecord,
                  offset: int) -> list[dict[str, Any]]:
    """Every file this record points at, checked for presence and — where the
    record carries a content address for it — for agreement with it.

    `refresh.ref` and `plan.plan_ref` have no stored digest, so existence is
    all there is to check. `payload.result_ref` does: the blob is what
    `ResultStore.put` wrote under `payload.result_digest`, so the digest the
    document carries must be the one the record names.
    """
    out: list[dict[str, Any]] = []
    refs = [("refresh.ref", record.refresh.get("ref")),
            ("plan.plan_ref", record.plan.get("plan_ref"))]
    if record.payload is not None:
        refs.append(("payload.result_ref", record.payload.get("result_ref")))
    where = f"{record.name!r} generation {record.generation}"
    for field, ref in refs:
        if not ref:
            continue
        blob = store_dir / ref
        if not blob.exists():
            out.append({"kind": "blob-missing", "offset": offset,
                        "detail": f"{where}: {field} names {ref}, which the store does "
                                  f"not hold"})
            continue
        if field != "payload.result_ref":
            continue
        claimed = (record.payload or {}).get("result_digest")
        try:
            document = json.loads(blob.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            out.append({"kind": "blob-digest-mismatch", "offset": offset,
                        "detail": f"{where}: the result blob {ref} is unreadable: {e}"})
            continue
        got = document.get("result_digest") if isinstance(document, dict) else None
        if claimed and got != claimed:
            out.append({
                "kind": "blob-digest-mismatch", "offset": offset,
                "detail": f"{where}: the result blob {ref} carries result_digest "
                          f"{got!r} but the record names {claimed!r}"})
    return out


__all__ = ["FILE_NAME", "HEADER", "Registry", "verify"]
