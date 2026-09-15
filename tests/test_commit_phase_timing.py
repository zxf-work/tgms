"""`NativeStore.last_commit_phases()` -- component timing for the commit
path, and the accounting invariant B1-v2d exists to satisfy.

`docs/design/B1V2_AB_DIAGNOSIS_2026-09-15.md` (internal, git-ignored) found
that 100% of the B1 A/B's last/first-decile growth in engine-commit
`total_us` sat in the untimed residual -- `total_us` minus the sum of the
phases `last_commit_phases()` already named (`seal_us, closes_us, stats_us,
dict_us, manifest_us, current_us`). B1-v2d adds three more phases
(`capture_us`, `digest_us`, `delta_build_us`) so that residual should now be
near zero on every commit, not just on average. A follow-up closed a fourth
gap specific to debug builds: `publish`'s O(segments) `debug_assert_eq!`
manifest-digest recompute ran ahead of any timer, so its cost (growing with
generation count) leaked into the residual under `cargo test`/`maturin
develop`; `debug_verify_us` times it (always 0 in release, where the assert
compiles away).

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
    "digest_us", "debug_verify_us", "delta_build_us", "manifest_us",
    "current_us", "total_us",
    "manifest_bytes", "segments_named", "manifest_checkpoint",
}

# Everything `total_us` (the engine's own `commit_start.elapsed()`) is
# supposed to fully account for -- every timed phase except itself and the
# three non-timing fields (byte count, segment count, checkpoint flag).
NAMED_PHASE_KEYS = COMMIT_PHASE_KEYS - {
    "total_us", "manifest_bytes", "segments_named", "manifest_checkpoint",
}


def _residual_bound(total_us: int, *, debug_assertions: bool) -> int:
    # A debug-assertions build pays real, non-representative overhead (see
    # `crates/tgms-engine-core/src/store.rs`'s sibling of this test), and CI
    # runs an unoptimized `cargo build`/`maturin develop` on a slow shared
    # disk where scheduler jitter alone can blow past a tight bar on any one
    # commit -- so the bar itself widens under a debug build, same as Rust's.
    if debug_assertions:
        return max(1_000, total_us // 10)  # max(1000us, 10%)
    return max(100, total_us // 50)  # max(100us, 2%)


def _median(xs: list[int]) -> int:
    xs = sorted(xs)
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) // 2


def _p90(xs: list[int]) -> int:
    xs = sorted(xs)
    n = len(xs)
    idx = round((n - 1) * 0.9)
    return xs[min(idx, n - 1)]


def _worst_three(commits: list[tuple[int, int, int, int]]) -> str:
    # `commits`: (index, total_us, named_sum, residual) per commit. Used only
    # to make failure messages actionable -- a single-outlier failure (one
    # scheduler hiccup) and a three-elevated-commits failure (consistent with
    # a systematic leak) look different here.
    worst = sorted(commits, key=lambda c: c[3], reverse=True)[:3]
    return "; ".join(
        f"commit {idx}: total_us={total} named_sum={named_sum} "
        f"residual={residual}us"
        for idx, total, named_sum, residual in worst)


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
    """60 singleton commits: the accounting invariant is checked on every
    one, not just in aggregate -- a region that only occasionally goes
    untimed (e.g. one that only fires on a checkpoint generation) would
    otherwise hide inside an average.

    Even the widened per-commit bar is not CI-safe as a hard per-commit
    assertion: a shared runner can preempt the process mid-commit and
    inflate that one commit's residual for reasons that have nothing to do
    with the engine (see the Rust sibling test's comment for the full
    argument). So this asserts the *distribution* instead: the median and
    p90 residual stay under a bar, and the back of the run is no worse than
    the front by more than that bar -- properties a single preempted commit
    cannot manufacture, but a systematic (e.g. O(segments)) leak would.
    """
    store = _open(tmp_path)
    try:
        debug_assertions = bool(
            store.adapter.build_info()["debug_assertions"])
        residuals = []
        totals = []
        commits = []
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
            residuals.append(residual)
            totals.append(total_us)
            commits.append((i, total_us, named_sum, residual))
        assert store.adapter._store.generation() == 60

        bar = _residual_bound(_median(totals), debug_assertions=debug_assertions)
        median_residual = _median(residuals)
        p90_residual = _p90(residuals)
        first_ten_median = _median(residuals[:10])
        last_ten_median = _median(residuals[-10:])

        assert median_residual <= bar, (
            f"median residual over 60 commits ({median_residual}us) exceeds "
            f"bar={bar}us -- a systematic accounting leak, not a one-off "
            f"scheduler hiccup (debug_assertions={debug_assertions}, "
            f"worst three: {_worst_three(commits)})")
        assert p90_residual <= 3 * bar, (
            f"p90 residual over 60 commits ({p90_residual}us) exceeds "
            f"3*bar={3 * bar}us -- too many commits are elevated for this "
            f"to be a single scheduler hiccup (debug_assertions="
            f"{debug_assertions}, worst three: {_worst_three(commits)})")
        assert last_ten_median <= first_ten_median + bar, (
            f"median residual grew from {first_ten_median}us (first 10 "
            f"commits) to {last_ten_median}us (last 10 commits), more than "
            f"bar={bar}us -- consistent with an O(segments) leak, not "
            f"scheduler noise (debug_assertions={debug_assertions}, "
            f"worst three: {_worst_three(commits)})")
    finally:
        store.close()
