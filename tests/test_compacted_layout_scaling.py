"""The native engine's full-store scan stays flat cost per row after
`compact()`, even when compaction has produced a maximally degenerate
transaction-time layout.

`compact()` rewrites every live row into fresh segments sorted into global
`(vt_s, vid)` order (spec §5.6), but each row keeps its own origin `tt_s`.
When the generations that fed a compaction wrote *interleaved* `vt` ranges,
the resulting segment's `tt_s_runs` run-length index degrades toward one run
per row instead of one run per ingest batch — measured on the real SNB SF1
store at 2,125,704 runs over 2,997,352 rows. The read path used to resolve
each materialized row's transaction time with a linear scan over that
segment's runs, so a full scan of a heavily compacted store paid O(rows) of
scanning *per row*, i.e. the whole scan was O(rows^2): `nodes_columnar` on
the SF1 store went from 7.3s to over 1,400s (>190x) after compaction. The fix
(`crates/tgms-engine-core`: `compact.rs`, `scan.rs`, `store.rs`) replaced the
linear run scan with a binary search (`tt_s_at`), coalesced
`believed_ranges`, and O(1) prunes — none of that changes what a scan
returns, only how it pays for `tt_s_runs`.

`verify()`'s report grew two keys to make the layout itself inspectable:
`tt_s_runs` (summed over live segments) and `max_tt_s_runs` (the worst single
segment). A batch ingest writes exactly one run per segment; only
compaction, and only of interleaved generations, inflates it. Both keys
exist solely on the fixed engine, which is also this file's own dependency:
every test below needs them, so a pre-fix `tgms._engine` fails these tests
at the `verify()` call rather than passing vacuously. The A/B control that
proves the performance gate itself (not just this key check) actually fires
on the pre-fix engine did not run as a pytest file against the old checkout
— the worktree's own `tgms` package shadows the old one on `sys.path`
regardless of `PYTHONPATH` ordering when invoked from this directory, so it
was run as a standalone script from the main checkout instead, with
`_build_interleaved`, the sizes, and both thresholds copied verbatim from
this file and only the `max_tt_s_runs` assert dropped (the pre-fix engine
cannot satisfy it — its absence there is itself confirmed by an assertion in
that script). See the measured numbers in the comment above `MAX_RATIO`
below.

**Why the fixture has to interleave `vt` across generations.** If each
ingest generation instead owned a disjoint `vt` range, `compact()`'s global
`(vt_s, vid)` sort would simply concatenate the generations' runs in order —
one run per *generation*, not per row — and every gate below would be
vacuous. `_build_interleaved` below stripes generation `g`'s rows across
`vt = g, g + n_gens, g + 2*n_gens, ...` specifically so the global sort
visits every generation in strict rotation.
`test_fixture_produces_degenerate_layout` pins that this really happens
before anything downstream trusts it (the same discipline D-073 forced
`test_correction_scaling.py` and `test_batch_scaling.py` to follow: a gate
that has never seen the defect it guards is an assertion about nothing).

These run against the **native** engine specifically, whatever
TGMS_TEST_BACKEND says — `tt_s_runs` is a native segment concept with no
portable-backend equivalent, and a DuckDB or Kùzu run would pass every test
here vacuously.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

import pytest


def _native_adapter(tmp):
    """The native engine, regardless of TGMS_TEST_BACKEND (see module doc)."""
    native = pytest.importorskip(
        "tgms.storage.native",
        reason="the compacted-layout scaling gate measures the native scan path",
    )
    a = native.NativeAdapter(tmp)
    a.paranoid = False  # the paranoid disjointness check is O(versions) per op
    return a


def _node_ops(vts: list[int]) -> list[dict]:
    return [
        {"op": "assert_node", "uid": f"n{vt}", "label": "N", "props": {"v": vt},
         "vt_s": vt, "vt_e": vt + 1}
        for vt in vts
    ]


def _edge_ops(vts: list[int]) -> list[dict]:
    return [
        {"op": "assert_edge", "src": f"s{vt}", "dst": f"d{vt}", "rel_type": "R",
         "props": {"v": vt}, "vt_s": vt, "vt_e": vt + 1}
        for vt in vts
    ]


def _build_interleaved(tmp: Path, n_rows: int, rows_per_batch: int = 100,
                        with_edges: bool = False):
    """A store ready for `compact()` whose `vt` values interleave across
    `n_gens = n_rows // rows_per_batch` ingest generations: generation `g`
    writes `vt = g, g + n_gens, g + 2*n_gens, ...`, so sorting the whole
    store into global `(vt_s, vid)` order — exactly what `compact()` does —
    visits every generation in strict rotation, and the compacted segment
    ends up with one run per row regardless of how large `n_gens` is (any
    value >= 2 already makes every pair of adjacent sorted rows come from
    different generations). See the module docstring for why a
    disjoint-per-generation `vt` assignment would make the repro void.

    `rows_per_batch` caps ops per `apply_ops` call rather than growing it
    with `n_rows`, deliberately: `apply_ops`'s own per-batch cost is a
    separate, still-open O(k^2) defect in the staged-row buffer this file
    does not test (`test_batch_scaling.py`'s job), and a single multi-
    thousand-op batch would make *building* the fixture the dominant cost
    here instead of the compacted scan this file exists to gate. A fixed
    500-op ceiling keeps every batch cheap regardless of `n_rows`.
    """
    adapter = _native_adapter(tmp)
    n_gens = n_rows // rows_per_batch
    assert n_gens >= 8, f"n_rows={n_rows} at rows_per_batch={rows_per_batch} gives only {n_gens} generations"
    assert n_rows % n_gens == 0, "n_rows must divide n_gens evenly for a clean stride"
    tt = 1000
    for g in range(n_gens):
        tt += 1
        vts = [i * n_gens + g for i in range(rows_per_batch)]
        ops = _node_ops(vts)
        if with_edges:
            ops += _edge_ops(vts)
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()
    return adapter


def _sorted_by_vid(versions):
    return sorted(versions, key=lambda v: v.vid)


# --- 1. the fixture is real: compaction actually degrades this layout ----- #


def test_fixture_produces_degenerate_layout(tmp_path):
    adapter = _build_interleaved(tmp_path / "s", n_rows=800, rows_per_batch=100)

    pre = adapter.verify()
    assert pre["healthy"], pre["problems"]
    assert pre["max_tt_s_runs"] == 1, (
        "a batch ingest writes exactly one transaction-time run per segment; "
        f"got {pre['max_tt_s_runs']} before any compaction"
    )

    adapter.compact()
    post = adapter.verify()
    assert post["healthy"], post["problems"]
    assert post["max_tt_s_runs"] > 1, (
        f"the interleaved fixture did not degrade the compacted layout "
        f"(max_tt_s_runs={post['max_tt_s_runs']} after compact()) — on a "
        f"healthy-by-luck layout every gate in this file would be vacuous"
    )
    # this shape is deliberately the worst case, not just a bad one: every
    # row lands in its own run, matching the field measurement's near-1:1
    # runs/rows ratio (2,125,704 / 2,997,352 on the real SNB SF1 store).
    assert post["max_tt_s_runs"] == post["rows"] == 800, (
        f"expected full saturation (one run per row): "
        f"max_tt_s_runs={post['max_tt_s_runs']}, rows={post['rows']}"
    )


# --- 2. correctness: compact() must not change what the store answers ----- #


def test_compaction_preserves_full_node_and_edge_listing(tmp_path):
    adapter = _build_interleaved(tmp_path / "s", n_rows=800, rows_per_batch=100, with_edges=True)

    nodes_before = _sorted_by_vid(adapter.all_node_versions())
    edges_before = _sorted_by_vid(adapter.all_edge_versions())
    assert len(nodes_before) == 800
    assert len(edges_before) == 800

    report = adapter.compact()
    assert adapter.verify()["healthy"]

    nodes_after = _sorted_by_vid(adapter.all_node_versions())
    edges_after = _sorted_by_vid(adapter.all_edge_versions())

    assert len(nodes_after) == len(nodes_before), (
        f"node count changed across compact(): "
        f"{len(nodes_before)} -> {len(nodes_after)} (report={report})"
    )
    assert len(edges_after) == len(edges_before), (
        f"edge count changed across compact(): "
        f"{len(edges_before)} -> {len(edges_after)} (report={report})"
    )
    # dataclass equality covers every field (uid/vid/label/vt/tt/props/...),
    # so this is a full content digest, not just counts
    assert nodes_after == nodes_before, "compact() changed the node listing"
    assert edges_after == edges_before, "compact() changed the edge listing"


# --- 3. the performance gate ------------------------------------------------ #
#
# Measured on this machine, native engine, this file's own fixture and
# statistic, both engines built from the same worktree state:
#
#   post-fix: this worktree's tgms/_engine.cpython-312-darwin.so (the fix
#             described in the module docstring, already built)
#   pre-fix:  the main checkout's tgms/_engine.cpython-312-darwin.so,
#             confirmed genuinely pre-fix because its verify() report lacks
#             `tt_s_runs`/`max_tt_s_runs` entirely — those keys exist only
#             post-fix (see module docstring)
#
# N_SMALL=16,000 / N_BIG=64,000 rows, both built in ROWS_PER_BATCH=500-row
# interleaved generations (32 and 128 generations respectively) and compacted
# (max_tt_s_runs == rows on *both* sizes, *both* engines — the compacted
# segment really is one run per row either way, so this is an apples-to-apples
# comparison of scan cost against an identical layout):
#
#              per-row scan cost: N_SMALL -> N_BIG        ratio (big/small)
#   post-fix   0.468-0.479us -> 0.510-0.522us (3 fresh runs)   1.07x - 1.10x
#   pre-fix    3.117-3.132us -> 10.899-10.924us (2 fresh runs)  3.49x - 3.50x
#
# matching the mechanism exactly: pre-fix, the read path re-scans every
# tt_s_runs entry up to a materialized row's position, so per-row cost
# itself grows with the compacted segment's row count and the whole scan is
# O(rows^2); post-fix it's a binary search, so per-row cost does not depend
# on how many rows are in the segment. The gap between engines is large and
# the ratio is stable rep to rep (post-fix never left 1.02x-1.10x across five
# total runs measured while setting this threshold; pre-fix never left
# 3.12x-3.56x across the runs that built the table above and the one at the
# bottom of this file's A/B script).
#
# MAX_RATIO=2.0 sits ~1.8x above the noisiest healthy rep observed (1.10x)
# and ~1.7x below the weakest defect rep observed (3.49x) — a loaded host
# has plenty of room to run slower without a false failure, and a partial
# regression has to close most of that 3.49x/1.10x ~= 3.2x gap before it
# slips through.
MAX_RATIO = 2.0

# A second, independent signal: the absolute per-row cost at N_BIG. Pre-fix
# measured 10.90-10.92us/row at 64,000 rows; post-fix measured 0.51-0.52us/row
# at the same size. 5.0us/row sits ~10x above the healthiest rep observed and
# ~2.2x below the defect reps — wider margin against noise than against the
# defect, which is why MAX_RATIO above (host-speed independent) is the
# primary signal and this is a belt-and-suspenders check on top of it.
MAX_ABS_US_PER_ROW = 5.0

N_SMALL = 16_000
N_BIG = 64_000
ROWS_PER_BATCH = 500


def _per_row_scan_cost(adapter, reps: int) -> float:
    """Median wall-clock seconds per row for a full `nodes_columnar` scan.
    Warms the store first: the first scan after opening / compacting may
    build caches (e.g. the segment-name map, D-077) that a cold-store timing
    would wrongly attribute to the scan itself."""
    adapter.nodes_columnar()  # warm-up, not timed
    times = []
    cols = None
    for _ in range(reps):
        start = time.perf_counter()
        cols = adapter.nodes_columnar()
        times.append(time.perf_counter() - start)
    return statistics.median(times) / len(cols["uid"])


def test_full_node_scan_cost_stays_flat_as_compacted_store_grows(tmp_path):
    small = _build_interleaved(tmp_path / "small", n_rows=N_SMALL, rows_per_batch=ROWS_PER_BATCH)
    small.compact()
    small_report = small.verify()
    assert small_report["healthy"], small_report["problems"]
    assert small_report["max_tt_s_runs"] == N_SMALL, (
        f"N_SMALL fixture did not saturate to one run per row: "
        f"max_tt_s_runs={small_report['max_tt_s_runs']}, rows={N_SMALL}"
    )
    small_per_row = _per_row_scan_cost(small, reps=5)
    small.close()

    big = _build_interleaved(tmp_path / "big", n_rows=N_BIG, rows_per_batch=ROWS_PER_BATCH)
    big.compact()
    big_report = big.verify()
    assert big_report["healthy"], big_report["problems"]
    assert big_report["max_tt_s_runs"] == N_BIG, (
        f"N_BIG fixture did not saturate to one run per row: "
        f"max_tt_s_runs={big_report['max_tt_s_runs']}, rows={N_BIG}"
    )
    big_per_row = _per_row_scan_cost(big, reps=3)
    big.close()

    ratio = big_per_row / small_per_row
    assert ratio < MAX_RATIO, (
        f"full node scan per-row cost grew {ratio:.2f}x from a "
        f"{N_SMALL}-row compacted store to a {N_BIG}-row one (limit "
        f"{MAX_RATIO}x): {small_per_row * 1e6:.3f}us/row -> "
        f"{big_per_row * 1e6:.3f}us/row. Both stores have max_tt_s_runs "
        f"equal to their row count, so this is not a layout difference — "
        f"the read path is paying more per row as the compacted segment "
        f"itself grows, which is the O(rows)-per-row tt_s_runs scan the fix "
        f"(binary-search tt_s_at, coalesced believed_ranges, O(1) prunes) "
        f"was written to remove."
    )
    assert big_per_row * 1e6 < MAX_ABS_US_PER_ROW, (
        f"full node scan on the {N_BIG}-row compacted store (max_tt_s_runs "
        f"== rows) cost {big_per_row * 1e6:.3f}us/row, over the "
        f"{MAX_ABS_US_PER_ROW}us/row bound measured against the fixed "
        f"engine's 0.51-0.52us/row"
    )


# --- 4. verify() surfaces the layout metric, not just healthy/unhealthy --- #


def test_verify_reports_tt_s_run_keys(tmp_path):
    adapter = _native_adapter(tmp_path / "s")
    adapter.begin()
    adapter.apply_ops(_node_ops(list(range(50))), 1000)
    adapter.commit()

    report = adapter.verify()
    assert report["healthy"], report["problems"]
    assert isinstance(report["tt_s_runs"], int)
    assert isinstance(report["max_tt_s_runs"], int)
    assert report["max_tt_s_runs"] == 1, (
        "a freshly ingested, uncompacted segment writes exactly one "
        f"transaction-time run; got {report['max_tt_s_runs']}"
    )
