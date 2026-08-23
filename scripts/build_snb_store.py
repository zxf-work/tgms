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
    LABEL_ROLLUP, SF1_EDGES, SF1_NODES, edge_events, edge_ops, fidelity,
    node_ops, node_records,
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

#: Fold segments and drop superseded manifests every this many ops.
#:
#: **Needed at any batch size and on any write path**, because the cost it
#: controls is driven by the *number of published generations*, not by how the
#: ops were spelled: every `_write` publishes a manifest that references every
#: live segment, so manifest bytes grow as O(batches^2).
#:
#: Measured on xzgpu (`runs/snb-sf1-build.log`, batch=250, no compaction): at
#: 2.5M node ops the store held 10,147 segments totalling **163 MB** and 10,147
#: manifests totalling **25,451 MB** — 99.4% of a 27 GB store was manifests, on
#: a trajectory to ~1.65 TB at completion. The run was killed there.
#:
#: With `compact()` + `gc(keep_last=2)` every 100,000 ops (`runs/calib.log`,
#: batch=1000): manifests **0 MB** and the whole store **285 MB** at 600k node
#: ops. That is the entire justification for this constant.
#:
#: **Honest gap:** that run did not isolate compaction's own time cost — there
#: is no same-batch-size no-compaction control at 600k ops, so the 379 s it
#: took is apply + compaction together and the split is unmeasured. The build
#: now records `compact_s` in its receipt so the next run closes this.
COMPACT_EVERY_OPS = 100_000


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


def _identity(store: Any, mode: str) -> dict[str, Any]:
    """How to name this store in a receipt.

    `full` is `Store.digest()` — the backend-independent digest of the whole
    logical content, and a **replay-equivalence check**, which is what it was
    written for. It sorts one Python dict per version and canonical-JSON-hashes
    the lot: at SF1 that is 20.4M dicts, 15.6 GB resident, and it had not
    returned after 50 minutes when the first build was killed in it. It is the
    right tool for a store small enough to compare against a replay, and the
    wrong one here.

    `manifest` is the default: the generation counter plus the manifest sha the
    engine already stamps its own verify walks with, plus `store_identity`.
    Cheap, stable, and a real identity for a receipt.
    """
    if mode == "none":
        return {"mode": "none"}
    if mode == "full":
        return {"mode": "full", "store_digest": store.digest()}
    out: dict[str, Any] = {"mode": "manifest",
                           "store_identity": store.store_identity}
    for name in ("generation",):
        try:
            out[name] = getattr(store.adapter, name)
        except Exception:                              # noqa: BLE001
            pass
    try:
        out["manifest_sha"] = store.adapter._store.manifest_sha()  # noqa: SLF001
    except Exception:                                  # noqa: BLE001
        pass
    return out


def build(csv_root: Path, out: Path, backend: str,
          batch: int = DEFAULT_BATCH,
          compact_every: int = COMPACT_EVERY_OPS,
          write_path: str = "bulk",
          digest_mode: str = "manifest") -> dict[str, Any]:
    import tgms

    if out.exists():
        raise SystemExit(f"{out} exists — refusing to write into a live store. "
                         f"Remove it deliberately, then re-run.")
    store = tgms.open(out, backend=backend)
    counts = {"nodes": 0, "edges": 0, "batches": 0, "compactions": 0}
    #: Labels tallied as records are emitted. The store exposes no label
    #: histogram, and materialising one costs >50 min at SF1 by either
    #: available route (`all_node_versions` and `nodes_columnar` both build a
    #: Python object per version). Counting here is free, and `fidelity()`
    #: cross-checks the total against the store's own `n_node_versions`, so a
    #: loader that emitted the right count with the wrong labels still fails.
    label_counts: dict[str, int] = {}

    def tally(label: str) -> None:
        key = LABEL_ROLLUP.get(label, label)
        label_counts[key] = label_counts.get(key, 0) + 1
    t0 = time.time()
    compact_s = [0.0]
    since_compaction = [0]

    def maybe_compact(force: bool = False) -> None:
        """Fold the small per-batch segments together and drop the manifests
        that referenced them. Skipped silently on a backend without either
        entry point, so this stays a native-store optimisation rather than a
        precondition."""
        if not compact_every:
            return
        if not force and since_compaction[0] < compact_every:
            return
        if since_compaction[0] == 0:
            return
        compact = getattr(store.adapter, "compact", None)
        gc = getattr(store.adapter, "gc", None)
        if compact is None or gc is None:
            return
        t = time.time()
        compact()
        gc(keep_last=2)
        compact_s[0] += time.time() - t
        counts["compactions"] += 1
        since_compaction[0] = 0

    def drive(stream: Iterator[dict[str, Any]], kind: str) -> None:
        for ops in _chunks(stream, batch):
            if kind == "nodes":
                for op in ops:
                    tally(op["label"])
            store._write(ops)                          # noqa: SLF001 — a writer
            counts[kind] += len(ops)
            counts["batches"] += 1
            since_compaction[0] += len(ops)
            maybe_compact()
            if counts["batches"] % 2_000 == 0:
                done = counts["nodes"] + counts["edges"]
                rate = done / max(time.time() - t0, 1e-9)
                print(f"  {done:>12,} ops  {rate:>9,.0f} ops/s  "
                      f"({counts['nodes']:,} nodes / {counts['edges']:,} edges; "
                      f"{counts['compactions']} compactions, "
                      f"{compact_s[0]:,.0f}s)", flush=True)

    if write_path == "bulk":
        # One call. `ingest_events` writes the `nodes` array first in its own
        # chunked batches, then the event stream — the path measured flat at
        # ~42.4k ev/s, against `assert_*`'s O(N^2). Counting has to happen
        # inside the generators, since the store consumes them lazily.
        print("bulk — nodes then events through ingest_events", flush=True)

        def counted(stream, kind):
            for rec in stream:
                if kind == "nodes":
                    tally(rec["label"])
                counts[kind] += 1
                since_compaction[0] += 1
                if counts[kind] % 1_000_000 == 0:
                    done = counts["nodes"] + counts["edges"]
                    print(f"  {done:>12,} records  "
                          f"{done / max(time.time() - t0, 1e-9):>9,.0f} rec/s",
                          flush=True)
                yield rec

        store.ingest_events(counted(edge_events(csv_root), "edges"),
                            nodes=counted(node_records(csv_root), "nodes"))
        counts["batches"] = -1                         # owned by the store
        # No periodic compaction on this path, deliberately. The manifest
        # problem is driven by the *number* of published generations, and
        # `ingest_events` publishes one per 50,000-op chunk: ~408 batches for
        # the whole of SF1 against ~81,600 at `--batch 250`. Manifest bytes go
        # as O(batches^2), so 408 batches is ~40 MB — a rounding error rather
        # than the 25 GB the assert path reached. The single forced fold below
        # is enough, and mid-load compaction would only cost time.
    else:
        print("assert — pass 1/2 nodes", flush=True)
        drive(node_ops(csv_root), "nodes")
        print("assert — pass 2/2 edges", flush=True)
        drive(edge_ops(csv_root), "edges")
    maybe_compact(force=True)                          # leave the store folded

    wall = time.time() - t0
    stats = store.stats()
    digest = _identity(store, digest_mode)
    store.close()

    return {"counts": counts, "wall_s": round(wall, 1),
            "compact_s": round(compact_s[0], 1), "stats": stats,
            "digest": digest, "labels": label_counts}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True,
                    help="the initial_snapshot directory (with static/ and dynamic/)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="native", choices=("native", "duckdb"))
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help="ops per event-log record; see DEFAULT_BATCH — "
                         "apply_ops is O(k^2) in this number")
    ap.add_argument("--digest", default="manifest",
                    choices=("none", "manifest", "full"), dest="digest_mode",
                    help="store identity for the receipt; see _identity — "
                         "'full' is the replay-equivalence digest and is "
                         "impractical above a few million versions")
    ap.add_argument("--write-path", default="bulk", choices=("bulk", "assert"),
                    help="bulk: ingest_events with the nodes array (flat, the "
                         "default). assert: assert_node/assert_edge per op — "
                         "kept for A/B evidence; O(N^2), see DEFAULT_BATCH")
    ap.add_argument("--compact-every", type=int, default=COMPACT_EVERY_OPS,
                    help="fold segments and drop superseded manifests every N "
                         "ops (0 disables); see COMPACT_EVERY_OPS — manifest "
                         "bytes grow O(batches^2) without it")
    args = ap.parse_args()

    csv_root, out = Path(args.csv), Path(args.out)
    sha = _sha()
    print(f"RUN_STARTED commit={sha} csv={csv_root} out={out} "
          f"backend={args.backend} batch={args.batch} "
          f"compact_every={args.compact_every} path={args.write_path} "
          f"digest={args.digest_mode} "
          f"host={platform.node()}", flush=True)

    result = build(csv_root, out, args.backend, args.batch,
                   args.compact_every, args.write_path,
                   args.digest_mode)
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
        "compact_every": args.compact_every,
        "write_path": args.write_path,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": result["wall_s"],
        "compact_s": result["compact_s"],
        "ops": result["counts"],
        "store_identity": result["digest"],
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
    print(f"compaction: {result['counts']['compactions']} runs, "
          f"{result['compact_s']}s of {result['wall_s']}s wall")
    print(f"identity: {result['digest']}  wall: {result['wall_s']}s  "
          f"bytes: {card['store_bytes']:,}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
