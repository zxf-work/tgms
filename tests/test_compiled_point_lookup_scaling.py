"""Compiled `entity_history`'s `NodeScan(uids=[uid])` takes the postings
point-read, not a full-store scan, and stays flat cost per lookup as the
store grows.

**The regression this guards (P2, fixed in 62709b3).** `entity_history`'s
compiled expansion (`tgms/tgir/compiled/entity_history.py::rows_plan`) is a
single `NodeScan(uids=[uid], belief="current")` — one identity. Before
62709b3, `scan_nodes` (`tgms/tgir/eval/scan.py`) had no notion of a bind-time
anchor: because `entity_history`'s payload carries `tt_s`/`tt_e`, which
`{nodes,edges}_columnar` cannot produce, `needs_fallback` sent every such scan
through `versions_columnar` — a struct-of-arrays over **every version ever
written**, of that kind, regardless of how many uids the plan actually named.
A one-identity lookup paid full-store-scan cost, and that cost grew with the
store: measured locally at 292.7x the kernel at 1M versions and 446.9x at
10M, widening with scale (`_anchored`'s docstring in `scan.py` has the fuller
table). The fix adds two functions in `scan.py`:

- `_anchored(node, adapter)` — true when a scan should take the postings
  point-read instead of a scan: a bind-time `node.uids` exists, `node.belief
  == "current"` (the only belief mode `believed_node_versions`'s `as_of_tt`
  argument can serve), and `len(node.uids) * PROBE_COST_RATIO < stored_count`
  (`PROBE_COST_RATIO = 20`, the engine's own probe-vs-scan constant, reused
  rather than reinvented).
- `_nodes_by_uid(adapter, node)` — the anchor set's versions via
  `adapter.believed_node_versions`, one identity through the open-version
  index, producing the same columns `_nodes_fast`/`_versions_fallback`
  produce so everything downstream (Σ masking, post-filters, `_assemble`) is
  the same code either way.

`scan_nodes` checks `_anchored` *before* `needs_fallback`, so an anchored scan
never reaches `versions_columnar` at all, regardless of what columns it wants.

**No pytest gate pinned this before this file.** The only thing that measured
it was the offline `scripts/bench_compiled_vs_kernel.py` (E14 §C2, a
kernel-vs-compiled ratio bound, run by hand against multi-million-row stores
and not part of the pytest suite) — nothing failed a `pytest` run if this
regressed.

**Two independent claims, two tests.** Route selection and flat scaling are
not the same fact: a plan could take the postings route on every call and
still degrade with store size (if, say, the postings lookup itself scanned
something proportional to the store), and a plan could stay flat while
routing through some other mechanism entirely. `_anchored`'s own C2 defect
was specifically the combination — wrong route, and the wrong route's cost
grows with the store — so both are asserted, separately:

1. `test_uid_anchored_entity_history_takes_postings_route` spies on the
   adapter's own `believed_node_versions` and `versions_columnar` — the two
   methods `_anchored`'s branch chooses between — rather than reaching into
   `scan.py`'s private `_anchored`/`_nodes_by_uid` functions directly. A call
   count on the adapter boundary is the least invasive observable available:
   it says exactly what code the pre-fix regression is about (which adapter
   method actually ran) without coupling the test to `scan.py`'s internal
   names, so a future refactor of *how* the routing decision is reached does
   not break this test as long as the route itself does not regress.
2. `test_entity_history_point_lookup_cost_stays_flat_as_store_grows` measures
   wall-clock median per-call cost at two store sizes 20x apart and asserts
   the ratio stays near 1. This is deliberately wall-clock, not a row/call
   count: unlike `test_compacted_layout_scaling.py`'s full-store scan (where
   the row count read *is* the cost driver and a fixed quantity either way),
   the whole point of the fix is that the postings route's own row/call count
   does **not** grow with the store — it is always one `believed_node_versions`
   call returning one identity's versions, on both sides of the fix and at
   every store size. A pre-fix plan and a post-fix plan issue the *same*
   number of top-level calls (one scan, one fallback-or-not); what differs is
   what each call costs internally, which is only observable as wall time.
   Generous teeth (§ below) keep it from flaking on a loaded host.

**Why `n_node_versions == n_nodes` after the build.** Both stores in test 2
are built from disjoint uids with one version each and no corrections, so
`stats()["n_node_versions"]` is asserted equal to the requested size — a
cheap check that the fixture actually has the row count the scaling claim
assumes, the same way `test_compacted_layout_scaling.py` pins `max_tt_s_runs
== rows` before trusting its own scaling numbers.

**Native only, deliberately.** `_anchored`/`_nodes_by_uid` live in the shared
`tgms/tgir/eval/scan.py` and call only `adapter.stats()`,
`adapter.believed_node_versions()` and `adapter.versions_columnar()` — every
backend (`NativeAdapter`, `DuckDBAdapter`, `KuzuAdapter`) implements all
three, so the *routing* claim (test 1) is not native-specific. The *scaling*
claim (test 2) is scoped to native anyway, for two reasons: it is where the
regression was actually measured (`scripts/bench_compiled_vs_kernel.py`'s
default stores are native, and the 292.7x/446.9x numbers in `_anchored`'s own
docstring are native numbers), and a wall-clock comparison needs a backend
whose per-call cost is not dominated by something else (DuckDB's SQL
planning overhead, a network round trip) that would swamp the signal this
file exists to measure. `pytest.importorskip` skips both tests together, in
the same style as `test_compacted_layout_scaling.py`, when the native
extension is not built.

See the comment above `MAX_RATIO` for the measured numbers that set the
scaling test's thresholds, including the pre-fix control run.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest


def _native_adapter(tmp):
    """The native engine specifically (see module doc's "Native only")."""
    native = pytest.importorskip(
        "tgms.storage.native",
        reason="the compiled point-lookup scaling gate measures the native "
               "postings route",
    )
    a = native.NativeAdapter(tmp)
    a.paranoid = False  # the paranoid disjointness check is O(versions) per op
    return a


def _build_store(tmp: Path, n_nodes: int, batch: int = 5000):
    """`n_nodes` distinct single-version nodes, no edges, no corrections —
    the minimal shape a uid-anchored `entity_history` lookup needs, and one
    whose `n_node_versions` is exactly `n_nodes` (test 2 checks this).

    Batched at a fixed `batch` size rather than growing it with `n_nodes`,
    deliberately: `apply_ops`'s own per-batch cost is a separate, already
    gated concern (`test_batch_scaling.py`), not what this file measures, and
    a fixed batch ceiling keeps fixture *construction* cheap regardless of
    how large `n_nodes` gets.
    """
    adapter = _native_adapter(tmp)
    tt = 1000
    i = 0
    while i < n_nodes:
        k = min(batch, n_nodes - i)
        ops = [
            {"op": "assert_node", "uid": f"n{j}", "label": "N", "props": {"v": j},
             "vt_s": j, "vt_e": j + 1}
            for j in range(i, i + k)
        ]
        tt += 1
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()
        i += k
    return adapter


def _spy(monkeypatch, adapter, name: str) -> list[int]:
    """Wrap `adapter.<name>` with a call counter that still delegates to the
    real bound method underneath, and return the counter as a one-element
    mutable cell (the wrapper closes over it, so the caller reads `cell[0]`
    after the fact)."""
    original = getattr(adapter, name)
    calls = [0]

    def wrapper(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, name, wrapper)
    return calls


def _entity_history_args(uid: str) -> dict:
    from tgms.temporal.algebra import ensure_all_registered, validate_args

    ensure_all_registered()  # populates REGISTRY (entity_history lives in ops_snapshot)
    return validate_args("entity_history", {"uid": uid})


# --- 1. route selection: the postings point-read, not versions_columnar --- #


def test_uid_anchored_entity_history_takes_postings_route(tmp_path, monkeypatch):
    from tgms.tgir.compiled import COMPILED

    adapter = _build_store(tmp_path / "s", n_nodes=5_000)

    postings_calls = _spy(monkeypatch, adapter, "believed_node_versions")
    fallback_calls = _spy(monkeypatch, adapter, "versions_columnar")

    args = _entity_history_args("n0")
    payload = COMPILED["entity_history"](adapter, dict(args))

    assert payload["rows"], "sanity: the anchor uid must resolve to at least one row"
    assert fallback_calls[0] == 0, (
        f"a uid-anchored entity_history plan (NodeScan(uids=['n0'])) took the "
        f"versions_columnar fallback {fallback_calls[0]} time(s) instead of "
        f"the postings point-read. This is P2 (62709b3): the compiled scan "
        f"resolving a single-identity lookup by materializing every version "
        f"ever written, instead of reading through believed_node_versions."
    )
    assert postings_calls[0] >= 1, (
        "a uid-anchored entity_history plan never called "
        "believed_node_versions (the postings point-read) at all — "
        f"payload={payload!r}"
    )


# --- 2. scaling: per-lookup cost stays flat as the store grows ------------ #
#
# Measured on this machine, native engine, this file's own fixture and
# statistic (`_median_call_seconds`, 25 reps, `COMPILED["entity_history"]`
# looking up the store's first-ever uid "n0"), N_SMALL=20,000 / N_BIG=400,000
# node versions (20x):
#
#   post-fix (this checkout, HEAD, includes 62709b3):
#     N_SMALL 480-495 us/call  ->  N_BIG 408-473 us/call   ratio 0.84x-1.10x
#     (3 independent full reps; per-call cost does not grow with the store —
#     it is dominated by fixed per-call overhead, not the store's row count)
#
#   pre-fix (git worktree at 62709b3^, same fixture/statistic, `tgms/_engine`
#   copied verbatim from this checkout since the fix is Python-only — see the
#   P0.7 task notes for the exact recipe):
#     N_SMALL 63,645 us/call  ->  N_BIG 1,534,753 us/call   ratio 24.11x
#     (both routes at that commit take versions_columnar regardless of
#     anchor count, confirmed separately by test 1 failing on the same
#     worktree: 1 fallback call, 0 postings calls)
#
# MAX_RATIO below sits well above the post-fix noise band (which never left
# roughly 0.8x-1.1x across reps) and an order of magnitude below the pre-fix
# ratio (24.11x), which is the point of the fix: a NodeScan(uids=[uid])
# fallback re-reads O(store) rows through versions_columnar regardless of
# anchor count, so its per-call cost grows with the store while the postings
# route's does not — a partial regression has a wide gap to close before it
# slips under this bound.
MAX_RATIO = 3.0

# A second, independent signal: the absolute per-call cost at N_BIG. Post-fix
# measured 408-497 us/call on this machine across sizes and reps; pre-fix
# measured 1,534,753 us/call at the same N_BIG. 5000 us (5 ms) sits an order
# of magnitude above the healthiest post-fix reps and over 300x below the
# measured pre-fix cost at this size.
MAX_ABS_US_PER_CALL = 5000.0

N_SMALL = 20_000
N_BIG = 400_000


def _median_call_seconds(adapter, uid: str, reps: int) -> float:
    """Median wall-clock seconds for one `COMPILED["entity_history"]` call.
    Warms up first: the first call after opening a fresh store may pay
    one-time costs (e.g. building the identity postings index) that a cold
    timing would wrongly attribute to steady-state per-call cost."""
    from tgms.tgir.compiled import COMPILED

    args = _entity_history_args(uid)
    COMPILED["entity_history"](adapter, dict(args))  # warm-up, not timed
    times = []
    for _ in range(reps):
        start = time.perf_counter()
        COMPILED["entity_history"](adapter, dict(args))
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def test_entity_history_point_lookup_cost_stays_flat_as_store_grows(tmp_path):
    small = _build_store(tmp_path / "small", n_nodes=N_SMALL)
    small_stats = small.stats()
    assert small_stats["n_node_versions"] == N_SMALL, (
        f"N_SMALL fixture has {small_stats['n_node_versions']} node versions, "
        f"expected exactly {N_SMALL}"
    )
    small_per_call = _median_call_seconds(small, "n0", reps=25)
    small.close()

    big = _build_store(tmp_path / "big", n_nodes=N_BIG)
    big_stats = big.stats()
    assert big_stats["n_node_versions"] == N_BIG, (
        f"N_BIG fixture has {big_stats['n_node_versions']} node versions, "
        f"expected exactly {N_BIG}"
    )
    big_per_call = _median_call_seconds(big, "n0", reps=25)
    big.close()

    ratio = big_per_call / small_per_call
    assert ratio < MAX_RATIO, (
        f"entity_history's point-lookup cost grew {ratio:.2f}x from a "
        f"{N_SMALL}-node store to a {N_BIG}-node one (limit {MAX_RATIO}x): "
        f"{small_per_call * 1e6:.2f}us/call -> {big_per_call * 1e6:.2f}us/call. "
        f"A uid-anchored entity_history lookup should cost about the same "
        f"regardless of store size (the postings point-read, D-076's "
        f"open-version index) — growth here means the scan fell back to "
        f"versions_columnar, or some other store-size-proportional path, "
        f"which is the P2 regression 62709b3 (_anchored/_nodes_by_uid in "
        f"tgms/tgir/eval/scan.py) fixed."
    )
    assert big_per_call * 1e6 < MAX_ABS_US_PER_CALL, (
        f"entity_history's point lookup on the {N_BIG}-node store cost "
        f"{big_per_call * 1e6:.2f}us/call, over the {MAX_ABS_US_PER_CALL}us "
        f"bound measured against the fixed postings route (408-497us/call "
        f"on this machine)"
    )
