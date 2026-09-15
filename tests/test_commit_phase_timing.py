"""`NativeStore.last_commit_phases()` -- component timing for the commit
path, and the accounting invariant B1-v2d exists to satisfy.

`docs/design/B1V2_AB_DIAGNOSIS_2026-09-15.md` (internal, git-ignored) found
that 100% of the B1 A/B's last/first-decile growth in engine-commit
`total_us` sat in the untimed residual -- `total_us` minus the sum of the
phases `last_commit_phases()` already named (`seal_us, closes_us, stats_us,
dict_us, manifest_us, current_us`). B1-v2d adds three more phases
(`capture_us`, `digest_us`, `delta_build_us`) so that residual should now be
near zero on every commit, not just on average.

Engine-level properties (per-phase arithmetic) belong in Rust
(`crates/tgms-engine-core/src/store.rs`, which carries the same invariant
test over the same 60-commit shape); what is worth asserting from Python,
mirroring `test_open_phase_timing.py`, is the product-facing promise: every
key the pyo3 binding is supposed to expose is actually there, and the
accounting invariant holds commit by commit through that binding too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

# `Store` is the batching entry point that gives one `ingest_events` call one
# generation for a singleton event list -- the same shape
# `test_open_phase_timing.py::build_store` relies on for `delta_count`.
import tgms  # noqa: E402

COMMIT_PHASE_KEYS = {
    "capture_us", "seal_us", "closes_us", "stats_us", "dict_us",
    "digest_us", "delta_build_us", "manifest_us", "current_us", "total_us",
    "manifest_bytes", "segments_named", "manifest_checkpoint",
}

# Everything `total_us` (the engine's own `commit_start.elapsed()`) is
# supposed to fully account for -- every timed phase except itself and the
# three non-timing fields (byte count, segment count, checkpoint flag).
NAMED_PHASE_KEYS = COMMIT_PHASE_KEYS - {
    "total_us", "manifest_bytes", "segments_named", "manifest_checkpoint",
}


def _residual_bound(total_us: int) -> int:
    return max(50, total_us // 50)  # max(50us, 2% of total_us)


def _open(tmp_path: Path):
    return tgms.open(tmp_path / "s", backend="native")


def test_commit_phases_has_every_key(tmp_path):
    store = _open(tmp_path)
    try:
        store.ingest_events(
            [{"src": "a", "dst": "b", "rel_type": "R", "vt_s": 0}])
        phases = store.adapter._store.last_commit_phases()
    finally:
        store.close()
    assert set(phases) == COMMIT_PHASE_KEYS
    for key in COMMIT_PHASE_KEYS:
        assert isinstance(phases[key], int), f"{key} is not an int: {phases[key]!r}"


def test_total_us_accounts_for_every_named_phase_on_every_commit(tmp_path):
    """60 singleton commits: the accounting invariant must hold on *every*
    one, not just in aggregate -- a region that only occasionally goes
    untimed (e.g. one that only fires on a checkpoint generation) would
    otherwise hide inside an average."""
    store = _open(tmp_path)
    try:
        for i in range(60):
            store.ingest_events([
                {"src": f"n{i}", "dst": f"n{i + 1}", "rel_type": "R",
                 "vt_s": i}
            ])
            phases = store.adapter._store.last_commit_phases()
            named_sum = sum(phases[k] for k in NAMED_PHASE_KEYS)
            total_us = phases["total_us"]
            assert total_us >= named_sum, (
                f"commit {i}: named phases ({named_sum}us) exceed total_us "
                f"({total_us}us) -- a phase is double-counting another's "
                f"window")
            residual = total_us - named_sum
            bound = _residual_bound(total_us)
            assert residual <= bound, (
                f"commit {i}: residual {residual}us exceeds "
                f"max(50us, 2%)={bound}us (total_us={total_us}, "
                f"named_sum={named_sum})")
        assert store.adapter._store.generation() == 60
    finally:
        store.close()
