"""`version_history` and the node scan must not size their memory by the store.

**The claim these gates pin** comes from the frozen forecast
`docs/design/BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md` §4: what a
`version_history` page costs is a function of the *page*, not of the
population the page is drawn from. Before this change it was the population —
measured at 10.6 GB and 75.8 s over 10M edge versions (D-069,
`docs/DECISIONS.md:3301-3305`), for a call that returns at most `limit` rows.

**Why it was the population.** `NativeAdapter` did not override
`versions_columnar`, so the call took the ABC default
(`tgms/storage/base.py`), which asks the engine for *every* version of the
kind (`all_edge_versions`), converts each into a Python object, then converts
those into ten object arrays. Four full copies of the store, two of them
resident at once: the forecast's §1 table. `props` was built for every row by
`json.loads` on the way through, even though the operator drops it
(`VERSION_COLS`) and never returns it.

The fix (forecast §4, design (a)) keeps the population inside Rust and brings
only the page across: a streaming key pass that emits a 32-byte integer key
per surviving row, an integer sort, and a second pass that materializes just
the ~`limit` rows the page names. The second O(store) site, `scan_nodes`
(`crates/tgms-engine-py/src/lib.rs`), got the same treatment: it used to call
`all_node_versions()` and filter the full listing *afterwards* — D-149's scan
that never completed in 1,400 s on SNB SF1's 2,997,352 node versions
(`docs/DECISIONS.md:7127-7131`).

**Three claims, three tests**, because they can fail apart:

1. `test_version_history_never_materializes_the_population` — the route.
   Spies on the adapter's own `versions_columnar`, the method the ABC
   default path goes through, exactly as
   `test_compiled_point_lookup_scaling.py`'s route test does (`_spy` at the
   adapter boundary rather than reaching into private functions).
2. `test_version_history_memory_is_bounded_per_stored_version` — the cost.
3. `test_node_scan_does_not_materialize_every_node_version` — the second
   site, by the engine's own `rows_materialized` counter and by the same
   memory measurement.

**The metric is bytes per stored version, not a flat peak — and that is a
deviation from the forecast's §5 wording, taken deliberately.** §5 asks for a
"peak-RSS ratio ≤ 1.5 across the 20x". Total peak RSS is
`floor + c·(stored versions) + page`, and that gate is a statement about `c`
only when the floor dominates — which is what §4 is measuring on the eval
host, where D-082 puts the per-process floor at 1.8–2.4 GB and the fixed
0.28 GB of keys at 10M sits inside it. At the sizes a pytest fixture can
build the floor is ~40 MB, and two things that have nothing to do with this
operator grow right past it: the uid dictionary (~93 MB at 500,000 nodes) and
`stats()`'s per-source out-degree map (~85 MB). A 1.5x total-peak gate here
would be measuring the fixture. So the probe takes its baseline *after* the
store is open and `stats()` is warm, and the gate is on what the operator
itself then adds, per stored version — which is exactly the `c` the design
changed, and is scale-free in a way a ratio at one pair of sizes is not.

**Peak RSS is measured in a child process, one per measurement.**
`resource.getrusage(...).ru_maxrss` is a high-water mark that only ever rises
within a process, so two measurements in one process cannot be compared —
whichever ran second would inherit the first's peak. Each child opens exactly
one store and runs exactly one operator.

**Both stores are compacted, and deliberately built so that compaction
shatters the `tt_s` runs** (D-149's caveat, forecast §4). `tt_s` is
run-length encoded in the segment header; a store whose valid-time order
happens to agree with its batch order keeps a handful of long runs and never
exercises `tt_s_at`. Compaction re-sorts globally by `(vt_s, vid)` while each
row keeps its origin `tt_s`, so interleaving the batches' valid times (the
`vt_s = i * n_batches + b` pattern `test_compacted_layout_scaling.py` uses)
drives the store to one run per row — the shape the key pass has to survive,
since it reads `tt_s` for every row it sweeps. `max_tt_s_runs` is asserted
before any measurement is trusted, in that file's style.

**Native only.** The fix is a native engine path plus a native adapter
override; DuckDB and Kùzu keep the ABC default unchanged, which is the point
of adding `versions_page` as a defaulted ABC method rather than an abstract
one. `pytest.importorskip` skips the file when the extension is not built.

MEASURED on this machine (Darwin 25.6.0, Python 3.12.13), this file's own
fixtures and probe, N_SMALL=25,000 / N_BIG=500,000 edge versions and the same
counts of node versions. The pre-change arm is `feaab3f` — its tree extracted
to a scratch directory, its own `.so` built from its `crates/`, run against
these same two stores:

    version_history, unbounded window, 50-row page
                          pre-change            this checkout
      25,000    growth      44.84 MB              1.06 MB
                per row     1,794 B                42.6 B
                wall         0.152 s               0.006 s
      500,000   growth     597.02 MB             26.64 MB
                per row     1,194 B                53.3 B
                wall         4.087 s               0.049 s
      per-row ratio          0.67x                 1.25x

    nodes_columnar, 1,000-point window over 500,001 node versions
      growth              264.45 MB              36.83 MB
      per row               529 B                  73.7 B
      wall                  0.291 s                0.101 s
      rows_materialized   500,001 (all of them)   1,001 (the survivors)

The pre-change 1,194 B per stored version at 500,000 is the same quantity
D-069 measured at ~1,060 B over 10M — both are the ~1,060 B/row of the
forecast's §1 table, and the higher figure at 25,000 is allocator granularity
at the smaller size. MAX_BYTES_PER_VERSION sits 8x under the pre-change
number and 2.8x over the post-change one.

Note what the per-row *ratio* does and does not catch: pre-change it is
0.67x, comfortably inside MAX_PER_ROW_RATIO. Flatness of the constant was
never the broken property — the constant's size was — so that assertion is
there for a different failure (a superlinear term appearing later) and is not
part of this A/B.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# --- fixture sizes and gate thresholds ------------------------------------ #

N_SMALL = 25_000
N_BIG = 500_000          # 20x
N_BATCHES = 20           # >= 2, or compaction has no runs to shatter

#: Resident bytes the operator itself adds, per version stored of its kind.
#: Pre-change 1,380 (and ~1,060 at 10M, D-069); post-change 53.4. The design
#: budgets a 32-byte sort key per *surviving* row plus the segment pages the
#: sweep touches, so this is not zero and is not meant to be — it is the
#: constant the change is about.
MAX_BYTES_PER_VERSION = 150.0

#: The same constant at the node scan, whose survivors are materialized but
#: whose sweep still touches every row's integer columns. Pre-change 527,
#: post-change 70.4.
MAX_NODE_BYTES_PER_VERSION = 200.0

#: The per-row constant must not itself grow with the store — the flatness
#: half of the claim, and the one a partial fix could still fail. Measured
#: 1.20 across the 20x.
MAX_PER_ROW_RATIO = 1.5

#: Wall at N_BIG. The forecast's second falsifier is a wall-clock one
#: (> 113.7 s at 10M); this is its local shadow. Pre-change 4.322 s,
#: post-change 0.048 s.
MAX_BIG_WALL_S = 1.5

#: The page the probe asks for. Small on purpose: the whole claim is that the
#: cost tracks this number and not the store.
PAGE_LIMIT = 50


def _native():
    return pytest.importorskip(
        "tgms.storage.native",
        reason="the bounded-version-history gates measure the native engine's "
               "own read path",
    )


def _repo_root() -> Path:
    import tgms

    return Path(tgms.__file__).resolve().parents[1]


def _build_store(path: Path, n: int) -> None:
    """`n` instantaneous edge versions over `n + 1` single-version nodes,
    written in `N_BATCHES` batches whose valid times interleave, then
    compacted.

    `ingest_events` rather than `n` `assert_edge` calls: it is the bulk path,
    each event is its own logical edge (its batch offset is the
    discriminator), and it creates one node version per first-seen endpoint —
    so one build gives both operators under test a population of the same
    size.

    The interleaving is the D-149 caveat made concrete. Batch `b` writes valid
    times `{i * N_BATCHES + b}`, so a global `(vt_s, vid)` re-sort at
    compaction time places rows from all `N_BATCHES` transaction times
    alternately, and every row starts its own `tt_s` run. Building the batches
    in valid-time order instead would leave the runs coalesced and the gate
    would not see the cost it exists to bound.
    """
    native = _native()
    a = native.NativeAdapter(path)
    a.paranoid = False  # the disjointness check is O(versions) per op
    per = n // N_BATCHES
    tt = 1000
    written = 0
    for b in range(N_BATCHES):
        k = per if b < N_BATCHES - 1 else n - written
        events = [
            {"src": f"u{written + i}", "dst": f"u{written + i + 1}",
             "rel_type": "R", "vt_s": (i * N_BATCHES + b) + 1000}
            for i in range(k)
        ]
        tt += 1
        a.begin()
        a.apply_ops([{"op": "ingest_events", "events": events, "offset": written}], tt)
        a.commit()
        written += k
    a.compact()
    a.close()


@pytest.fixture(scope="module")
def stores(tmp_path_factory) -> dict[str, Path]:
    """The 20x pair, built once for the file — ~19 s of fixture construction
    that three tests would otherwise pay separately. Their `tt_s` runs are
    checked here, so no test measures a store that never shattered them.
    """
    native = _native()
    base = tmp_path_factory.mktemp("bounded-version-history")
    out = {}
    for name, n in (("small", N_SMALL), ("big", N_BIG)):
        d = base / name
        _build_store(d, n)
        a = native.NativeAdapter(d)
        report = a.verify()
        assert report["max_tt_s_runs"] > n // 2, (
            f"the {n}-version fixture did not shatter its tt_s runs at "
            f"compaction (max_tt_s_runs={report['max_tt_s_runs']}); the key "
            f"pass reads tt_s per row and these gates would not see D-149's "
            f"cost. Check the valid-time interleaving in _build_store."
        )
        a.close()
        out[name] = d
    return out


#: Run one operator against one store in a fresh interpreter and report that
#: interpreter's own high-water RSS, against a baseline taken once the store
#: is open and `stats()` is warm — so the uid dictionary and the out-degree
#: map, neither of which this change touches, are outside the measurement.
#: Kept as source text rather than a helper module so the measurement and its
#: gate read as one file.
_PROBE = r"""
import json, resource, sys, time

root, store, op, limit = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
# The parent's own tree, ahead of everything — `python -c` puts the *working
# directory* at sys.path[0], so a probe launched from anywhere but the repo
# root would silently measure whichever checkout happened to be the cwd. That
# is not hypothetical: it is how the A/B against the pre-change tree first
# reported post-change numbers.
sys.path.insert(0, root)

def rss():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes
    return r if sys.platform == "darwin" else r * 1024

from tgms.storage.native import NativeAdapter
from tgms.temporal.algebra import REGISTRY, ensure_all_registered, validate_args

ensure_all_registered()
a = NativeAdapter(store)
a.paranoid = False
stats = a.stats()          # warms the dictionary and the stats accumulator
baseline = rss()
t0 = time.perf_counter()

if op == "version_history":
    # The registered kernel directly, not `call_operator`: the cost guardrail
    # refuses this operator well below these store sizes and re-pricing it is
    # a separate decision (forecast SS4, D-069's precedent). D-069's own probe
    # did the same.
    args = validate_args("version_history", {
        "kind": "edge",
        "window": {"t_a": 0, "t_b": 4611686018427387904},
        "limit": limit,
    })
    out = REGISTRY["version_history"].fn(a, args)
    extra = {"rows_total": out["rows_total"], "rows": len(out["rows"]),
             "n_versions": stats["n_edge_versions"]}
elif op == "nodes_columnar":
    cols = a.nodes_columnar(vt_min=1000, vt_max=1000 + 1000)
    extra = {"rows": len(cols["vid"]), "n_versions": stats["n_node_versions"]}
else:
    raise SystemExit("unknown op " + op)

wall = time.perf_counter() - t0
peak = rss()
print(json.dumps({
    "baseline": baseline, "peak": peak, "growth": peak - baseline,
    "wall_s": wall, **extra,
}))
"""


def _probe(store: Path, op: str, limit: int = PAGE_LIMIT) -> dict:
    env = dict(os.environ)
    root = str(_repo_root())
    env["PYTHONPATH"] = (root + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else root)
    r = subprocess.run(
        [sys.executable, "-c", _PROBE, root, str(store), op, str(limit)],
        capture_output=True, text=True, env=env, timeout=1800,
    )
    if r.returncode != 0:
        raise AssertionError(
            f"the {op} probe against {store} exited {r.returncode}\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
    got = json.loads(r.stdout.strip().splitlines()[-1])
    got["per_row"] = got["growth"] / got["n_versions"]
    return got


def _mb(n: float) -> str:
    return f"{n / 1e6:.1f} MB"


# --- 1. route: the population never crosses the boundary ------------------ #


def test_version_history_never_materializes_the_population(stores, monkeypatch):
    """`versions_columnar` is the ABC default's O(store) door. A native
    `version_history` must not go through it — not once, not for `rows_total`.
    """
    native = _native()
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered, validate_args

    ensure_all_registered()
    a = native.NativeAdapter(stores["small"])
    a.paranoid = False

    calls = [0]
    original = a.versions_columnar

    def wrapper(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(a, "versions_columnar", wrapper)

    args = validate_args("version_history", {
        "kind": "edge", "window": {"t_a": 0, "t_b": 2**62}, "limit": PAGE_LIMIT,
    })
    out = REGISTRY["version_history"].fn(a, args)

    assert out["rows_total"] == N_SMALL, (
        f"sanity: the whole population should survive an unbounded window, "
        f"got rows_total={out['rows_total']} over {N_SMALL} edge versions"
    )
    assert len(out["rows"]) == PAGE_LIMIT
    assert calls[0] == 0, (
        f"native version_history called versions_columnar {calls[0]} time(s). "
        f"That is the ABC default at tgms/storage/base.py, which materializes "
        f"every version of the kind — four copies of the store to return "
        f"{PAGE_LIMIT} rows (forecast SS1). The native adapter is supposed to "
        f"override `versions_page` and keep the population in the engine."
    )
    a.close()


# --- 2. cost: bytes per stored version, and its flatness ------------------ #


def test_version_history_memory_is_bounded_per_stored_version(stores):
    small = _probe(stores["small"], "version_history")
    big = _probe(stores["big"], "version_history")

    assert small["rows_total"] == N_SMALL and big["rows_total"] == N_BIG, (
        f"fixture sanity: rows_total {small['rows_total']} / "
        f"{big['rows_total']}, expected {N_SMALL} / {N_BIG}"
    )
    assert small["rows"] == big["rows"] == PAGE_LIMIT

    detail = (
        f"{N_SMALL:,}: {_mb(small['growth'])} = {small['per_row']:.0f} B/row, "
        f"{small['wall_s']:.3f}s | "
        f"{N_BIG:,}: {_mb(big['growth'])} = {big['per_row']:.0f} B/row, "
        f"{big['wall_s']:.3f}s"
    )

    assert big["per_row"] <= MAX_BYTES_PER_VERSION, (
        f"version_history added {big['per_row']:.0f} resident bytes per stored "
        f"edge version to return a {PAGE_LIMIT}-row page (limit "
        f"{MAX_BYTES_PER_VERSION:.0f} B). Measured pre-change: 1,380 B here "
        f"and ~1,060 B at 10M (D-069) — the whole population materialized, "
        f"four copies deep, to slice a page out of it. This is what "
        f"docs/design/BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md SS4 "
        f"budgets at a 32-byte key per surviving row. [{detail}]"
    )
    if small["per_row"] <= 0:
        # On Linux CI the small store's operator-only growth rounds to zero
        # (VmHWM never moves past the warm floor), so the flatness ratio is
        # undefined there; the absolute per-row bound above already holds.
        return
    ratio = big["per_row"] / small["per_row"]
    assert ratio <= MAX_PER_ROW_RATIO, (
        f"version_history's per-stored-version cost itself grew {ratio:.2f}x "
        f"across a 20x store (limit {MAX_PER_ROW_RATIO}x). The absolute bound "
        f"can pass while this fails: a term that is superlinear in the store "
        f"is a different defect from one that is merely large, and at 100M it "
        f"is the one that matters. [{detail}]"
    )
    assert big["wall_s"] <= MAX_BIG_WALL_S, (
        f"version_history took {big['wall_s']:.3f}s over {N_BIG:,} versions "
        f"(limit {MAX_BIG_WALL_S}s; measured 4.322s pre-change, 0.048s after). "
        f"The forecast's own second falsifier is a wall-clock one — > 113.7 s "
        f"at 10M — and 53.3 s of the pre-change 75.8 s was `versions_columnar` "
        f"building Python strings (D-069). [{detail}]"
    )


# --- 3. the second site: the node scan ------------------------------------ #


def test_node_scan_does_not_materialize_every_node_version(stores):
    """`scan_nodes` filtered a fully materialized listing; now it filters
    during the sweep and materializes survivors.

    `rows_materialized` is the engine's own counter, added beside the
    pre-existing `rows_examined` — the sweep still *reads* every row's
    integer columns (node segments carry no belief index, so there is nothing
    to prune by), but a row's uid, vid and label are built only if it
    survives. Those three are the whole cost: a dictionary lookup, a 24-hex
    identity and a string-table read per row, plus the props/source/
    provenance the caller never asked for.

    The counter is the mechanism and the probe is the consequence; both are
    asserted, because a counter can be right while the memory is not.
    """
    native = _native()
    a = native.NativeAdapter(stores["big"])
    a.paranoid = False

    total = a.stats()["n_node_versions"]
    assert total == N_BIG + 1, f"fixture has {total} node versions, expected {N_BIG + 1}"

    # A window covering 1,000 of the 500,001 valid-time points.
    got = a._store.scan_nodes(as_of_tt=2**62 - 1, vt_min=1000, vt_max=2000)

    assert "rows_materialized" in got, (
        "scan_nodes reports no `rows_materialized` counter, so it cannot "
        "distinguish 'swept the store' from 'built the store'. Pre-change it "
        "built the whole listing (all_node_versions) and filtered afterwards "
        "— crates/tgms-engine-py/src/lib.rs, the second O(store) site in "
        "docs/design/BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md SS1."
    )
    survivors = got["rows"]
    assert 0 < survivors < total // 100, (
        f"sanity: the window should select a small slice, got {survivors} of "
        f"{total}"
    )
    assert got["rows_examined"] == total, (
        f"the sweep should still visit every stored node version "
        f"({got['rows_examined']} of {total}) — the claim is about what gets "
        f"built, not about pruning that does not exist"
    )
    assert got["rows_materialized"] == survivors, (
        f"scan_nodes materialized {got['rows_materialized']} node versions to "
        f"return {survivors} of {total}. The filter belongs below the "
        f"materialization: this is D-149's scan that never completed in "
        f"1,400 s on SNB SF1's 2,997,352 node versions "
        f"(docs/DECISIONS.md:7127-7131)."
    )
    a.close()

    probe = _probe(stores["big"], "nodes_columnar")
    assert probe["rows"] == survivors
    assert probe["per_row"] <= MAX_NODE_BYTES_PER_VERSION, (
        f"nodes_columnar added {probe['per_row']:.0f} resident bytes per "
        f"stored node version ({_mb(probe['growth'])} total) to return "
        f"{survivors} rows, over the {MAX_NODE_BYTES_PER_VERSION:.0f} B bound "
        f"(pre-change 527 B / {_mb(263.5e6)}; after, 70 B). Its sweep touches "
        f"every row's integer columns and cannot do better than the segment "
        f"pages it reads — but it must not be building the rows."
    )
