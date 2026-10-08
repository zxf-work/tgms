"""The store-identity memo (failure ledger `registry-store-identity-recomputed`).

`Registry._store_identity()` and `check`'s step 3 used to recompute
`store_identity(log.header(), log.first_batch())` on every call — a full
parse and canonicalization of the log's genesis record, which on a
bulk-loaded store is one multi-megabyte line — twice per publish and once
per check. Both now go through `tgms.tgir.depscope.StoreIdentityMemo`. This
file pins the three properties that make that safe:

1. **reuse** — one computation per `Registry` (and per `EventLog` object
   for `check`), however many publishes or checks follow;
2. **invalidation** — a different log at the same path, an in-place
   same-size rewrite of the genesis record with its mtime put back (the
   D-162 aliasing case a stat key cannot see), and a removed log all take
   the uncached path again, with its value or its exception;
3. **byte identity** — a storm run publishes exactly the same
   `artifacts.jsonl` bytes and the same non-timing rows with the memo as
   with the uncached expression.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
import tgms.tgir.check as check_mod
from tgms.artifact.record import StepDependency
from tgms.artifact.registry import Registry
from tgms.core.errors import InvalidArgError
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import check
from tgms.tgir.depscope import (
    UNANCHORED, DependencyScope, ScopeTerm, StoreIdentityMemo, Targets, store_identity,
)

BACKEND = "native"


def _uncached(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _node(uid: str, **props: Any) -> dict[str, Any]:
    return make_op("assert_node", uid=uid, label="N", props=dict(props), vt_s=0, vt_e=100)


def _store(*batches: tuple[int, list[dict[str, Any]]]) -> tuple[Path, EventLog]:
    store = Path(tempfile.mkdtemp())
    log = EventLog(store / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return store, log


def _genesis(n: int = 200) -> list[dict[str, Any]]:
    """A genesis batch big enough that its identity is a real parse."""
    return [_node(f"u{i:04d}", w=i) for i in range(n)]


def _register(reg: Registry, log: EventLog, name: str, store: str | None = None) -> Any:
    """`register` with `store=None` by default, so it asks the registry for
    the identity itself (and `append` asks again) — the publish path's two
    calls."""
    scope = DependencyScope(store=store or _uncached(log), tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("u0000",))),))
    return reg.register(
        name=name, kind="query_result",
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1},
        basis={"tt_q": 10, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope)], store=store)


def _rewrite_genesis_same_size(log: EventLog, old: bytes, new: bytes) -> None:
    """Replace `old` with `new` (same length) inside the genesis record, in
    place, and put the file's atime/mtime back — every stat field a
    stat-keyed memo would compare is unchanged except ctime."""
    assert len(old) == len(new)
    st = os.stat(log.path)
    data = log.path.read_bytes()
    header_end = data.index(b"\n") + 1
    at = data.index(old, header_end)
    assert at < data.index(b"\n", header_end)  # inside the first record
    with open(log.path, "r+b") as f:
        f.seek(at)
        f.write(new)
    os.utime(log.path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(log.path).st_size == st.st_size


# ---------------------------------------------------------------------------
# 1 — reuse
# ---------------------------------------------------------------------------

def test_registry_computes_the_identity_once_per_instance() -> None:
    store, log = _store((10, _genesis()))
    reg = Registry(store)
    for i in range(3):
        rec = _register(reg, log, f"a{i}")
        assert rec.store == _uncached(log)
    memo = reg._identity_memo
    # three publishes, two identity calls each: one computation, five hits
    assert (memo.misses, memo.hits) == (1, 5)

    # appends after genesis do not change the identity, and do not miss
    log.append(20, [_node("late")])
    assert reg._store_identity() == _uncached(log)
    assert memo.misses == 1

    # a second Registry has its own memo (per instance, never shared)
    other = Registry(store)
    assert other._identity_memo is not memo
    assert other._store_identity() == reg._store_identity()
    assert other._identity_memo.misses == 1


def test_check_reuses_the_identity_per_event_log_object() -> None:
    store, log = _store((10, _genesis()))
    scope = DependencyScope(store=_uncached(log), tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("u0000",))),))
    first = check(scope, log)
    for _ in range(3):
        assert check(scope, log) == first
    memo = check_mod._IDENTITY_MEMOS[log]
    assert (memo.misses, memo.hits) == (1, 3)
    # a different EventLog object over the same file gets its own memo
    log2 = EventLog(log.path)
    assert check(scope, log2) == first
    assert check_mod._IDENTITY_MEMOS[log2] is not memo


# ---------------------------------------------------------------------------
# 2 — invalidation
# ---------------------------------------------------------------------------

def test_a_different_log_at_the_same_path_is_recomputed_and_still_refused() -> None:
    store, log = _store((10, _genesis()))
    reg = Registry(store)
    rec_a = _register(reg, log, "a")
    identity_a = rec_a.store

    _other_store, other_log = _store((10, _genesis(150)))
    shutil.copyfile(other_log.path, log.path)  # a rebuilt store, same path
    identity_b = _uncached(log)
    assert identity_b != identity_a

    assert reg._store_identity() == identity_b
    assert reg._identity_memo.misses == 2
    # the protection the identity exists for still holds: a record naming
    # the old store is refused against the new log
    with pytest.raises(InvalidArgError, match="different store than the event log"):
        _register(reg, log, "b", store=identity_a)


def test_same_size_in_place_rewrite_of_the_genesis_record_is_recomputed() -> None:
    store, log = _store((10, _genesis()))
    reg = Registry(store)
    before = reg._store_identity()
    _rewrite_genesis_same_size(log, b'"u0007"', b'"u9997"')
    after = reg._store_identity()
    assert after == _uncached(log)
    assert after != before
    assert reg._identity_memo.misses == 2


def test_check_verdict_on_a_rewritten_genesis_matches_the_uncached_check() -> None:
    _store_dir, log = _store((10, _genesis()), (20, [_node("x")]))
    scope = DependencyScope(store=_uncached(log), tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("u0000",))),))
    assert check(scope, log).reason != "store-mismatch"  # memo now populated
    _rewrite_genesis_same_size(log, b'"u0007"', b'"u9997"')
    cached = check(scope, log)
    fresh_object = check(scope, EventLog(log.path))  # a brand-new, empty memo
    assert cached == fresh_object
    assert cached.reason == "store-mismatch"


def test_unanchored_log_is_never_memoized_and_anchors_on_first_write() -> None:
    _store_dir, log = _store()
    memo = StoreIdentityMemo()
    assert memo.identity(log) == UNANCHORED
    assert memo.identity(log) == UNANCHORED
    assert memo.hits == 0
    log.append(10, _genesis(3))
    assert memo.identity(log) == _uncached(log) != UNANCHORED
    assert memo.identity(log) == _uncached(log)
    assert (memo.misses, memo.hits) == (3, 1)


def test_a_removed_log_raises_what_the_uncached_path_raises() -> None:
    _store_dir, log = _store((10, _genesis()))
    memo = StoreIdentityMemo()
    memo.identity(log)
    os.remove(log.path)
    with pytest.raises(FileNotFoundError):
        _uncached(log)
    with pytest.raises(FileNotFoundError):
        memo.identity(log)


def test_an_unparseable_genesis_raises_what_the_uncached_path_raises() -> None:
    _store_dir, log = _store((10, _genesis()))
    memo = StoreIdentityMemo()
    memo.identity(log)
    _rewrite_genesis_same_size(log, b'"u0007"', b'"u0007\n')  # breaks the line
    with pytest.raises(Exception) as uncached_exc:
        _uncached(log)
    with pytest.raises(Exception) as memo_exc:
        memo.identity(log)
    assert type(memo_exc.value) is type(uncached_exc.value)
    assert str(memo_exc.value) == str(uncached_exc.value)


# ---------------------------------------------------------------------------
# 3 — byte identity of every published record, vs the uncached path
# ---------------------------------------------------------------------------

def _build_fixture(store_dir: Path) -> None:
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(1)
    uids = [f"n{i}" for i in range(24)]
    events = []
    for i in range(240):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst, "rel_type": rng.choice(["F", "M", "C"]),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


def _non_timing(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()
           if k not in ("lookup_wall_ms", "global_recompute_wall_ms")}
    out["arms"] = {
        arm: {k: (v is None if k == "ttf_ms" else v) for k, v in a.items()
              if k not in ("check_wall_ms", "refresh_wall_ms")}
        for arm, a in row["arms"].items()
    }
    return out


def _storm_run(store_dir: Path) -> tuple[bytes, list[dict[str, Any]]]:
    from tgms.eval.storm import Storm

    storm = Storm(store_dir, n_artifacts=10, seed=3, backend=BACKEND)
    try:
        rows = [_non_timing(r.to_json()) for r in storm.run(3)]
        return storm.registry.path.read_bytes(), rows
    finally:
        storm.close()


def test_storm_publishes_byte_identical_records_with_and_without_the_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pristine = tmp_path / "pristine"
    _build_fixture(pristine)
    shutil.copytree(pristine, tmp_path / "uncached")
    shutil.copytree(pristine, tmp_path / "cached")

    with monkeypatch.context() as m:
        m.setattr(Registry, "_store_identity", lambda self: _uncached(self._log))
        m.setattr(check_mod, "_store_identity_of", _uncached)
        uncached_bytes, uncached_rows = _storm_run(tmp_path / "uncached")

    cached_bytes, cached_rows = _storm_run(tmp_path / "cached")
    assert len(cached_rows) == 3
    assert cached_bytes == uncached_bytes
    assert cached_rows == uncached_rows
