"""What `KuzuAdapter.believed_edge_versions` costs: scan vs anchored (D-145).

`eid` is a hash of (src, dst, rel_type, disc) with no stored inverse, so a
bare `MATCH (a:Entity)-[e:EdgeVersion]->(b:Entity) WHERE e.eid = $eid` is a
property scan of the whole EdgeVersion rel table. When the caller supplies
`src`/`dst` (every base.py caller does — see `believed_edge_versions`'s
docstring in `tgms/storage/kuzu_adapter.py`), the query anchors on both
Entity primary keys instead, turning the scan into a key lookup plus a scan
of just that (src, dst) adjacency. This script measures the two side by
side, at growing store sizes, to keep the docstring's numbers reproducible
rather than remembered (the D-072 lesson `bench_corrections.py` names).

Builds one growing Kùzu store of EdgeVersion rows spread across many
(src, dst) pairs, with several distinct logical edges (rel_type) per pair so
multiple eids share endpoints -- the case the anchored query has to stay
correct under (`e.eid = $eid` stays in the WHERE for exactly this reason).
Each logical edge gets a short bi-temporal history: several DISJOINT,
non-overlapping valid-time versions closed at successive tt, then one final
version left open (tt_e=OPEN_END) -- so `believed_edge_versions` at
OPEN_END returns exactly one row per eid, as it would in a real store, and
no two versions of one eid share a vt_s. That last property is what makes
`ORDER BY e.vt_s` deterministic, so the scan and anchored result lists are
directly comparable row-for-row rather than merely set-equal.

Measurements are taken at cumulative checkpoints (each includes the rows
from the checkpoints before it) so the whole sweep pays for insertion once.

    PYTHONPATH=<worktree> <venv python> scripts/bench_believed_edges.py
    PYTHONPATH=<worktree> <venv python> scripts/bench_believed_edges.py \\
        --checkpoints 2000,4000 --n-calls 30 --warmup 5   # smoke run
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgms.core.model import OPEN_END, EdgeVersion  # noqa: E402
from tgms.storage.kuzu_adapter import KuzuAdapter  # noqa: E402

N_ENTITIES = 220          # distinct uids; N*(N-1) unique ordered pairs, no wraparound needed
EDGES_PER_PAIR = 4        # distinct logical edges (rel_type) sharing one (src, dst)
N_VERSIONS = 3            # tt-versions per logical edge (2 closed + 1 open)


def _pair(i: int, n_entities: int) -> tuple[int, int]:
    """i-th of n_entities*(n_entities-1) unique ordered (src_idx, dst_idx)
    pairs, src != dst -- enough distinct (src, dst) pairs that a checkpoint
    sweep never has to revisit one (revisiting would re-open an eid that
    already has an open version, breaking the one-open-row-per-eid shape the
    deterministic-order argument above depends on)."""
    src = i % n_entities
    j = (i // n_entities) % (n_entities - 1)
    dst = (src + 1 + j) % n_entities
    return src, dst


def time_calls(fn, n: int, warmup: int) -> float:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def run(workdir: Path, checkpoints: list[int], n_calls: int, warmup: int) -> list[tuple[int, float, float]]:
    adapter = KuzuAdapter(workdir / "bench.kuzu")

    uids = [f"e{i}" for i in range(N_ENTITIES)]
    adapter.ensure_entities([(u, "N") for u in uids])

    probe_eid = probe_src = probe_dst = None
    results: list[tuple[int, float, float]] = []
    n_rows_written = 0
    tt = 1
    pair_i = 0

    for target in checkpoints:
        rows: list[EdgeVersion] = []
        while n_rows_written + len(rows) < target:
            si, di = _pair(pair_i, N_ENTITIES)
            src, dst = uids[si], uids[di]
            pair_i += 1
            for k in range(EDGES_PER_PAIR):
                rel_type = f"R{k}"
                eid_str = f"{src}|{dst}|{rel_type}"
                if probe_eid is None:
                    probe_eid, probe_src, probe_dst = eid_str, src, dst
                for j in range(N_VERSIONS):
                    vt_s, vt_e = j * 10, (j + 1) * 10
                    tt_s = tt
                    tt_e = tt + 1 if j < N_VERSIONS - 1 else OPEN_END
                    rows.append(EdgeVersion(
                        eid=eid_str, vid=f"v{n_rows_written + len(rows)}",
                        src=src, dst=dst, rel_type=rel_type, disc="",
                        vt_s=vt_s, vt_e=vt_e, tt_s=tt_s, tt_e=tt_e,
                        props={"w": j}, source="bench", provenance_ref=None))
                    tt += 1

        adapter.begin()
        adapter.insert_edge_versions(rows)
        adapter.commit()
        n_rows_written += len(rows)

        n_rows = sum(1 for _ in adapter.all_edge_versions())

        scan_ms = time_calls(
            lambda: adapter.believed_edge_versions(probe_eid, OPEN_END), n_calls, warmup)
        anchored_ms = time_calls(
            lambda: adapter.believed_edge_versions(probe_eid, OPEN_END,
                                                    src=probe_src, dst=probe_dst),
            n_calls, warmup)

        r1 = adapter.believed_edge_versions(probe_eid, OPEN_END)
        r2 = adapter.believed_edge_versions(probe_eid, OPEN_END, src=probe_src, dst=probe_dst)
        assert r1 == r2, ("scan and anchored results diverge!", r1, r2)
        assert len(r1) == 1, f"expected exactly 1 open version, got {len(r1)}"

        results.append((n_rows, scan_ms, anchored_ms))
        print(f"checkpoint reached: {n_rows} edge rows written so far", flush=True)

    adapter.close()
    return results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoints", default="20000,80000,200000",
                   help="comma-separated cumulative edge-row targets")
    p.add_argument("--n-calls", type=int, default=300,
                   help="timed calls per path per checkpoint")
    p.add_argument("--warmup", type=int, default=20)
    p.add_argument("--workdir", default=None,
                   help="directory to build the Kùzu store in "
                        "(default: a fresh temp dir, cleaned up after)")
    args = p.parse_args()
    checkpoints = [int(c) for c in args.checkpoints.split(",")]

    def report(results):
        print(f"\n{'rows':>10}  {'scan ms/call':>14}  {'anchored ms/call':>18}  {'ratio':>8}")
        for n_rows, scan_ms, anchored_ms in results:
            ratio = scan_ms / anchored_ms if anchored_ms else float("inf")
            print(f"{n_rows:>10}  {scan_ms:>14.3f}  {anchored_ms:>18.3f}  {ratio:>7.1f}x")

    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        report(run(workdir, checkpoints, args.n_calls, args.warmup))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            report(run(Path(tmp), checkpoints, args.n_calls, args.warmup))


if __name__ == "__main__":
    main()
