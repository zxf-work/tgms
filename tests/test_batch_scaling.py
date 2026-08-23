"""The native engine's per-op batch cost stays flat as a single batch grows.

The standing gate for the O(k^2)-per-batch defect fixed in `staging.rs` /
`read.rs`. `StorageAdapter.apply_ops` (`tgms/storage/base.py`) calls
`believed_node_versions` / `believed_edge_versions` once per assert op, and
those ended in a full sweep of the *open batch's* staged rows: for edges,
every staged row paid a fresh `uid_of` lookup plus a sha256 `edge_eid`
derivation. With `k` ops staged, op *i* paid O(i) of that work, so a single
`apply_ops` call over `k` ops cost O(k^2) — the k-th op re-derived the same
identity of every op before it, not just its own. `staging.rs` now carries a
lazy, per-identity index into the staged-row buffer so each row is keyed once
per batch, not once per read.

**What this file asserts, and why it is a ratio.** Absolute timings are a
property of the host, so nothing here asserts one. The defect's signature is
per-op cost that grows with the size of the *open* batch: doubling k should
double wall time (linear, flat per-op cost) if the fix holds, and roughly
quadruple it (per-op cost also doubling) if it does not. The statistic is

    (time(one k_big-op apply_ops batch) / k_big)
    / (time(one k_small-op apply_ops batch) / k_small)

— the per-op cost of a big batch relative to a small one, inside a single
`apply_ops` call, on a fresh store each time so no cross-batch state leaks in.
A ratio near 1 means per-op cost did not grow with batch size; a ratio well
above 1 means it did. This is deliberately a different axis from
`test_correction_scaling.py`, which holds batch size fixed and grows the
*store* (committed close runs) between separate `apply_ops` calls — the
defect here was paid entirely *within* one open batch, before anything is
committed, so a store-growth sweep would not touch it. It is also why this
file does not duplicate that store-growth control: it is already covered
there, and adding it here would not add signal.

These run against the **native** engine specifically, whatever
TGMS_TEST_BACKEND says — the defect was in the native engine's staged-row
buffer, which DuckDB and Kùzu do not have (both were measured linear on this
shape already); a DuckDB or Kùzu run would pass this vacuously.
"""

from __future__ import annotations

import statistics
import tempfile
import time

import pytest

# --- the threshold, and the control that set it --------------------------- #
#
# Not a guess: measured on this machine against both the pre-fix engine
# (`scratchpad/_engine.prefix.so`, built from the commit before the staged-row
# index landed) and the post-fix engine, using this file's own statistic and
# op shapes (`node_ops`/`edge_ops` below), K_SMALL=500 / K_BIG=8000. Five
# fresh-process reps each (`_batch_scaling_ratio` invoked directly, and
# independently via `pytest -q` for the first pre-fix row):
#
#              pre-fix statistic (5 reps)             post-fix statistic (5 reps)
#   node    13.89, 14.01, 13.93, 13.92, 13.78x        0.97, 0.95, 1.02, 1.02, 1.06x
#   edge    14.83, 14.86, 14.74, 14.82, 14.86x        1.00, 1.02, 1.05, 1.01, 1.01x
#
# matching the field measurement that surfaced the defect (the Paper A SF1
# load, xzgpu: 0.063 s at 500 ops -> 6.888 s at 8,000, per-op 126 us ->
# 861 us; D-145): pre-fix per-op cost multiplies ~14x (node) to ~15x
# (edge) from a 500-op batch to an 8,000-op batch; post-fix it is flat
# (0.95x-1.06x, pure noise at these small absolute times — a post-fix
# 500-op batch is a few ms). MAX_RATIO=4.0 sits ~3.4x under the smallest
# observed defect ratio (13.78x) and ~3.8x over the noisiest healthy one
# (1.06x), so neither a loaded CI runner nor a modest regression lands
# ambiguously.
MAX_RATIO = 4.0

K_SMALL = 500
K_BIG = 8000
# post-fix a 500-op batch is ~4-5ms wall time, small enough that host
# scheduling noise is a real fraction of it (a single rep varied 1.0x-1.6x
# across runs during threshold-setting); seven reps and taking the median
# tames that without adding meaningful runtime (~35ms of work). The 8,000-op
# batch is the big signal (pre-fix ~7-20s, post-fix ~0.1s) and noise on it is
# negligible by comparison, so one rep is enough.
K_SMALL_REPS = 7


def _native_adapter(tmp):
    """The native engine, regardless of TGMS_TEST_BACKEND (see module doc)."""
    native = pytest.importorskip(
        "tgms.storage.native",
        reason="the batch-scaling gate measures the native staged-row buffer",
    )
    a = native.NativeAdapter(tmp)
    a.paranoid = False  # the paranoid disjointness check is O(versions) per op
    return a


def node_ops(n: int) -> list[dict]:
    """n assert_node ops, each a distinct identity."""
    return [
        {"op": "assert_node", "uid": f"n{i}", "label": "N", "props": {"v": i},
         "vt_s": 0, "vt_e": 1_000_000}
        for i in range(n)
    ]


def edge_ops(n: int) -> list[dict]:
    """n assert_edge ops, each a distinct identity (distinct src and dst, so
    no op shares an endpoint with another — ensure_entities never repeats a
    uid within the batch either)."""
    return [
        {"op": "assert_edge", "src": f"s{i}", "dst": f"d{i}", "rel_type": "R",
         "props": {"v": i}, "vt_s": 0, "vt_e": 1_000_000}
        for i in range(n)
    ]


def _time_one_batch(make_ops, n: int) -> float:
    """Wall-clock seconds for a single `apply_ops` call over n ops, on a
    fresh store, inside one begin/commit."""
    with tempfile.TemporaryDirectory(prefix="tgms-batch-scale-") as tmp:
        adapter = _native_adapter(tmp)
        ops = make_ops(n)
        adapter.begin()
        start = time.perf_counter()
        adapter.apply_ops(ops, 1000)
        elapsed = time.perf_counter() - start
        adapter.commit()
        adapter.close()
    return elapsed


def _batch_scaling_ratio(make_ops) -> float:
    small_per_op = [
        _time_one_batch(make_ops, K_SMALL) / K_SMALL for _ in range(K_SMALL_REPS)
    ]
    small = statistics.median(small_per_op)
    big = _time_one_batch(make_ops, K_BIG) / K_BIG
    return big / small


def test_assert_node_batch_cost_stays_flat_as_batch_grows():
    ratio = _batch_scaling_ratio(node_ops)
    assert ratio < MAX_RATIO, (
        f"assert_node per-op cost grew {ratio:.2f}x from a {K_SMALL}-op batch "
        f"to a {K_BIG}-op batch (limit {MAX_RATIO}x). A single apply_ops call "
        f"is paying more per op as its own open batch grows, which is the "
        f"O(k^2)-per-batch shape the staged-row identity index in staging.rs "
        f"was added to remove."
    )


def test_assert_edge_batch_cost_stays_flat_as_batch_grows():
    ratio = _batch_scaling_ratio(edge_ops)
    assert ratio < MAX_RATIO, (
        f"assert_edge per-op cost grew {ratio:.2f}x from a {K_SMALL}-op batch "
        f"to a {K_BIG}-op batch (limit {MAX_RATIO}x). Edges were the worse "
        f"case pre-fix (a sha256 edge_eid derivation per staged row on every "
        f"lookup), so this is the sharper of the two halves of the gate."
    )
