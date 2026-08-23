"""Build the LDBC SNB SF1 store, then gate it against LDBC's published counts.

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing this script produces is an LDBC Benchmark Result.**

The mapping is `tgms/data/snb_loader.py`, frozen rule by rule in
`docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A3-A9 + §E addendum 2. This script
adds no mapping decision of its own; it batches, writes, and checks.

The store is written by `Store._write`, one event-log record per batch, exactly
as `scripts/build_ldbc_fixture.py` does at fixture scale — so the store is
reproducible by **replay of the log it writes** and never by a second ingest
(D-023: independently built stores differ in `tt` and in every derived id).

    uv run python scripts/build_snb_store.py \
        --csv /mnt/project/xzhang/tgms/ldbc-sf1/.../initial_snapshot \
        --out stores/snb-sf1

Exit status is 0 only if the fidelity gate passes. A mismatch is a mapping
defect or a data surprise; it is printed in full and it blocks.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgms.data.snb_loader import (  # noqa: E402
    SF1_EDGES, SF1_NODES, edge_ops, fidelity, node_ops, store_label_counts,
)

#: One event-log record per batch. **Small on purpose, and the number is
#: measured rather than chosen.**
#:
#: `StorageAdapter.apply_ops` costs O(k^2) in the batch size k: `_assert_node`
#: and `_assert_edge` each call `believed_*_versions` and `insert_*_versions`
#: once per op, and something in that path is linear in the ops already applied
#: *within the same batch*. The first attempt at this build used
#: `Store.INGEST_CHUNK` (50,000) and spent 11 minutes on batch one without
#: finishing it, having written the log record but not one segment.
#:
#: Measured on the same machine, one batch, native backend:
#:
#:     batch      500   1,000   2,000   4,000   8,000
#:     seconds  0.063   0.147   0.477   1.751   6.888     (~4x per 2x rows)
#:
#: The cost is **within** a batch, not in the store: at a fixed batch of 1,000
#: the per-batch time is flat as the store grows (1.02x over twelve batches),
#: so small batches make the whole load linear. Steady-state throughput by
#: batch size, projected onto SF1's 3.0M nodes + 17.4M edges:
#:
#:     batch      100     250     500   1,000   2,000
#:     est.     118min  64min   66min   97min  173min
#:
#: 250 is the floor of that curve — below it the per-record event-log overhead
#: takes over. Raising this back toward `INGEST_CHUNK` does not make the build
#: faster; it makes it not finish.
DEFAULT_BATCH = 250


def _chunks(it: Iterator[dict[str, Any]], n: int) -> Iterator[list[dict[str, Any]]]:
    buf: list[dict[str, Any]] = []
    for op in it:
        buf.append(op)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def build(csv_root: Path, out: Path, backend: str,
          batch: int = DEFAULT_BATCH) -> dict[str, Any]:
    import tgms

    if out.exists():
        raise SystemExit(f"{out} exists — refusing to write into a live store. "
                         f"Remove it deliberately, then re-run.")
    store = tgms.open(out, backend=backend)
    counts = {"nodes": 0, "edges": 0, "batches": 0}
    t0 = time.time()

    def drive(stream: Iterator[dict[str, Any]], kind: str) -> None:
        for ops in _chunks(stream, batch):
            store._write(ops)                          # noqa: SLF001 — a writer
            counts[kind] += len(ops)
            counts["batches"] += 1
            if counts["batches"] % 2_000 == 0:
                done = counts["nodes"] + counts["edges"]
                rate = done / max(time.time() - t0, 1e-9)
                print(f"  {done:>12,} ops  {rate:>9,.0f} ops/s  "
                      f"({counts['nodes']:,} nodes / {counts['edges']:,} edges)",
                      flush=True)

    print("pass 1/2 — nodes", flush=True)
    drive(node_ops(csv_root), "nodes")
    print("pass 2/2 — edges", flush=True)
    drive(edge_ops(csv_root), "edges")

    wall = time.time() - t0
    stats = store.stats()
    digest = store.digest()
    print(f"\nstreaming label histogram over {stats['n_node_versions']:,} "
          f"node versions", flush=True)
    labels = store_label_counts(store)
    store.close()

    return {"counts": counts, "wall_s": round(wall, 1), "stats": stats,
            "digest": digest, "labels": labels}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True,
                    help="the initial_snapshot directory (with static/ and dynamic/)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="native", choices=("native", "duckdb"))
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help="ops per event-log record; see DEFAULT_BATCH — "
                         "apply_ops is O(k^2) in this number")
    args = ap.parse_args()

    csv_root, out = Path(args.csv), Path(args.out)
    sha = _sha()
    print(f"RUN_STARTED commit={sha} csv={csv_root} out={out} "
          f"backend={args.backend} batch={args.batch} "
          f"host={platform.node()}", flush=True)

    result = build(csv_root, out, args.backend, args.batch)
    ok, lines = fidelity(result["labels"], result["stats"])

    print("\n=== mapping-fidelity gate ===")
    for line in lines:
        print(line)
    print(f"\nGATE: {'PASS' if ok else 'FAIL'}")

    card = {
        "dataset": "ldbc-snb-sf1",
        "note": ("LDBC SNB SF1 BI initial snapshot, mapped under "
                 "PAPER_A_EVIDENCE_FREEZE.md A3-A9 + addendum 2. NOT an LDBC "
                 "Benchmark Result; LDBC material under CC-BY 4.0."),
        "commit": sha,
        "host": platform.node(),
        "platform": platform.platform(),
        "backend": args.backend,
        "batch": args.batch,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": result["wall_s"],
        "ops": result["counts"],
        "store_digest": result["digest"],
        "store_bytes": sum(p.stat().st_size for p in out.rglob("*") if p.is_file()),
        "stats": result["stats"],
        "label_counts": result["labels"],
        "expected_nodes": SF1_NODES,
        "expected_edges": SF1_EDGES,
        "fidelity_gate": "PASS" if ok else "FAIL",
        "fidelity_table": lines,
    }
    (out / "dataset_card.json").write_text(
        json.dumps(card, indent=1, sort_keys=True, default=str))
    print(f"\ncard: {out / 'dataset_card.json'}")
    print(f"digest: {result['digest']}  wall: {result['wall_s']}s  "
          f"bytes: {card['store_bytes']:,}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
