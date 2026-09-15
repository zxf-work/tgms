"""Replay with periodic compaction (Gate E / D-149).

The 24h longevity soak (`benchmarks/longevity-v1/README.md`) could not run
its replay/digest-equivalence check: replaying the soak's 1,074,952
uncompacted batches would have grown the rebuilt store's manifests along
D-149's own O(batches^2) curve to a projected ~280 TB. The fix is
`eventlog.replay(..., compact_every=N)`, which folds the manifest back down
periodically during replay exactly as a live writer's own periodic
`compact()`/`gc()` does.

This file establishes, from behavior rather than just from reading the
engine source, that calling `compact()`/`gc()` between replayed batches is
safe:

* compaction does not stamp a wall-clock or "live store" transaction time —
  the compacted generation inherits the pre-compaction `created_tt`
  unchanged (`NativeStore::compact`, `crates/tgms-engine-core/src/
  compact.rs`: "Compaction is a physical rewrite, so it does not advance
  transaction time"), so it cannot collide with or outrun the next
  historical `tt` replay is about to apply.
* replay's own strict-monotonic check (`tgms/storage/eventlog.py::replay`)
  compares each batch's `tt` only against the log's own previous batch `tt`
  (a variable local to the replay loop) — it never reads the store's clock
  or frontier, so nothing compaction does to the store can perturb it.
* `store_digest()` (`tgms/storage/base.py::StorageAdapter.store_digest`) is
  computed purely from the sorted logical node/edge rows — compaction and
  gc are physical-layout-only, so they cannot change it.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.cli import main as cli_main
from tgms.core.errors import StateError
from tgms.storage.base import make_op
from tgms.storage.eventlog import replay

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402

N_BATCHES = 600
COMPACT_EVERY = 100


def _write_batches(store: "tgms.Store", n: int, compact_every: int | None) -> None:
    for b in range(n):
        store.assert_edge(f"n{b}", f"n{b + 1}", "R", {"w": b},
                          vt_s=b, vt_e=b + 10_000, disc=f"#{b}")
        if compact_every and (b + 1) % compact_every == 0:
            store.adapter.compact()
            store.adapter.gc(keep_last=2)


def test_compact_does_not_advance_created_tt(tmp_path):
    """1(a): the compacted generation's created_tt is unchanged, not stamped
    from wall clock or any 'live' notion of the current time.

    There is no Python binding to read `created_tt` directly, so this is
    tested behaviorally against the exact engine check it would otherwise
    trip (`NativeStore::begin`, `crates/tgms-engine-core/src/store.rs`
    ~line 1351: `if tt <= self.manifest.created_tt ... "transaction time
    must advance"`): commit a batch at a small tt, compact(), then commit
    the very next tt (+1). Wall-clock nanoseconds would dwarf a +1 step, so
    if compact() ever stamped the live clock instead of inheriting
    `created_tt` unchanged, this next commit would fail with exactly that
    error.
    """
    adapter = NativeAdapter(tmp_path / "native")
    adapter.begin()
    adapter.apply_ops([make_op("assert_edge", src="a", dst="b", rel_type="R",
                               props={}, vt_s=0, vt_e=10, disc="")], tt=1)
    adapter.commit()

    adapter.compact()
    adapter.gc(keep_last=1)

    # Must not raise: proves compact()/gc() did not push created_tt past 2.
    adapter.begin()
    adapter.apply_ops([make_op("assert_edge", src="b", dst="c", rel_type="R",
                               props={}, vt_s=0, vt_e=10, disc="")], tt=2)
    adapter.commit()
    adapter.close()


def test_compact_and_gc_do_not_change_store_digest(tmp_path):
    """1(c): store_digest() is content-only."""
    store = tgms.open(tmp_path / "s", backend="native")
    for b in range(50):
        store.assert_edge(f"n{b}", f"n{b + 1}", "R", {"w": b}, vt_s=b, vt_e=b + 100)
    before = store.adapter.store_digest()
    store.adapter.compact()
    after_compact = store.adapter.store_digest()
    store.adapter.gc(keep_last=1)
    after_gc = store.adapter.store_digest()
    assert after_compact == before
    assert after_gc == before
    store.close()


def test_replay_monotonic_check_is_local_to_the_log_not_the_store(tmp_path):
    """1(b): replay's tt check is `tt <= prev_tt` against the log's own
    previous record — a variable local to the replay loop — never against
    the store's clock or frontier (confirmed by reading
    `tgms/storage/eventlog.py::replay`: `prev_tt` is seeded from `0` and
    updated only from `batch["tt"]`, with no call into `adapter` before the
    comparison). A regression that made this check depend on store state
    would still need to reject a genuinely non-monotonic log, which is what
    this test exercises end to end."""
    store = tgms.open(tmp_path / "orig", backend="native")
    store.assert_edge("a", "b", "R", {}, vt_s=0, vt_e=1)
    store.assert_edge("b", "c", "R", {}, vt_s=0, vt_e=1)
    store.close()

    log_path = tmp_path / "orig" / "eventlog.jsonl"
    lines = log_path.read_text().splitlines()
    # header + 2 batches expected
    assert len(lines) >= 3
    rec = json.loads(lines[-1])
    rec["tt"] = 1  # force non-monotonic (first batch's tt is >= 1)
    rec["batch_id"] = "corrupted-for-test"
    lines[-1] = json.dumps(rec)
    log_path.write_text("\n".join(lines) + "\n")

    fresh = NativeAdapter(tmp_path / "fresh" / "native")
    with pytest.raises(StateError, match="non-monotonic"):
        replay(log_path, fresh)
    fresh.close()


def test_replay_with_periodic_compaction_reproduces_the_original_digest(tmp_path):
    """The Gate E deliverable: write N=600 batches with compact() every 100,
    record the digest; replay into a fresh store (i) uncompacted and (ii)
    with compact_every=100; both replay digests must equal the original."""
    orig_root = tmp_path / "orig"
    store = tgms.open(orig_root, backend="native")
    _write_batches(store, N_BATCHES, COMPACT_EVERY)
    original_digest = store.adapter.store_digest()
    store.close()

    log_path = orig_root / "eventlog.jsonl"

    # (i) replay without compaction
    plain = NativeAdapter(tmp_path / "plain" / "native")
    n_plain = replay(log_path, plain)
    assert n_plain == N_BATCHES
    assert plain.store_digest() == original_digest
    plain.close()

    # (ii) replay with periodic compaction
    compacted = NativeAdapter(tmp_path / "compacted" / "native")
    n_compacted = replay(log_path, compacted, compact_every=COMPACT_EVERY)
    assert n_compacted == N_BATCHES
    assert compacted.store_digest() == original_digest
    compacted.close()


def test_replay_compact_every_rejects_non_positive(tmp_path):
    store = tgms.open(tmp_path / "orig", backend="native")
    store.assert_edge("a", "b", "R", {}, vt_s=0, vt_e=1)
    store.close()

    fresh = NativeAdapter(tmp_path / "fresh" / "native")
    with pytest.raises(ValueError):
        replay(tmp_path / "orig" / "eventlog.jsonl", fresh, compact_every=0)
    fresh.close()


def test_replay_compact_every_rejects_adapters_without_compact(tmp_path):
    """Only the native engine exposes compact()/gc(); another adapter must
    fail loudly rather than silently ignore the flag."""
    from tgms.storage.duckdb_adapter import DuckDBAdapter

    store = tgms.open(tmp_path / "orig", backend="native")
    store.assert_edge("a", "b", "R", {}, vt_s=0, vt_e=1)
    store.close()

    duck = DuckDBAdapter(":memory:")
    with pytest.raises(TypeError, match="compact"):
        replay(tmp_path / "orig" / "eventlog.jsonl", duck, compact_every=10)
    duck.close()


def test_cli_replay_compact_every_flag(tmp_path, capsys):
    """`tgms replay --compact-every N` end to end, matching the harness's
    own writer-side flag naming (`--compact-every-batches` in
    scripts/longevity_run.py) closely enough to be recognizable, while
    naming what it does at the replay boundary specifically."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    store = tgms.open(src, backend="native")
    _write_batches(store, 250, compact_every=None)
    original_digest = store.adapter.store_digest()
    store.close()

    rc = cli_main(["replay", str(src / "eventlog.jsonl"), "--store", str(dst),
                   "--backend", "native", "--compact-every", "40"])
    assert rc == 0
    capsys.readouterr()

    reopened = tgms.open(dst, backend="native")
    assert reopened.adapter.store_digest() == original_digest
    reopened.close()
