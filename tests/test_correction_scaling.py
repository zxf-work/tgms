"""The correction path stays linear as belief history accumulates (D-073).

The standing gate for the defect D-072 found already fixed but still
recorded as open. Two engine changes made the correction path linear —
`de5071b` routed `close_version` through the WP-N4 vid postings instead of
a linear scan, and the same commit cached `close_index()` per manifest
generation instead of rebuilding it from every accumulated close run on
every read. Before them, replay at 20% correction density took 2,856.8 s
against 361.1 s after (docs/eval_bitemporal.md).

**What this file asserts, and why it is a ratio.** Absolute timings are a
property of the host, so nothing here asserts one. The defect's signature
is *marginal*: with the per-read rebuild in place, the k-th batch of
corrections re-reads k close runs, so batch cost grows with the number of
batches already committed and total cost is quadratic. Flat marginal cost
is therefore the linearity test, and the ratio of late batches to early
batches is the same number on any machine.

`test_correction_cost_is_flat_as_close_runs_accumulate` sweeps the axis the
one-off §13 experiment could not: cost *within* a run as history deepens,
rather than cost across separately built stores.

**The threshold was set against the real defect, not chosen.** The pre-fix
engine was rebuilt from `de5071b^` and measured on this shape; see the table
above `MAX_MARGINAL_RATIO`. The first configuration tried was blind to the
bug, which is the whole reason the control was run.

These run against the **native** engine specifically, whatever
TGMS_TEST_BACKEND says — the defect was in the native store's close layer,
and a DuckDB run would pass it vacuously.
"""

from __future__ import annotations

import statistics
import tempfile
import time

import pytest

# --- the threshold, and the control that set it --------------------------- #
#
# These constants are not guesses. The pre-fix engine (`de5071b^` = 8136ecb)
# was built in a worktree and measured against this exact shape, because a
# gate that has never seen the defect it guards is an assertion about
# nothing (D-073).
#
#   config (entities/batches/per-batch)   pre-fix   post-fix
#   -----------------------------------   -------   --------
#   60  /  24 /  10                        1.13x      1.03x   <- blind
#   60  / 120 /  10                        1.70x       —
#   200 /  60 /  50                        2.65x      1.12x
#   200 / 120 /  50                        3.81x      1.03x   <- chosen
#
# and, re-verified through this file's own assertion after the statistic
# was changed from mean to median: 3.35x pre-fix, 1.03x post-fix.
#
# The first row is the instructive one: the obvious small config **does not
# catch the bug**. The defect is paid per *read* against the close runs
# accumulated so far, so the signal needs both enough committed batches
# (close runs to re-read) and enough corrections per batch (reads to pay it)
# before it clears the fixed per-commit cost of seal + fsync + manifest.
#
# At the chosen config the pre-fix engine's per-batch time climbs 25 ms ->
# 200 ms linearly across the run, which is the quadratic signature. 2.0
# sits ~1.9x under the defect and ~1.9x over the healthy path, so neither a
# loaded CI runner nor a modest regression lands ambiguously.
MAX_MARGINAL_RATIO = 2.0

N_ENTITIES = 200
N_BATCHES = 120
PER_BATCH = 50


def _native_adapter(tmp):
    """The native engine, regardless of TGMS_TEST_BACKEND (see module doc)."""
    native = pytest.importorskip(
        "tgms.storage.native",
        reason="the correction-scaling gate measures the native close layer",
    )
    a = native.NativeAdapter(tmp)
    a.paranoid = False  # the paranoid disjointness check is O(versions) per op
    return a


def _seed(adapter, tt: int) -> None:
    adapter.begin()
    adapter.apply_ops(
        [
            {"op": "assert_node", "uid": f"e{i}", "label": "N", "props": {"v": 0},
             "vt_s": 0, "vt_e": 1_000_000}
            for i in range(N_ENTITIES)
        ],
        tt,
    )
    adapter.commit()


def _timed_correction_batches(adapter, tt: int) -> list[float]:
    """Per-batch wall time, ms. Each correction targets a distinct entity so
    per-identity history depth stays fixed and the only thing growing is the
    number of committed close runs — the quantity the defect re-read."""
    times: list[float] = []
    for b in range(N_BATCHES):
        tt += 1
        ops = [
            {"op": "assert_node", "uid": f"e{(b * PER_BATCH + j) % N_ENTITIES}",
             "label": "N", "props": {"v": b + 1}, "vt_s": 0, "vt_e": 1_000_000}
            for j in range(PER_BATCH)
        ]
        start = time.perf_counter()
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()
        times.append((time.perf_counter() - start) * 1000)
    return times


def test_correction_cost_is_flat_as_close_runs_accumulate():
    with tempfile.TemporaryDirectory(prefix="tgms-corr-scale-") as tmp:
        adapter = _native_adapter(tmp)
        _seed(adapter, 1000)
        times = _timed_correction_batches(adapter, 1000)

    # median, not mean: the defect is a *trend* across batches, which both
    # statistics capture, but a shared CI runner contributes one-off spikes
    # that only the mean would mistake for one. Measured identically on both
    # engines when this was swapped in.
    quartile = max(1, N_BATCHES // 4)
    early = statistics.median(times[:quartile])
    late = statistics.median(times[-quartile:])
    ratio = late / early

    assert ratio < MAX_MARGINAL_RATIO, (
        f"correction cost grows with accumulated belief history: the last "
        f"{quartile} batches have a median of {late:.1f} ms against "
        f"{early:.1f} ms for the first {quartile} "
        f"({ratio:.2f}x, limit {MAX_MARGINAL_RATIO}x). "
        f"A correction batch is paying for the corrections before it, which "
        f"is the quadratic shape close_index()'s per-generation cache and the "
        f"WP-N4 postings removed (D-072/D-073). Per-batch ms: "
        f"{[round(t, 1) for t in times]}"
    )


def test_corrections_to_one_identity_stay_answerable():
    """The other half of the shape: depth on a *single* identity.

    F3 in docs/bench_corrections.md predicts this axis is linear per
    correction rather than flat, because a correction resolves its target
    through `believed_*_versions(identity)`, which returns every version of
    that identity. This test does not assert a slope — it pins the
    *correctness* half so a future optimization of that path cannot quietly
    lose a version, which is the failure D-059 caught on the same code.
    """
    with tempfile.TemporaryDirectory(prefix="tgms-corr-depth-") as tmp:
        adapter = _native_adapter(tmp)
        tt = 1000
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": "deep", "label": "N", "props": {"v": 0},
              "vt_s": 0, "vt_e": 1_000_000}],
            tt,
        )
        adapter.commit()

        depth = 40
        for i in range(depth):
            tt += 1
            adapter.begin()
            adapter.apply_ops(
                [{"op": "assert_node", "uid": "deep", "label": "N",
                  "props": {"v": i + 1}, "vt_s": 0, "vt_e": 1_000_000}],
                tt,
            )
            adapter.commit()

        believed = adapter.believed_node_versions("deep")
        assert len(believed) == 1, (
            f"a whole-interval overwrite leaves exactly one believed version; "
            f"got {len(believed)}"
        )
        assert believed[0].props["v"] == depth

        # every superseded version is retained and closed, none lost: the
        # store's whole purpose is that a revised belief is still on record
        all_versions = [v for v in adapter.all_node_versions() if v.uid == "deep"]
        assert len(all_versions) == depth + 1, (
            f"expected {depth + 1} retained versions after {depth} corrections, "
            f"got {len(all_versions)} — a correction dropped history"
        )
