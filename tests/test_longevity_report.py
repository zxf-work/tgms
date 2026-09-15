"""`scripts/longevity_report.py`'s two Gate E reporting fixes, both found by
reading (not re-running) `benchmarks/longevity-v1/`'s 24h soak:

1. `digest_equal: null` ("the disk guard skipped the replay, never
   checked") used to be coerced to `False` (`bool(None)`), so a skipped
   replay's "deterministic final state" row read FAIL, indistinguishable
   from a replay that actually ran and found a mismatch. It must now read
   `NOT COMPUTED (replay skipped: ...)` and say Gate E is inconclusive on
   that row.
2. The "no unbounded memory" row's naive first-vs-last RSS slope is
   dominated by the once-per-restart saw-tooth (every writer life is a
   fresh OS process). `median_within_life_slope` computes the median
   per-life linear-regression slope instead, splitting samples at each
   life's own restart time (`recoveries.jsonl`'s `t_death`) rather than by
   guessing from the series itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_report as LREP  # noqa: E402


def _base_summary(**overrides) -> dict:
    summary = {
        "verify_healthy": True,
        "digest_equal": True,
        "error_count": 0,
        "recoveries": 0,
        "reader_restarts": 0,
        "memory_slope_kb_per_s": 1.0,
        "metadata_growth_slope_bytes_per_s": {"manifests": 0.0, "segments": 0.0},
        "drift": {},
        "compaction_stall_max_reader_p99_ms": 0.0,
    }
    summary.update(overrides)
    return {"summary": summary}


def _row(rows_text: str, name: str) -> str:
    lines = rows_text.splitlines()
    for i, line in enumerate(lines):
        if name in line:
            return lines[i] + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
    raise AssertionError(f"row {name!r} not found in:\n{rows_text}")


# --------------------------------------------------------------------------- #
# digest_equal / replay_skipped -> NOT COMPUTED, never FAIL                   #
# --------------------------------------------------------------------------- #


def test_skipped_replay_is_not_computed_not_fail() -> None:
    manifest = _base_summary(
        digest_equal=None,
        replay_skipped={"reason": "projected_replay_exceeds_limit",
                        "total_batches": 1074952, "projected_mb": 280791798.0,
                        "limit_mb": 20000.0},
    )
    text, markdown = LREP.build_table(manifest)

    assert "FAIL" not in _row(text, "deterministic final state")
    assert "NOT COMPUTED (replay skipped: projected_replay_exceeds_limit)" in text
    assert "inconclusive" in text.lower()
    # the projection must be printed with an explicit unit, in both MB and
    # TB, so a units mixup (this run's own "~268 PB" mislabeling) cannot
    # recur silently.
    assert "280,791,798.0 MB" in text
    assert "TB" in text
    assert "NOT COMPUTED" in markdown
    assert "inconclusive" in markdown.lower()


def test_computed_digest_mismatch_is_still_fail() -> None:
    """A replay that actually ran and found a mismatch is a real FAIL, not
    NOT COMPUTED — only a `None` (never checked) gets the new treatment."""
    manifest = _base_summary(digest_equal=False, replay_skipped=None)
    text, _ = LREP.build_table(manifest)
    row = _row(text, "deterministic final state")
    assert "FAIL" in row
    assert "NOT COMPUTED" not in row


def test_computed_digest_match_is_pass() -> None:
    manifest = _base_summary(digest_equal=True, replay_skipped=None)
    text, _ = LREP.build_table(manifest)
    row = _row(text, "deterministic final state")
    assert "PASS" in row


# --------------------------------------------------------------------------- #
# median within-life RSS slope                                                #
# --------------------------------------------------------------------------- #


def test_median_within_life_slope_is_zero_for_a_flat_sawtooth() -> None:
    """Flat within each life, rising baselines across lives (the real
    soak's own shape, minus the within-life growth) — the naive first-vs-
    last slope over the whole series is strongly positive (baselines rise
    100 -> 320), but the within-life median must read ~0."""
    points: list[tuple[float, float]] = []
    restart_times: list[float] = []
    t = 0.0
    baselines = [100.0, 150.0, 300.0, 320.0]
    for i, base in enumerate(baselines):
        for _ in range(5):
            points.append((t, base))
            t += 60.0
        if i < len(baselines) - 1:
            restart_times.append(t - 30.0)   # the restart happened mid-gap
            t += 10.0

    naive_slope = (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])
    assert naive_slope > 0.05, "the naive series should show a rising trend"

    median_slope = LREP.median_within_life_slope(points, restart_times)
    assert median_slope is not None
    assert abs(median_slope) < 1e-9


def test_median_within_life_slope_detects_a_real_within_life_leak() -> None:
    """Every life grows linearly at the same rate; restarts reset the
    baseline down each time. The naive first-vs-last slope over the whole
    series can even read *negative* (last life's baseline is lower than the
    first life's peak), while the within-life median correctly reports the
    real per-life growth rate."""
    points: list[tuple[float, float]] = []
    restart_times: list[float] = []
    t = 0.0
    for life in range(3):
        life_start = 500.0 - life * 50.0   # baseline drifts down life-to-life
        for k in range(6):
            points.append((t, life_start + 2.0 * k))   # +2 units/sample within-life
            t += 10.0
        if life < 2:
            restart_times.append(t - 5.0)
            t += 5.0

    naive_slope = (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])
    median_slope = LREP.median_within_life_slope(points, restart_times)
    assert median_slope is not None
    # within-life rate is 2.0 units per 10s = 0.2 units/s in every life
    assert abs(median_slope - 0.2) < 1e-9
    assert median_slope != naive_slope


def test_median_within_life_slope_none_when_no_points() -> None:
    assert LREP.median_within_life_slope([], []) is None
    assert LREP.median_within_life_slope([(0.0, 1.0)], []) is None  # single point


def test_memory_row_falls_back_when_side_files_unavailable() -> None:
    """No rss_points/restart_times given (the real soak's own situation
    locally: metrics.jsonl stays on xzgpu) — the row must still print the
    first-vs-last figure, labelled, and say the within-life figure is n/a,
    without crashing."""
    manifest = _base_summary(memory_slope_kb_per_s=111.639)
    text, _ = LREP.build_table(manifest, rss_points=None, restart_times=None)
    row = _row(text, "no unbounded memory")
    assert "111.639" in row
    assert "n/a" in row
