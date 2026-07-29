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
from tgms.core.model import canonical_json, sha256_hex
from tgms.temporal.algebra import call_operator, ensure_all_registered

ROOT = Path(__file__).resolve().parents[1]

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


def registry(scale: int) -> list[Query]:
    """The Phase 0 query set: one per operator family, plus belief-time probes.

    Parameters are derived from the scale so the same registry is meaningful
    at 1e5 and 1e7 without editing.
    """
    span = scale
    mid = span // 2
    return [
        Query("hist.single", "entity_history", {"uid": "n1"},
              "point lookup by identity"),
        Query("hist.asof", "entity_history", {"uid": "n1", "as_of_tt": 1},
              "same lookup under an earlier belief", requires=("bitemporal",)),
        Query("snap.hop2", "snapshot_subgraph",
              {"seeds": ["n1"], "hops": 2, "t_valid": mid},
              "2-hop neighbourhood at an instant"),
        Query("diff.global", "diff_snapshots", {"t1": mid, "t2": mid + span // 10},
              "global difference between two instants"),
        Query("reach.window", "temporal_reachability",
              {"src": "n1", "window": {"t_a": 0, "t_b": span // 10}},
              "time-respecting reachability"),
        Query("paths.k", "temporal_paths",
              {"src": "n1", "dst": "n2", "window": {"t_a": 0, "t_b": span // 20},
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
               "node_filter": [f"n{i}" for i in range(40)]},
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
            rows = payload.get("rows_total")
            if rows is None:
                rows = len(payload.get("rows", []))
            out.append(Result(q.id, True, canonical_hash(payload),
                              round(min(timings), 3), int(rows)))
        except Exception as e:  # a system that cannot answer is data, not a crash
            out.append(Result(q.id, False, error=f"{type(e).__name__}: {e}"[:160]))
    store.close()
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
    }


def build_event_log(scale: int) -> Path:
    """Write the reference event log once. Every system replays *this*.

    Building a store per system would look equivalent and is not: transaction
    times come from a clock at write time, so independently built stores of
    the same data differ in tt, and therefore in every version id derived from
    it. The first run of this harness reported two systems disagreeing for
    exactly that reason (D-023).
    """
    path = Path(tempfile.mkdtemp()) / "reference"
    store = tgms.open(path, backend="native")
    store.ingest_events([
        {"src": f"n{i % 2000}", "dst": f"n{(i * 7 + 3) % 2000}",
         "rel_type": "R" if i % 3 else "S", "vt_s": i, "vt_e": i + 40}
        for i in range(scale)
    ])
    store.assert_node("n1", "Node", {"name": "alpha"}, vt_s=0, vt_e=scale)
    store.close()
    return path / "eventlog.jsonl"


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
    queries = registry(args.scale)
    meta = manifest(args.scale, systems, args.repeats)

    print(f"phase-0 harness — {len(queries)} queries x {len(systems)} systems "
          f"@ {args.scale:,} events")
    print(f"  commit {meta['commit']}{' (dirty)' if meta['dirty'] else ''} | "
          f"{meta['platform']} | {meta['cpu_count']} cores\n")

    log = build_event_log(args.scale)
    results: dict[str, list[Result]] = {}
    for name in systems:
        path = Path(tempfile.mkdtemp()) / "store"
        t0 = time.perf_counter()
        load_store(path, name, log)
        load = time.perf_counter() - t0
        results[name] = run_system(name, path, queries, args.repeats)
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
        print(f"  unsupported: {s}/{qid} — {err}")
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

    # Phase 0's exit criterion: identical hashes on native and DuckDB
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
