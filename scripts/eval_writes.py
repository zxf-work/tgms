"""Write-path evaluation: load, append, corrections, compaction (plan §12).

Every benchmark so far measured reads over stores built once by bulk replay.
The write path is what agents actually generate — small appends, corrections,
retractions — and its costs were only ever discovered incidentally
(`engine_lessons.md` §9b). This measures them on purpose.

PostgreSQL is absent by design: it is a baseline that never implements the
write semantics (D-030), so there is nothing equivalent to measure.

    uv run python scripts/eval_writes.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tgms
from eval_harness import _edge_ref, _event, edge_life  # shared generator


def _du(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, -(-int(p * len(xs)) // 1) - 1))]


def fresh(backend: str) -> tgms.store.Store:
    return tgms.open(Path(tempfile.mkdtemp()) / "s", backend=backend)


def bench_load(backend: str, scale: int, runs: int = 3) -> dict:
    """Bulk ingest throughput — the batched path real loads use."""
    times = []
    for _ in range(runs):
        st = fresh(backend)
        t0 = time.perf_counter()
        st.ingest_events([_event(i, scale) for i in range(scale)])
        times.append(time.perf_counter() - t0)
        st.close()
    best = min(times)
    return {"scale": scale, "seconds": round(best, 2),
            "events_per_s": int(scale / best), "runs": runs}


def bench_append(backend: str, batch: int, total: int = 2000) -> dict:
    """Sustained small-batch appends: the agent write shape.

    Each call is one durable commit; the per-commit fsync floor is the
    designed cost (§7 of the lessons), so what matters is how latency
    divides by batch size.
    """
    st = fresh(backend)
    base = 100_000
    st.ingest_events([_event(i, base) for i in range(20_000)])  # non-empty store
    lat = []
    n = 0
    while n < total:
        events = [_event(base + n + j, base) for j in range(batch)]
        t0 = time.perf_counter()
        st.ingest_events(events)
        lat.append((time.perf_counter() - t0) * 1e3)
        n += batch
    st.close()
    return {"batch": batch, "commits": len(lat),
            "p50_ms": round(statistics.median(lat), 2),
            "p95_ms": round(pctl(lat, 0.95), 2),
            "events_per_s": int(total / (sum(lat) / 1e3))}


def bench_corrections(backend: str, scale: int = 100_000, n_corr: int = 200) -> dict:
    """Single-op corrections against a populated store, plus what they cost
    in space: a correction closes one version and writes another, so the
    store must grow — the question is by how much."""
    st = fresh(backend)
    st.ingest_events([_event(i, scale) for i in range(scale)])
    root = Path(st.path)
    before = _du(root)
    lat = []
    step = max(1, scale // n_corr)
    for i in range(0, scale, step):
        e = _event(i, scale)
        t0 = time.perf_counter()
        st.correct(_edge_ref(i, scale), {"weight": 2},
                   vt_s=e["vt_s"], vt_e=e["vt_e"])
        lat.append((time.perf_counter() - t0) * 1e3)
    grew = _du(root) - before
    digest = st.digest()
    st.close()
    return {"corrections": len(lat),
            "p50_ms": round(statistics.median(lat), 2),
            "p95_ms": round(pctl(lat, 0.95), 2),
            "bytes_per_correction": int(grew / len(lat)),
            "digest": digest[:16]}


def bench_compaction(backend: str, scale: int = 100_000) -> dict:
    """Compaction on a corrected store: wall time, transient peak, and the
    non-negotiable — the digest must not move."""
    st = fresh(backend)
    st.ingest_events([_event(i, scale) for i in range(scale)])
    step = max(1, scale // 200)
    for i in range(0, scale, step):
        e = _event(i, scale)
        st.correct(_edge_ref(i, scale), {"weight": 2},
                   vt_s=e["vt_s"], vt_e=e["vt_e"])
    for i in range(0, scale, step * 4):
        st.retract(_edge_ref(i, scale), _event(i, scale)["vt_s"] + edge_life(scale) // 2)
    adapter = st.adapter
    if not hasattr(adapter, "compact"):
        st.close()
        return {"supported": False}
    root = Path(st.path)
    before_bytes = _du(root)
    before_digest = st.digest()
    t0 = time.perf_counter()
    stats = adapter.compact()
    seconds = time.perf_counter() - t0
    after_bytes = _du(root)
    after_digest = st.digest()
    st.close()
    return {"supported": True, "seconds": round(seconds, 2),
            "bytes_before": before_bytes, "bytes_after": after_bytes,
            "digest_preserved": before_digest == after_digest,
            "stats": stats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="native,duckdb")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    out: dict = {}
    for backend in [s.strip() for s in args.systems.split(",")]:
        r: dict = {"load": [], "append": []}
        print(f"== {backend}")
        for scale in (200_000, 1_000_000):
            row = bench_load(backend, scale)
            r["load"].append(row)
            print(f"  load {scale:>9,}: {row['seconds']:6.2f}s  "
                  f"{row['events_per_s']:>9,} ev/s")
        for batch in (1, 10, 100, 1000):
            row = bench_append(backend, batch)
            r["append"].append(row)
            print(f"  append b={batch:<5} p50 {row['p50_ms']:7.2f} ms  "
                  f"p95 {row['p95_ms']:7.2f}  {row['events_per_s']:>7,} ev/s")
        r["corrections"] = bench_corrections(backend)
        c = r["corrections"]
        print(f"  correct: p50 {c['p50_ms']} ms p95 {c['p95_ms']} "
              f"({c['bytes_per_correction']} B each)")
        r["compaction"] = bench_compaction(backend)
        print(f"  compact: {r['compaction']}")
        out[backend] = r
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
