"""The correction-density matrix: what corrections cost, as a standing sweep.

The §13 experiment (`scripts/eval_bitemporal.py`) asked what two clocks cost
at rest and swept exactly one axis — correction density — once. That single
number found a real defect, was fixed, and then went on being quoted at its
pre-fix value for four days across three internal documents and an external
review, because nothing re-measured it (D-072). This script exists so the
number is reproducible on demand rather than remembered.

Four axes, because density alone conflates them:

- **density** — corrections as a fraction of writes. The axis §13 swept.
- **batch size** — corrections per committed generation. Never swept before;
  a batch is one seal + fsync + close run + manifest, so this is the axis
  that decides whether the fixed commit cost is amortized or paid per row.
- **versions per identity** — how deep one entity's belief history is. A
  correction resolves its target through `believed_*_versions(identity)`,
  which returns *every* version of that identity, so this axis prices
  per-entity history depth rather than store size.
- **out-of-order distance** — how far back in valid time a correction lands.
  Lookup is keyed by identity rather than time, so this should price
  interval carving (`base.py::_remainder`) and not the lookup.

Reported per cell: throughput, p50/p95/p99 commit latency, replay time, and
the storage breakdown that gives write and space amplification.

The 2D core (density × batch size) is a full grid. The other two axes are
swept one at a time against a fixed baseline, because the full cross product
is 5 × 5 × 4 × 4 = 400 cells and the marginal information does not pay for
it — a full grid is available with --grid all if a cell ever looks
interactive.

    # CI scale, seconds, what tests/test_correction_scaling.py gates
    python scripts/bench_corrections.py --profile ci --json out.json

    # release scale, run on xzgpu
    python scripts/bench_corrections.py --profile full --json out.json

Forecast and scoring live in docs/bench_corrections.md — written before the
first run, per the standing rule.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgms  # noqa: E402

OPEN_END = 1_000_000


# --- profiles -------------------------------------------------------------- #
#
# `ci` must finish in seconds inside a 15-minute total budget (spec §7.5);
# `full` is the release matrix and expects xzgpu.

#
# `events` is the base the density percentages are taken against, exactly as
# in the §13 sweep: a 5% cell applies `events * 0.05` corrections. `max_
# batches` bounds the number of *commits* a single cell may pay for, because
# a batch-size-1 cell at 20% density would otherwise run 200,000 commits at
# ~25 ms each. Cells that hit that bound are truncated, reported with
# `truncated: true`, and printed — a silently capped cell reads as full
# coverage when it is not.

PROFILES: dict[str, dict[str, Any]] = {
    "ci": {
        "n_entities": 500,
        "events": 20_000,
        "max_batches": 120,
        "densities": [1, 5, 20],
        "batch_sizes": [1, 10, 100],
        "depths": [1, 5, 20],
        "ooo_distances": [0, 100, 10_000],
    },
    "full": {
        "n_entities": 20_000,
        "events": 1_000_000,
        "max_batches": 2_000,
        "densities": [1, 5, 20, 50],
        "batch_sizes": [1, 10, 100, 1_000, 10_000],
        "depths": [1, 10, 100, 1_000],
        "ooo_distances": [0, 100, 10_000, 1_000_000],
    },
}

BASELINE_BATCH = 100
BASELINE_DEPTH = 1
BASELINE_OOO = 0


# --- measurement ----------------------------------------------------------- #


def storage_breakdown(root: Path) -> dict[str, int]:
    """Bytes by component. Mirrors eval_bitemporal.storage_breakdown so the
    two harnesses' storage columns are comparable."""
    native = root / "native" if (root / "native").exists() else root
    buckets = {
        "segments": native / "seg",
        "manifests": native / "manifests",
        "close_runs": native / "close",
    }
    out: dict[str, int] = {}
    for name, path in buckets.items():
        out[name] = (
            sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            if path.exists() else 0
        )
    dict_log = native / "dict.log"
    out["dict"] = dict_log.stat().st_size if dict_log.exists() else 0
    accounted = set(buckets.values())
    out["other"] = sum(
        f.stat().st_size for f in native.rglob("*")
        if f.is_file() and f.name != "dict.log"
        and not any(p in accounted for p in f.parents)
    )
    out["store_total"] = sum(out.values())
    return out


def percentiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    s = sorted(xs)

    def pick(q: float) -> float:
        # nearest-rank; with few batches an interpolating percentile would
        # invent a latency no commit actually had
        return s[min(len(s) - 1, max(0, int(round(q * len(s))) - 1))]

    return {"p50": round(pick(0.50), 3),
            "p95": round(pick(0.95), 3),
            "p99": round(pick(0.99), 3)}


def _seed_ops(n_entities: int, depth: int) -> list[list[dict[str, Any]]]:
    """Initial history: `depth` versions per identity before the sweep starts.

    Depth is built by overwriting the whole interval, so each round leaves one
    believed version and `depth` retained ones — the shape a long-lived entity
    reaches, not a synthetic pile of disjoint intervals.
    """
    rounds = []
    for d in range(depth):
        rounds.append([
            {"op": "assert_node", "uid": f"e{i}", "label": "N",
             "props": {"v": d}, "vt_s": 0, "vt_e": OPEN_END}
            for i in range(n_entities)
        ])
    return rounds


def _correction_ops(i: int, n_entities: int, ooo: int) -> dict[str, Any]:
    """One correction against entity `i % n_entities`.

    `ooo` is how far back in valid time a correction may land. At 0 every
    correction rewrites the whole open interval, which is the cheap case: an
    overwrite with nothing left over. Above 0 the start is spread
    deterministically across `[0, ooo)`, so the correction lands *inside* the
    existing interval and forces a carve (`base.py::_remainder`) — the cost
    this axis is meant to price. 7919 is prime, so the spread does not
    resonate with `n_entities`.
    """
    vt_s = (i * 7919) % ooo if ooo else 0
    return {"op": "assert_node", "uid": f"e{i % n_entities}", "label": "N",
            "props": {"v": i + 1000},
            "vt_s": vt_s, "vt_e": OPEN_END}


def run_cell(
    *,
    n_entities: int,
    n_corrections: int,
    batch_size: int,
    depth: int,
    ooo: int,
    measure_replay: bool,
) -> dict[str, Any]:
    """One matrix cell against a fresh store."""
    work = Path(tempfile.mkdtemp(prefix="tgms-bench-corr-"))
    root = work / "store"
    store = tgms.open(root, backend="native")
    adapter = store.adapter
    adapter.paranoid = False  # the disjointness check is O(versions) per op

    tt = 1000
    for ops in _seed_ops(n_entities, depth):
        tt += 1
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()
    seed_bytes = storage_breakdown(root)["store_total"]

    latencies: list[float] = []
    t_start = time.perf_counter()
    applied = 0
    while applied < n_corrections:
        take = min(batch_size, n_corrections - applied)
        ops = [_correction_ops(applied + j, n_entities, ooo) for j in range(take)]
        tt += 1
        t0 = time.perf_counter()
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()
        latencies.append((time.perf_counter() - t0) * 1000)
        applied += take
    wall = time.perf_counter() - t_start
    store.close()

    storage = storage_breakdown(root)
    rec: dict[str, Any] = {
        "batch_size": batch_size,
        "depth": depth,
        "ooo": ooo,
        "n_entities": n_entities,
        "corrections": n_corrections,
        "batches": len(latencies),
        "wall_s": round(wall, 3),
        "throughput_per_s": round(n_corrections / wall, 1) if wall else None,
        "ms_per_correction": round(wall * 1000 / n_corrections, 4),
        "commit_latency_ms": percentiles(latencies),
        "storage": storage,
        "bytes_per_correction": round(
            (storage["store_total"] - seed_bytes) / n_corrections, 1),
        # the marginal shape: late batches against early ones. Flat is
        # linear; growth is the quadratic close-run rebuild returning.
        "marginal_ratio": _marginal_ratio(latencies),
    }

    if measure_replay:
        log = root / "eventlog.jsonl"
        if log.exists():
            replay_root = work / "replayed"
            t0 = time.perf_counter()
            from tgms.storage.eventlog import replay

            s2 = tgms.open(replay_root, backend="native")
            replay(log, s2.adapter)
            s2.close()
            rec["replay_s"] = round(time.perf_counter() - t0, 3)
            rec["replay_storage"] = storage_breakdown(replay_root)

    return rec


def _marginal_ratio(latencies: list[float]) -> float | None:
    """Mean of the last quarter of batches over the first quarter.

    This is the host-independent linearity statistic and the one
    tests/test_correction_scaling.py gates on: a correction batch must not
    pay for the corrections committed before it.
    """
    if len(latencies) < 8:
        return None
    q = len(latencies) // 4
    early = statistics.mean(latencies[:q])
    late = statistics.mean(latencies[-q:])
    return round(late / early, 3) if early else None


# --- the sweep ------------------------------------------------------------- #


def sweep(profile: dict[str, Any], want_replay: bool) -> dict[str, Any]:
    n_ent = profile["n_entities"]
    events = profile["events"]
    max_batches = profile["max_batches"]
    out: dict[str, Any] = {"axes": {}, "truncated_cells": []}

    def budget(target: int, batch_size: int) -> tuple[int, bool]:
        """Corrections this cell may actually run, and whether that is a cut."""
        allowed = batch_size * max_batches
        return (min(target, allowed), target > allowed)

    # 2D core: density × batch size. Density is taken against the profile's
    # event base exactly as §13 took it — a 5% cell revises 5% of the writes.
    grid = []
    for pct in profile["densities"]:
        target = max(1, int(events * pct / 100))
        for bs in profile["batch_sizes"]:
            corrections, cut = budget(target, bs)
            note = f" [capped from {target}]" if cut else ""
            print(f"  density {pct}% × batch {bs} "
                  f"({corrections} corrections){note} …", flush=True)
            cell = run_cell(
                n_entities=n_ent, n_corrections=corrections, batch_size=bs,
                depth=BASELINE_DEPTH, ooo=BASELINE_OOO,
                measure_replay=want_replay and bs == BASELINE_BATCH and not cut,
            )
            cell["density_pct"] = pct
            cell["density_target_corrections"] = target
            cell["truncated"] = cut
            if cut:
                out["truncated_cells"].append(
                    f"density {pct}% × batch {bs}: ran {corrections} of {target}"
                )
            grid.append(cell)
    out["axes"]["density_x_batch"] = grid

    # the two single-axis sweeps run at a fixed correction count so their
    # cells are comparable to each other rather than to the grid
    axis_corrections = min(events // 100, BASELINE_BATCH * max_batches)

    depths = []
    for d in profile["depths"]:
        print(f"  depth {d} versions/identity ({axis_corrections} corrections) …",
              flush=True)
        depths.append(run_cell(
            n_entities=n_ent, n_corrections=axis_corrections,
            batch_size=BASELINE_BATCH, depth=d, ooo=BASELINE_OOO,
            measure_replay=False,
        ))
    out["axes"]["versions_per_identity"] = depths

    ooos = []
    for dist in profile["ooo_distances"]:
        print(f"  out-of-order distance {dist} ({axis_corrections} corrections) …",
              flush=True)
        ooos.append(run_cell(
            n_entities=n_ent, n_corrections=axis_corrections,
            batch_size=BASELINE_BATCH, depth=BASELINE_DEPTH, ooo=dist,
            measure_replay=False,
        ))
    out["axes"]["out_of_order_distance"] = ooos

    return out


def manifest() -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        except Exception:
            return ""

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }


def report(rec: dict[str, Any]) -> None:
    print("\n=== density × batch size ===")
    print(f"{'dens%':>6} {'batch':>7} {'corr':>8} {'ms/corr':>9} "
          f"{'thru/s':>10} {'p50':>8} {'p99':>9} {'marg':>7}  cut")
    for c in rec["axes"]["density_x_batch"]:
        lat = c["commit_latency_ms"]
        marg = c["marginal_ratio"]
        print(f"{c['density_pct']:>6} {c['batch_size']:>7} {c['corrections']:>8} "
              f"{c['ms_per_correction']:>9.4f} {c['throughput_per_s']:>10.1f} "
              f"{lat['p50']:>8.2f} {lat['p99']:>9.2f} "
              f"{('—' if marg is None else f'{marg:.2f}x'):>7}  "
              f"{'yes' if c.get('truncated') else ''}")
    if rec.get("truncated_cells"):
        print(f"\n  {len(rec['truncated_cells'])} cell(s) capped at "
              f"the profile's commit budget — not full coverage:")
        for line in rec["truncated_cells"]:
            print(f"    - {line}")

    print("\n=== versions per identity ===")
    print(f"{'depth':>6} {'ms/corr':>9} {'thru/s':>10} {'bytes/corr':>11}")
    for c in rec["axes"]["versions_per_identity"]:
        print(f"{c['depth']:>6} {c['ms_per_correction']:>9.4f} "
              f"{c['throughput_per_s']:>10.1f} {c['bytes_per_correction']:>11.1f}")

    print("\n=== out-of-order valid-time distance ===")
    print(f"{'dist':>9} {'ms/corr':>9} {'thru/s':>10} {'bytes/corr':>11}")
    for c in rec["axes"]["out_of_order_distance"]:
        print(f"{c['ooo']:>9} {c['ms_per_correction']:>9.4f} "
              f"{c['throughput_per_s']:>10.1f} {c['bytes_per_correction']:>11.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", choices=sorted(PROFILES), default="ci")
    ap.add_argument("--json", type=Path, help="write the full record here")
    ap.add_argument("--replay", action="store_true",
                    help="also time event-log replay per density (slow)")
    args = ap.parse_args()

    profile = PROFILES[args.profile]
    print(f"correction matrix — profile {args.profile}: "
          f"{profile['n_entities']} entities over a {profile['events']}-event "
          f"base, at most {profile['max_batches']} commits per cell")

    t0 = time.perf_counter()
    rec = sweep(profile, args.replay)
    rec["manifest"] = manifest() | {
        "profile": args.profile,
        "total_s": round(time.perf_counter() - t0, 1),
    }

    report(rec)
    print(f"\ntotal {rec['manifest']['total_s']} s")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rec, indent=2) + "\n")
        print(f"record → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
