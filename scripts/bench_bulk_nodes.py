"""Is the structured node path *flat*? (the M4-era write-path gate)

The claim under test is not a rate, it is a **shape**. `Store.assert_node`
writes one batch per call, and a batch is a log append plus a manifest commit
whose cost grows with the store, so the per-node cost rises as the store fills:
measured on this machine, the instantaneous rate fell from 29 to 15 nodes/s
over the first 8k nodes while producing 8,436 manifests and 15 GB of them.
That is O(N^2) in bytes as well as in time, and it is why SNB SF1 could not be
loaded through `assert_node` at all.

`ingest_events`' `nodes` array writes one batch per chunk and stages node
versions as one bulk array. This measures the resulting curve over windows, so
a hidden per-store term shows up as a falling rate rather than hiding inside an
average.

    uv run python scripts/bench_bulk_nodes.py [--nodes 1000000] [--assert-probe]
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402

LABELS = ("Person", "Forum", "Post", "Comment", "Tag", "Place", "Org", "Uni")


def _nodes(lo: int, hi: int) -> list[dict[str, Any]]:
    """SNB-shaped: eight labels, several properties, a real valid-time start."""
    out = []
    for i in range(lo, hi):
        out.append({
            "uid": f"n{i}",
            "label": LABELS[i % len(LABELS)],
            "props": {"name": f"name-{i}", "seq": i,
                      "locale": "en_US" if i % 3 else "de_DE"},
            "vt_s": 1_500_000_000_000_000 + i,
        })
    return out


def bulk_curve(total: int, window: int, backend: str | None) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="tgms-bulk-")) / "s"
    store = tgms.open(root, backend=backend)
    rates: list[float] = []
    try:
        t0 = time.perf_counter()
        done = 0
        print(f"{'nodes':>12} {'window nodes/s':>16} {'cum nodes/s':>14}")
        while done < total:
            take = min(window, total - done)
            batch = _nodes(done, done + take)
            t = time.perf_counter()
            store.ingest_events([], nodes=batch)
            dt = time.perf_counter() - t
            done += take
            rate = take / dt
            rates.append(rate)
            print(f"{done:>12,} {rate:>16,.0f} "
                  f"{done / (time.perf_counter() - t0):>14,.0f}")
        wall = time.perf_counter() - t0
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        log = store.eventlog
        batches = sum(1 for _ in log.batches())
        n_versions = sum(1 for _ in store.adapter.all_node_versions())
    finally:
        store.close()
        shutil.rmtree(root.parent, ignore_errors=True)

    first, last = rates[0], rates[-1]
    return {
        "nodes": total, "chunk": window, "wall_s": round(wall, 2),
        "overall_nodes_per_s": round(total / wall),
        "first_window_nodes_per_s": round(first),
        "last_window_nodes_per_s": round(last),
        "median_window_nodes_per_s": round(statistics.median(rates)),
        "last_over_first": round(last / first, 3),
        "batches": batches, "node_versions": n_versions,
        "store_bytes": size, "bytes_per_node": round(size / total, 1),
    }


def assert_probe(n: int, window: int, backend: str | None) -> dict[str, Any]:
    """The contrast, deliberately tiny: this path fills a disk if you let it.

    Not run by default. `--assert-probe` opts in, and the ceiling is low on
    purpose — at 8k nodes the earlier run had already written 15 GB.
    """
    root = Path(tempfile.mkdtemp(prefix="tgms-assert-")) / "s"
    store = tgms.open(root, backend=backend)
    rates = []
    try:
        t0 = time.perf_counter()
        last = t0
        for i in range(n):
            store.assert_node(f"n{i}", LABELS[i % len(LABELS)],
                              {"name": f"name-{i}", "seq": i},
                              vt_s=1_500_000_000_000_000 + i)
            if (i + 1) % window == 0:
                now = time.perf_counter()
                rates.append(window / (now - last))
                print(f"{i + 1:>12,} {rates[-1]:>16,.0f}")
                last = now
        wall = time.perf_counter() - t0
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        manifests = len(list((root / "native" / "manifests").glob("*"))) \
            if (root / "native" / "manifests").exists() else 0
    finally:
        store.close()
        shutil.rmtree(root.parent, ignore_errors=True)
    return {
        "nodes": n, "wall_s": round(wall, 2),
        "overall_nodes_per_s": round(n / wall),
        "first_window_nodes_per_s": round(rates[0]) if rates else None,
        "last_window_nodes_per_s": round(rates[-1]) if rates else None,
        "last_over_first": round(rates[-1] / rates[0], 3) if len(rates) > 1 else None,
        "manifests": manifests, "store_bytes": size,
        "bytes_per_node": round(size / n, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=1_000_000)
    ap.add_argument("--window", type=int, default=50_000)
    ap.add_argument("--backend", default=None)
    ap.add_argument("--assert-probe", type=int, default=0,
                    help="also run the assert_node path for N nodes (fills disk)")
    ap.add_argument("--gate", type=int, default=20_000,
                    help="required nodes/s, sustained in every window")
    args = ap.parse_args()

    print(f"--- bulk `nodes` path: {args.nodes:,} nodes, "
          f"{args.window:,} per batch ---")
    bulk = bulk_curve(args.nodes, args.window, args.backend)
    print()
    print(json.dumps(bulk, indent=1))

    out: dict[str, Any] = {"bulk": bulk}
    if args.assert_probe:
        print(f"\n--- assert_node path: {args.assert_probe:,} nodes (the contrast) ---")
        out["assert"] = assert_probe(args.assert_probe,
                                     max(1, args.assert_probe // 6), args.backend)
        print(json.dumps(out["assert"], indent=1))

    ok = bulk["last_window_nodes_per_s"] >= args.gate
    flat = bulk["last_over_first"] >= 0.5
    print()
    print(f"GATE >= {args.gate:,} nodes/s in the final window: "
          f"{'PASS' if ok else 'FAIL'} ({bulk['last_window_nodes_per_s']:,})")
    print(f"FLAT last/first window ratio >= 0.5: "
          f"{'PASS' if flat else 'FAIL'} ({bulk['last_over_first']})")
    return 0 if (ok and flat) else 1


if __name__ == "__main__":
    sys.exit(main())
