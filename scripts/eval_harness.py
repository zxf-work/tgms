"""Phase 0 evaluation harness: query registry, result canonicalizer, run manifest.

The evaluation plan compares systems that format results differently, express
different subsets of the semantics, and cannot be trusted to agree on field
order. So a comparison needs three things before it needs a second system:

* a **query registry** — one stable identifier per logical question, so a
  result can be attributed to a query rather than to a code path;
* a **result canonicalizer** — a hash over the logical answer, ignoring
  presentation, so "same answer" is decidable rather than eyeballed;
* a **run manifest** — enough environment to make a number reproducible, and
  to know when two numbers are not comparable.

Today it runs the two TGMS backends. That is deliberately the smallest
interesting matrix: they have identical semantics, so any hash mismatch is a
bug rather than an expressiveness gap, which is exactly the property Phase 0's
exit criterion depends on. Adding PostgreSQL or Neo4j means adding a runner
that answers the same registry, not changing anything here.

    uv run python scripts/eval_harness.py --scale 200000
    uv run python scripts/eval_harness.py --store benchmarks/frozen-v1/... --json out.json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tgms
from tgms.core.model import EntityRef, canonical_json, sha256_hex
from tgms.temporal.algebra import call_operator, ensure_all_registered

ROOT = Path(__file__).resolve().parents[1]

#: Reported numbers come from one host, so that two rows of a table are
#: always comparable. A laptop run is for development; it differs by 5x in
#: cores and 6x in RAM, and macOS pins effective_io_concurrency to 0 for want
#: of posix_fadvise, so the PostgreSQL baseline cannot prefetch there at all.
MEASUREMENT_HOST = "xzgpu"
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Fields that describe the *call* rather than the answer. They legitimately
#: differ between systems and runs, so they are excluded from the hash: the
#: dataset extent moves as a store grows, and the digest is itself derived.
VOLATILE = ("op", "args_echo", "dataset_extent", "result_digest", "cursor")


@dataclass(frozen=True)
class Query:
    """One logical question, identified independently of how it is answered."""

    id: str
    op: str
    args: dict[str, Any]
    note: str = ""
    #: Semantics a system must support to answer this at all. Recorded so a
    #: system that cannot express one is reported as such rather than as slow.
    requires: tuple[str, ...] = ()


#: Fixed, not scale-relative. Sized to stay inside the cost guardrail at every
#: scale in the sweep while still covering whole communities: a filter of
#: n_nodes/5 answered at 20k events and raised E_COST at 200k, which makes the
#: motif row measure the guardrail rather than the operator.
MOTIF_FILTER = 200


def registry(scale: int, tt_epoch1: int) -> list[Query]:
    """The Phase 0 query set: one per operator family, plus belief-time probes.

    Parameters are derived from the scale so the same registry is meaningful
    at 1e5 and 1e7 without editing. `tt_epoch1` is the belief boundary from
    `build_dataset` — an `as_of_tt` that predates the corrections but not the
    data. It has to come from the store: transaction times are epoch
    microseconds, so any literal small enough to write by hand predates
    everything and turns the belief probe into a query over an empty store.
    """
    span = scale
    mid = span // 2
    return [
        Query("hist.single", "entity_history", {"uid": "n1"},
              "point lookup by identity"),
        Query("hist.asof", "entity_history", {"uid": "n1", "as_of_tt": tt_epoch1},
              "same lookup under the pre-correction belief",
              requires=("bitemporal",)),
        Query("snap.hop2", "snapshot_subgraph",
              {"seeds": ["n1"], "hops": 2, "t_valid": mid},
              "2-hop neighbourhood at an instant"),
        # t2 - t1 is half an edge lifetime, so the two instants can see
        # different versions of the same edge; a wider gap makes the edge sets
        # disjoint and props_changed empty whatever the data contains.
        Query("diff.global", "diff_snapshots", {"t1": mid, "t2": mid + span // 40},
              "global difference between two instants"),
        Query("reach.window", "temporal_reachability",
              {"src": "n1", "window": {"t_a": 0, "t_b": span // 10}},
              "time-respecting reachability"),
        Query("paths.k", "temporal_paths",
              {"src": "n1", "dst": "n2", "window": {"t_a": 0, "t_b": span // 4},
               "k": 3, "max_hops": 3},
              "k shortest time-respecting paths"),
        Query("series.count", "graph_metric_timeseries",
              {"metric": "edge_event_count", "window": {"t_a": 0, "t_b": span},
               "stride": max(1, span // 100)},
              "bucketed event rate"),
        Query("burst.zscore", "burst_detection",
              {"target": {"kind": "edge_event_rate"}, "window": {"t_a": 0, "t_b": span},
               "stride": max(1, span // 100), "method": "zscore", "params": {"z": 3.0}},
              "burst flags over the same buckets"),
        Query("nbr.evolution", "neighborhood_evolution",
              {"uid": "n1", "t1": mid, "t2": mid + span // 10},
              "neighbours gained and lost"),
        Query("coactive.narrow", "co_active",
              {"a_spec": {"src": "n1"}, "b_spec": {"src": "n2"},
               "allen_relation": {"relation": "overlaps"}},
              "interval join between two edge sets"),
        Query("resolve.substr", "resolve_entities", {"query": "n1"},
              "entity resolution by substring"),
        Query("motif.filtered", "count_temporal_motifs",
              {"motif": "M_triangle_cyclic", "delta": span // 50,
               "window": {"t_a": 0, "t_b": span},
               "node_filter": [f"n{i}" for i in range(MOTIF_FILTER)]},
              "delta-motif count, node-filtered to stay inside the guardrail"),
    ]


def canonical_hash(payload: dict[str, Any]) -> str:
    """Hash the logical answer, ignoring presentation.

    Two systems agree when this matches. Volatile call metadata is stripped
    first; everything else — including row order, which the operator contract
    fixes — is part of the answer.
    """
    stable = {k: v for k, v in payload.items() if k not in VOLATILE}
    return sha256_hex(canonical_json(stable))[:32]


def _answer_size(payload: dict[str, Any]) -> int:
    """How big the answer is, for operators that do not all use `rows`.

    `diff_snapshots` and `neighborhood_evolution` carry several named lists
    and no `rows` key at all, so reading `len(payload["rows"])` reported them
    as empty — which is how a 999-edge difference showed up in the results
    table as a zero.
    """
    if "rows_total" in payload:
        return int(payload["rows_total"])
    totals = [v for k, v in payload.items()
              if k.endswith("_total") and isinstance(v, int)]
    if totals:
        return sum(totals)
    if "count" in payload:
        return int(payload["count"])
    return len(payload.get("rows", []))


@dataclass
class Result:
    query: str
    ok: bool
    hash: str | None = None
    p50_ms: float | None = None
    rows: int | None = None
    error: str | None = None


def run_system(name: str, store_path: Path, queries: list[Query],
               repeats: int) -> list[Result]:
    """Answer every registry query on one system."""
    ensure_all_registered()
    store = tgms.open(store_path, backend=name)
    adapter = store.adapter
    out: list[Result] = []
    for q in queries:
        try:
            timings = []
            payload = None
            for _ in range(repeats):
                t0 = time.perf_counter()
                payload = call_operator(adapter, q.op, dict(q.args))
                timings.append((time.perf_counter() - t0) * 1e3)
            assert payload is not None
            rows = _answer_size(payload)
            out.append(Result(q.id, True, canonical_hash(payload),
                              round(min(timings), 3), int(rows)))
        except Exception as e:  # a system that cannot answer is data, not a crash
            out.append(Result(q.id, False, error=f"{type(e).__name__}: {e}"[:160]))
    store.close()
    return out


def run_postgres(store_path: Path, queries: list[Query],
                 repeats: int) -> list[Result]:
    """Answer the registry with SQL instead of with TGMS operators.

    Only the queries in `pg_queries.QUERIES` are attempted. A missing entry is
    reported as *not yet written*, which is deliberately not the same as
    `eval_semantics`'s `unsupported` verdict: that one asserts a system cannot
    express the query, and nothing here has established it. Conflating the two
    would let unfinished work read as a limitation of PostgreSQL.
    """
    import pg_baseline
    import pg_queries

    adapter = tgms.open(store_path, backend="native").adapter
    pg_baseline.ensure_database()
    conn = pg_baseline.connect()
    pg_baseline.apply_tuning(conn)
    conn.execute(pg_baseline.SCHEMA)
    pg_baseline.load(conn, adapter)
    conn.execute(pg_baseline.INDEXES)
    conn.execute("ANALYZE edge_versions")
    conn.execute("ANALYZE node_versions")

    out: list[Result] = []
    for q in queries:
        fn = pg_queries.QUERIES.get(q.id)
        if fn is None:
            out.append(Result(q.id, False, error="no SQL written yet (not a verdict)"))
            continue
        try:
            timings = []
            payload = None
            for _ in range(repeats):
                t0 = time.perf_counter()
                payload = fn(conn, **q.args)
                timings.append((time.perf_counter() - t0) * 1e3)
            assert payload is not None
            out.append(Result(q.id, True, canonical_hash(payload),
                              round(min(timings), 3), _answer_size(payload)))
        except Exception as e:
            out.append(Result(q.id, False, error=f"{type(e).__name__}: {e}"[:160]))
    conn.close()
    return out


def manifest(scale: int, systems: list[str], repeats: int) -> dict[str, Any]:
    """Everything needed to say whether two runs are comparable."""
    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                  check=True).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "commit": sh("git", "rev-parse", "--short", "HEAD"),
        "dirty": bool(sh("git", "status", "--porcelain")),
        "rustc": sh("rustc", "--version"),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu_count": __import__("os").cpu_count(),
        "scale_events": scale,
        "systems": systems,
        "repeats": repeats,
        "cache_state": "warm",  # queries repeat in-process; see plan §15
        "measurement_host": MEASUREMENT_HOST,
        "on_measurement_host": platform.node().split(".")[0] == MEASUREMENT_HOST,
    }


#: The graph grows with `scale` at *constant average degree* — |V| and edge
#: lifetime both scale, so an instant snapshot has ~10 neighbours per node at
#: every scale. A fixed |V| would make 2-hop neighbourhoods swallow the whole
#: graph as scale rose; a fixed lifetime made them empty. Neither measures the
#: operator.
def n_nodes(scale: int) -> int:
    return max(200, scale // 100)


def edge_life(scale: int) -> int:
    return max(40, scale // 20)


_M64 = (1 << 64) - 1


def _mix(x: int) -> int:
    """splitmix64 finalizer — a deterministic stand-in for a PRNG.

    Deterministic matters: the corrections in `build_dataset` have to name
    edges by the identity ingestion gave them, which means every endpoint must
    be recomputable from the event index alone.
    """
    x = (x * 0x9E3779B97F4A7C15) & _M64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & _M64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def _vt_s(i: int, scale: int) -> int:
    """Event `i` starts at tick `i`, except inside one deliberate burst.

    Without this the event rate is exactly flat, so `burst_detection` returns
    an empty list at every scale and two systems "agree" on having found
    nothing. A band of 5% of the events is compressed tenfold into one stretch
    of the timeline, which puts a bucket roughly six sigma above the mean.
    """
    b0 = scale // 2
    b1 = b0 + max(1, scale // 20)
    if b0 <= i < b1:
        return b0 + (i - b0) // 10
    return i


#: Nodes per community, and the share of edges that stay inside one.
#: A uniform random graph has almost no triangles — that is a property of the
#: model, not of the data it is standing in for. With a flat endpoint choice,
#: `count_temporal_motifs` returned zero cyclic triangles at every node-filter
#: size that stayed inside the cost guardrail, so the motif operator was being
#: compared on an answer of nothing. Real interaction graphs cluster; blocking
#: most edges inside a community reproduces enough of that for the operator to
#: have something to count.
COMMUNITY = 50
INTRA_PCT = 70


def _event(i: int, scale: int) -> dict[str, Any]:
    """Event `i`: a random edge with community structure, reproducible from `i`.

    Endpoints used to be `src = i mod |V|`, `dst = 7i + 3 mod |V|`. That is not
    a random graph — `dst` is a function of `src`, so the graph is one
    deterministic cycle in which every edge out of n1 goes to n10 and nowhere
    else. `neighborhood_evolution` reported zero neighbours gained and zero
    lost at every scale, and 2-hop neighbourhoods held four nodes, because
    there was nothing else to find. Mixing the index independently for each
    endpoint gives a random multigraph with Poisson degree instead.

    The degree distribution is deliberately uniform rather than power-law: a
    scale sweep should vary scale alone, and the real skew is measured on the
    frozen CollegeMsg replay, which has a genuine one.
    """
    n, t = n_nodes(scale), _vt_s(i, scale)
    src = _mix(2 * i) % n
    r = _mix(2 * i + 1)
    if r % 100 < INTRA_PCT:
        # Communities are contiguous id ranges so that a node_filter of the
        # first k nodes covers whole communities rather than slicing every one.
        base = (src // COMMUNITY) * COMMUNITY
        dst = base + (r >> 8) % min(COMMUNITY, n - base)
    else:
        dst = (r >> 8) % n
    return {"src": f"n{src}", "dst": f"n{dst}",
            "rel_type": "R" if i % 3 else "S",
            "vt_s": t, "vt_e": t + edge_life(scale)}


def _edge_ref(i: int, scale: int) -> EntityRef:
    """The identity `ingest_events` gave event `i`.

    Ingestion discriminates by batch offset (`storage/base.py:360`), so the
    endpoints repeat every 6000 events but each occurrence is a *distinct*
    logical edge, not a new version of one. A ref built without the disc names
    an edge that was never written, and correcting it raises NotFound.
    """
    e = _event(i, scale)
    return EntityRef(kind="edge", src=e["src"], dst=e["dst"],
                     rel_type=e["rel_type"], disc=f"#{i}")


@dataclass(frozen=True)
class Dataset:
    """A reference store plus the belief boundary needed to query it."""

    log: Path
    scale: int
    #: A transaction time at which epoch-1 data is believed and the epoch-2
    #: corrections are not yet. Without it there is no way to ask a belief
    #: question, because tt values are wall-clock and not known in advance.
    tt_epoch1: int


def build_dataset(scale: int) -> Dataset:
    """Write the reference event log once. Every system replays *this*.

    Building a store per system would look equivalent and is not: transaction
    times come from a clock at write time, so independently built stores of
    the same data differ in tt, and therefore in every version id derived from
    it. The first run of this harness reported two systems disagreeing for
    exactly that reason (D-023).

    Two properties of the generated data are deliberate, and both were added
    after measuring that their absence made the comparison vacuous:

    **The graph scales at constant average degree.** Lifetime used to be a
    constant 40 ticks while one edge starts per tick, so exactly ~40 edges were
    ever valid at any instant, on 2000 nodes — every instant operator was
    answering over an essentially empty graph no matter the scale.
    `snapshot_subgraph` at 2 hops returned its seed and nothing else at 20k
    events and at 200k alike. Two systems agreeing on that is not evidence of
    anything. Scaling |V| and lifetime together keeps degree near 10
    throughout, so the same query stays meaningful and stays bounded.

    **There is a second belief epoch.** The generator previously produced no
    corrections at all, so every row had an open `tt_e` and no query could
    distinguish one belief state from another — while `eval_semantics` §1 sets
    exactly that as the bar for calling a system `equivalent`. The corrections
    below close `tt_e` on real rows, so `hist.asof` finally tests the clock.
    """
    path = Path(tempfile.mkdtemp()) / "reference"
    store = tgms.open(path, backend="native")
    store.ingest_events([_event(i, scale) for i in range(scale)])
    tt_epoch1 = store.assert_node("n1", "Node", {"name": "alpha"},
                                  vt_s=0, vt_e=scale)

    # Epoch 2: corrections and a retraction. These supersede believed rows,
    # so their tt_e closes and the store stops being uniformly "current".
    step = max(1, scale // 200)
    for i in range(0, scale, step):
        e = _event(i, scale)
        store.correct(_edge_ref(i, scale), {"weight": 2},
                      vt_s=e["vt_s"], vt_e=e["vt_e"])
    store.correct(EntityRef(kind="node", uid="n1"), {"name": "alpha-corrected"},
                  vt_s=0, vt_e=scale)

    # Partial corrections: rewrite only the tail half of an edge's interval,
    # which splits it into two versions carrying different props. Whole-
    # interval corrections cannot produce that, and `diff_snapshots` only
    # reports a props change when the two instants see *different* vids — so
    # without a split its props_changed list is empty by construction and that
    # branch of any reimplementation goes untested.
    life, mid = edge_life(scale), scale // 2
    for i in range(mid - life // 2, mid, max(1, life // 20)):
        e = _event(i, scale)
        store.correct(_edge_ref(i, scale), {"weight": 3},
                      vt_s=e["vt_s"] + life // 2, vt_e=e["vt_e"])
    for i in range(0, scale, step * 4):
        store.retract(_edge_ref(i, scale),
                      _event(i, scale)["vt_s"] + edge_life(scale) // 2)
    store.close()
    return Dataset(path / "eventlog.jsonl", scale, tt_epoch1)


def load_store(path: Path, backend: str, log: Path) -> None:
    """Materialize the reference log into one system, preserving its tt."""
    from tgms.storage.eventlog import replay

    store = tgms.open(path, backend=backend)
    replay(log, store.adapter)
    store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=200_000, help="events to generate")
    ap.add_argument("--systems", default="native,duckdb")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--json", type=Path, help="write the full run record here")
    args = ap.parse_args()

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    data = build_dataset(args.scale)
    queries = registry(args.scale, data.tt_epoch1)
    meta = manifest(args.scale, systems, args.repeats)

    print(f"phase-0 harness — {len(queries)} queries x {len(systems)} systems "
          f"@ {args.scale:,} events")
    print(f"  commit {meta['commit']}{' (dirty)' if meta['dirty'] else ''} | "
          f"{meta['platform']} | {meta['cpu_count']} cores")
    if not meta["on_measurement_host"]:
        print(f"  WARNING: not {MEASUREMENT_HOST} — development run, timings "
              f"are not comparable with reported numbers")
    print()

    log = data.log
    results: dict[str, list[Result]] = {}
    for name in systems:
        path = Path(tempfile.mkdtemp()) / "store"
        t0 = time.perf_counter()
        # PostgreSQL is a baseline, not a backend: it cannot replay the log,
        # so it is loaded from a native store's canonical rows instead.
        load_store(path, "native" if name == "postgres" else name, log)
        load = time.perf_counter() - t0
        results[name] = (run_postgres(path, queries, args.repeats)
                         if name == "postgres"
                         else run_system(name, path, queries, args.repeats))
        print(f"  {name}: loaded in {load:.1f}s")

    base, *others = systems
    by_id = {s: {r.query: r for r in rs} for s, rs in results.items()}

    hdr = f"\n  {'query':<18}{'rows':>8}" + "".join(f"{s:>12}" for s in systems) + "  agree"
    print(hdr)
    print("  " + "-" * (len(hdr) - 3))
    mismatches, unsupported = [], []
    for q in queries:
        cells, hashes = [], []
        rows = None
        for s in systems:
            r = by_id[s][q.id]
            if r.ok:
                cells.append(f"{r.p50_ms:>11.1f}")
                hashes.append(r.hash)
                rows = r.rows if rows is None else rows
            else:
                cells.append(f"{'n/a':>11}")
                hashes.append(None)
                unsupported.append((s, q.id, by_id[s][q.id].error))
        ok = len({h for h in hashes if h}) <= 1 and all(hashes)
        if not ok and all(hashes):
            mismatches.append(q.id)
        mark = "yes" if ok else ("MISMATCH" if all(hashes) else "partial")
        print(f"  {q.id:<18}{rows if rows is not None else '-':>8}"
              + "".join(cells) + f"  {mark}")

    print()
    for s, qid, err in unsupported:
        print(f"  no answer: {s}/{qid} — {err}")
    if mismatches:
        print(f"\n  RESULT HASHES DIFFER on {len(mismatches)}: {', '.join(mismatches)}")
    else:
        print(f"  all {len(queries)} queries agree across {', '.join(systems)}")

    if args.json:
        args.json.write_text(json.dumps({
            "manifest": meta,
            "queries": [{"id": q.id, "op": q.op, "note": q.note,
                         "requires": list(q.requires)} for q in queries],
            "results": {s: [vars(r) for r in rs] for s, rs in results.items()},
            "agree": not mismatches,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  wrote {args.json}")

    # Phase 0's exit criterion: identical hashes across the TGMS backends.
    # A PostgreSQL gap is expressiveness, not a bug, so it must not fail here.
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
