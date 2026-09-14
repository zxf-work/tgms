"""Merge storm-v1 C6 freeze dag_phase task records into one committed
manifest (benchmarks/storm-v1/campaign.yaml's `dag_phase`: 5 shapes x
depth{4,16} x fanout{2,10} x seeds{0,1} = 40 cells, gate G-S2).

A separate, additive companion to `scripts/storm_campaign_merge.py`, not an
extension of it: that script's cell identity is (store, mix, age,
n_artifacts, seed, measure_ttf) and the dag_phase grid has no mix/age/
n_artifacts/ttf axis at all, only (shape, depth, fanout, seed) -- folding a
second, differently-shaped grid into that script's `_expected_cell` would
not be the smallest change (see `scripts/storm_campaign_dag.slurm`'s own
header for the same reasoning on the array-script side).

    python scripts/storm_campaign_dag_merge.py \\
        --records-dir /path/to/scpd/dag-records --node-meta-dir /path/to/scpd/node_meta \\
        --store synth-iv-60k --shapes chain,tree,diamond,layered,power-law \\
        --depths 4,16 --fanouts 2,10 --seeds 0,1 --commit 8962b78 \\
        --freeze-id storm-v1-2026-09-15 --freeze-sha256 ab002ebe... \\
        --out benchmarks/storm-v1/storm-campaign-dag-2026-09.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _expected_cell(task_id: int, shapes: list[str], depths: list[int], fanouts: list[int],
                   seeds: list[int]) -> dict[str, Any]:
    """Same convention as `storm_campaign_dag.slurm`: seed varies fastest,
    then fanout, then depth, then shape slowest."""
    rem = task_id
    n_seeds, n_fanouts, n_depths = len(seeds), len(fanouts), len(depths)
    seed_idx = rem % n_seeds
    rem //= n_seeds
    fanout_idx = rem % n_fanouts
    rem //= n_fanouts
    depth_idx = rem % n_depths
    rem //= n_depths
    shape_idx = rem % len(shapes)
    depth = depths[depth_idx]
    return {
        "shape": shapes[shape_idx], "depth": depth, "fanout": fanouts[fanout_idx],
        "seed": seeds[seed_idx], "cascade_k": depth,
    }


def load_tasks(records_dir: Path, n_tasks: int, store: str, shapes: list[str],
               depths: list[int], fanouts: list[int],
               seeds: list[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifests: list[dict[str, Any]] = []
    tasks_meta: list[dict[str, Any]] = []
    missing: list[int] = []
    for task_id in range(n_tasks):
        task_dir = records_dir / f"task-{task_id}"
        candidates = sorted(p for p in task_dir.glob("storm-*.json")) if task_dir.is_dir() \
            else []
        if not candidates:
            missing.append(task_id)
            continue
        if len(candidates) > 1:
            raise ValueError(f"task-{task_id}: expected exactly one storm-*.json manifest, "
                             f"found {[p.name for p in candidates]}")
        manifest = json.loads(candidates[0].read_text())
        expected = _expected_cell(task_id, shapes, depths, fanouts, seeds)
        dag = manifest.get("dag")
        if dag is None:
            raise ValueError(f"task-{task_id}: manifest has no \"dag\" phase recorded "
                             f"(expected {expected})")
        got_store = manifest.get("dataset", {}).get("name")
        if Path(store).name != got_store:
            raise ValueError(f"task-{task_id}: store {got_store!r} != expected {store!r}")
        got = {"shape": dag.get("shape"), "depth": dag.get("depth"),
              "fanout": dag.get("fanout"), "seed": manifest.get("seed", {}).get("value"),
              "cascade_k": dag.get("cascade_k")}
        for key in ("shape", "depth", "fanout", "seed", "cascade_k"):
            if got[key] != expected[key]:
                raise ValueError(f"task-{task_id}: {key} {got[key]!r} != expected "
                                 f"{expected[key]!r} (cell {expected})")
        manifest.setdefault("_task_id", task_id)
        manifests.append(manifest)
        tasks_meta.append({"task_id": task_id, **expected,
                          "wall_s": dag.get("wall_s")})
    if missing:
        raise FileNotFoundError(
            f"missing dag task record(s), campaign is incomplete: {missing}")
    return manifests, tasks_meta


def load_node_meta(node_meta_dir: Path | None, n_tasks: int, prefix: str) -> dict[str, Any]:
    """Best-effort, restated from `storm_campaign_merge.py::load_node_meta`."""
    hosts: set[str] = set()
    platform_line = ""
    cpus = None
    if node_meta_dir and node_meta_dir.is_dir():
        for task_id in range(n_tasks):
            hp = node_meta_dir / f"{prefix}-task-{task_id}.host"
            if hp.exists():
                hosts.add(hp.read_text().strip())
            up = node_meta_dir / f"{prefix}-task-{task_id}.uname"
            if up.exists() and not platform_line:
                lines = up.read_text().splitlines()
                if lines:
                    platform_line = lines[0].strip()
                if len(lines) > 1 and lines[1].strip().isdigit():
                    cpus = int(lines[1].strip())
    return {"host": ",".join(sorted(hosts)) if hosts else "unknown",
           "platform": platform_line or "unknown", "cpus": cpus if cpus else 4}


def per_cell_table(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per dag_phase cell: the cascade outcome plus this cell's own
    G-S2 verdict (§8's gate is pass/fail per cell, never averaged away)."""
    rows: list[dict[str, Any]] = []
    for m in manifests:
        dag = m["dag"]
        cascade = dag.get("cascade", {})
        false_safe_count = cascade.get("false_safe_count", 0)
        rows.append({
            "task_id": m.get("_task_id"), "store": m.get("dataset", {}).get("name"),
            "shape": dag.get("shape"), "depth": dag.get("depth"), "fanout": dag.get("fanout"),
            "seed": m.get("seed", {}).get("value"), "cascade_k": dag.get("cascade_k"),
            "wall_s": dag.get("wall_s"),
            "nodes_visited": cascade.get("nodes_visited"),
            "unnecessary_invalidations_count": cascade.get("unnecessary_invalidations_count"),
            "false_safe_count": false_safe_count,
            "quiescent": cascade.get("quiescent"),
            "truncated": dag.get("info", {}).get("truncated"),
            "g_s2_false_safe_zero": false_safe_count == 0,
        })
    return rows


def result_digest(manifests: list[dict[str, Any]]) -> str:
    canon = sorted(
        ({"task_id": m.get("_task_id"), "result_digest": m.get("result_digest")}
         for m in manifests),
        key=lambda r: r["task_id"])
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _csv_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records-dir", type=Path, required=True)
    ap.add_argument("--node-meta-dir", type=Path, default=None)
    ap.add_argument("--store", required=True)
    ap.add_argument("--shapes", required=True)
    ap.add_argument("--depths", required=True)
    ap.add_argument("--fanouts", required=True)
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--freeze-id", default=None)
    ap.add_argument("--freeze-sha256", default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--array-job-id", default=None)
    args = ap.parse_args(argv)

    shapes = _csv_list(args.shapes)
    depths = [int(x) for x in _csv_list(args.depths)]
    fanouts = [int(x) for x in _csv_list(args.fanouts)]
    seeds = [int(x) for x in _csv_list(args.seeds)]
    n_tasks = len(shapes) * len(depths) * len(fanouts) * len(seeds)

    manifests, tasks_meta = load_tasks(args.records_dir, n_tasks, args.store, shapes, depths,
                                       fanouts, seeds)
    machine_extra = load_node_meta(args.node_meta_dir, n_tasks, "dag")

    cells = per_cell_table(manifests)
    total_wall_s = round(sum(c["wall_s"] or 0.0 for c in cells), 2)
    g_s2_failures = [c for c in cells if not c["g_s2_false_safe_zero"]]

    record_path = f"benchmarks/storm-v1/{args.out.stem}-rows.jsonl"
    rows_path = args.out.parent / f"{args.out.stem}-rows.jsonl"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(rows_path, "w", encoding="utf-8") as f:
        for m in manifests:
            f.write(json.dumps(m, sort_keys=True) + "\n")

    manifest = {
        "schema_version": "1.0.0",
        "git_commit": args.commit,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": {"host": machine_extra["host"], "platform": machine_extra["platform"],
                   "cpus": machine_extra["cpus"], "ram_gb": 16.0},
        "config": {
            "harness": "scripts/bench_correction_storm.py (dag phase)",
            "slurm_script": "scripts/storm_campaign_dag.slurm",
            "array_job_id": args.array_job_id, "freeze_id": args.freeze_id,
            "freeze_sha256": args.freeze_sha256, "n_tasks": n_tasks,
            "store": args.store, "shapes": shapes, "depths": depths, "fanouts": fanouts,
            "seeds": seeds,
        },
        "seed": {"value": seeds[0] if seeds else 0},
        "dataset": {
            "name": "correction-storm dag_phase grid",
            "digest": hashlib.sha256(json.dumps(
                {"store": args.store, "shapes": shapes, "depths": depths, "fanouts": fanouts,
                 "seeds": seeds}, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "digest_kind": "manifest",
        },
        "result_digest": result_digest(manifests),
        "protocol": {"warmups": 0, "reps": n_tasks, "ceilings": {}},
        "record": record_path,
        "total_tasks": n_tasks, "total_wall_s": total_wall_s,
        "gates": {
            "g_s2_false_safe_zero": len(g_s2_failures) == 0,
            "g_s2_failing_cells": [c["task_id"] for c in g_s2_failures],
        },
        "per_cell": cells,
        "tasks": tasks_meta,
    }

    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"merged {n_tasks} dag cells ({total_wall_s}s) -> {args.out}")
    print(f"wrote {rows_path}")
    print(f"G-S2 (false-safe=0): {'PASS' if manifest['gates']['g_s2_false_safe_zero'] else 'FAIL'}"
         f" ({len(g_s2_failures)} failing cell(s))")
    return 1 if g_s2_failures else 0


if __name__ == "__main__":
    sys.exit(main())
