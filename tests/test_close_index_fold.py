"""The close index extends across a commit instead of rebuilding (D-079).

`committed_close_index()` is cached per manifest generation, so every commit
invalidates it and the next read re-reads *every* accumulated close-run file.
D-072 moved this from per-read to per-generation, which is why replay went
linear; per-generation is still O(all runs), which D-078 measured at 37 ms
with 999 runs — 370 µs per correction at batch 100.

**Why this file exists separately from `test_correction_scaling.py`.** That
gate runs 120 batches and reads 1.03: correct for the defect it guards
(D-072's per-read rebuild), and blind to this one, which needs enough
accumulated runs to dominate. A gate that cannot see a defect is not a gate
for it, so this one measures the mechanism directly rather than hoping a
batch-latency ratio surfaces it.

**The statistic.** The rebuild happens exactly once per generation, on the
first read; a later read in the same generation hits the cache. Their ratio
isolates the rebuild from the fixed seal-and-fsync cost of a commit, and
being a ratio it holds on any host.

Calibrated against the pre-fold engine, where the rebuild is real:

    close runs   first read   subsequent   ratio
            50    2272 us       12.9 us     176x
           100    4307          12.6        342x
           200    5954          11.5        519x
           300    5751           8.2        698x

Correctness comes first in this file, and the two maintenance tests matter
most: `close_runs` is *not* append-only across compaction, which folds runs
into sidecars and empties the list. An index that extends across that would
serve a stale belief, which is worse than any amount of slow.
"""

from __future__ import annotations

import statistics
import time

import pytest

from tgms.core.model import OPEN_END, clamp_tt

from .conftest import fresh_adapter

N_ENTITIES = 40
PER_BATCH = 20
RUNS = 100

# Pre-fold this reads 342x at RUNS=100 (table above); with the fold the first
# read costs one run plus a copy-on-write extend. 40x sits well clear of both.
MAX_FIRST_READ_RATIO = 40.0


def _seed(adapter, tt):
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": f"e{i}", "label": "N", "props": {"v": 0},
          "vt_s": 0, "vt_e": OPEN_END} for i in range(N_ENTITIES)], tt)
    adapter.commit()
    return tt


def _accumulate_runs(adapter, tt, runs=RUNS):
    """Every batch supersedes, so every batch writes a close run."""
    for b in range(runs):
        tt += 1
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": f"e{(b * PER_BATCH + j) % N_ENTITIES}",
              "label": "N", "props": {"v": b + 1}, "vt_s": 0, "vt_e": OPEN_END}
             for j in range(PER_BATCH)], tt)
        adapter.commit()
    return tt


def _believed_by_hand(adapter, uid):
    a = clamp_tt(OPEN_END)
    return sorted(
        (v for v in adapter.all_node_versions() if v.uid == uid and v.tt_s <= a < v.tt_e),
        key=lambda v: (v.vt_s, v.vid))


# --- correctness ---------------------------------------------------------- #


def test_extending_the_index_gives_the_same_belief_as_rebuilding():
    """The fold applies the same inserts in the same order, so it must agree
    with a full scan at every identity — not just the corrected ones."""
    adapter = fresh_adapter(paranoid=False)
    tt = _accumulate_runs(adapter, _seed(adapter, 1000))
    for i in range(N_ENTITIES):
        uid = f"e{i}"
        fast = sorted(adapter.believed_node_versions(uid), key=lambda v: (v.vt_s, v.vid))
        slow = _believed_by_hand(adapter, uid)
        assert [v.vid for v in fast] == [v.vid for v in slow], f"disagreement at {uid}"
    assert tt


def test_historical_belief_survives_extension():
    """Extending the index must not disturb what earlier transaction times
    believed — the property the whole store exists for."""
    adapter = fresh_adapter(paranoid=False)
    start = 1000
    tt = _seed(adapter, start)
    snapshots = {}
    for b in range(20):
        tt += 1
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": "e0", "label": "N",
              "props": {"v": b + 1}, "vt_s": 0, "vt_e": OPEN_END}], tt)
        adapter.commit()
        snapshots[tt] = [v.vid for v in adapter.believed_node_versions("e0", tt)]

    _accumulate_runs(adapter, tt, runs=30)
    for as_of, expected in snapshots.items():
        got = [v.vid for v in adapter.believed_node_versions("e0", as_of)]
        assert got == expected, f"belief at tt={as_of} changed after later writes"


def test_rebuild_after_compaction_empties_the_runs():
    """`close_runs` is not append-only across compaction: it folds runs into
    sidecars and empties the list. Extending across that would serve a stale
    index, so the cache must detect it and rebuild."""
    adapter = fresh_adapter(paranoid=False)
    tt = _accumulate_runs(adapter, _seed(adapter, 1000), runs=30)
    if not hasattr(adapter, "compact"):
        pytest.skip("backend has no compaction")
    before = {f"e{i}": [v.vid for v in adapter.believed_node_versions(f"e{i}")]
              for i in range(N_ENTITIES)}
    adapter.compact()
    for uid, expected in before.items():
        assert [v.vid for v in adapter.believed_node_versions(uid)] == expected

    tt += 1000
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "e0", "label": "N",
          "props": {"v": "post-compact"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.commit()
    believed = adapter.believed_node_versions("e0")
    assert len(believed) == 1 and believed[0].props["v"] == "post-compact"
    assert [v.vid for v in believed] == [v.vid for v in _believed_by_hand(adapter, "e0")]


def test_rollback_does_not_reach_the_committed_index():
    adapter = fresh_adapter(paranoid=False)
    tt = _accumulate_runs(adapter, _seed(adapter, 1000), runs=20)
    before = [v.vid for v in adapter.believed_node_versions("e0")]
    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "e0", "label": "N", "props": {"v": "doomed"},
          "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.rollback()
    assert [v.vid for v in adapter.believed_node_versions("e0")] == before


# --- the gate ------------------------------------------------------------- #


def test_first_read_after_a_commit_does_not_reread_every_close_run():
    adapter = fresh_adapter(paranoid=False)
    if not hasattr(adapter, "compact"):
        pytest.skip("the close-run rebuild is a native-engine cost")
    tt = _accumulate_runs(adapter, _seed(adapter, 1000))

    firsts, subsequents = [], []
    for k in range(7):
        tt += 1
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": "e0", "label": "N",
              "props": {"v": 500 + k}, "vt_s": 0, "vt_e": OPEN_END}], tt)
        adapter.commit()          # invalidates the cached close index

        t0 = time.perf_counter()
        adapter.believed_node_versions("e1")     # first read of the generation
        firsts.append((time.perf_counter() - t0) * 1e6)

        t0 = time.perf_counter()
        for _ in range(10):
            adapter.believed_node_versions("e1")  # cached
        subsequents.append((time.perf_counter() - t0) * 1e6 / 10)

    first = statistics.median(firsts)
    later = statistics.median(subsequents)
    ratio = first / later
    assert ratio < MAX_FIRST_READ_RATIO, (
        f"the first read after a commit costs {ratio:.0f}x a subsequent one "
        f"({first:.0f} us against {later:.1f} us) over {RUNS} close runs. The "
        f"committed close index is being rebuilt from every accumulated run "
        f"instead of extended with the one the commit added, which is "
        f"quadratic in correction volume (D-078/D-079)."
    )
