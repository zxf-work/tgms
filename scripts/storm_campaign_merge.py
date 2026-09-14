"""Merge Correction Storm campaign task records into one committed manifest
(Lane C, tasks C2/C5; `docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`
§7-8).

`scripts/storm_campaign.slurm` runs a Slurm array of `scripts/
bench_correction_storm.py` invocations, one per (store x mix x age x
n_artifacts x seed) cell, each writing its own `storm-<store>-<seed>.json`
manifest (+ a `-rows.jsonl` per-batch sidecar, never merged — see module
note below) into its own `task-<id>/` directory under iTiger's scratch
storage. This script merges those per-task manifests (scp'd down locally —
never committed individually, the same discipline `crash_campaign_merge.py`
follows) into a single `benchmarks/storm-v1/storm-campaign-<date>.json`
conforming to `benchmarks/schema/result_manifest.schema.json` (validate
afterward with `scripts/check_result_manifest.py`), with a per-cell table
(`per_cell`) and the campaign's own G-S1/G-S2 gate verdicts computed
directly from what each task actually reported.

**Why per-task manifests are embedded whole, and per-batch rows are not.**
Each task's own manifest (`config`, `summary`, `dag`) is small (one row per
arm, not per batch) and is embedded verbatim into the merged file's
sidecar `<out>-rows.jsonl` (`manifest["record"]` names it) — the same
"embeds verbatim" choice `crash_campaign_merge.py` makes for its own
per-trial rows. Each task's `-rows.jsonl` (one line per correction batch,
up to `--batches` long) stays on iTiger's stage directory; merging
thousands-of-batches-per-cell raw rows into one committed JSON file across
a whole campaign would make the merged artifact itself the next storage
problem the design memo's own §3 registry-growth discussion warns about.

    python scripts/storm_campaign_merge.py \\
        --records-dir /path/to/scpd/records \\
        --node-meta-dir /path/to/scpd/node_meta \\
        --stores synth-iv-60k,collegemsg --mixes c1,c2,c3,c4 \\
        --ages none,recent,hours,days,deep --n-artifacts-list 1000 \\
        --n-seeds 5 --base-seed 0 --commit 4af2181 \\
        --out benchmarks/storm-v1/storm-campaign-2026-09-13.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: G-S1 (design memo §8): false-fresh = 0 in every cell, for both `tgms-*`
#: arms. G-S2: false-safe = 0 over every propagation/cascade decision — only
#: checked in cells that actually ran the DAG phase (`manifest["dag"]` is
#: not `None`).
TGMS_ARMS = ("tgms-L0", "tgms-L1")


def _expected_cell(task_id: int, stores: list[str], mixes: list[str], ages: list[str],
                   n_list: list[int], n_seeds: int, base_seed: int,
                   ttf_modes: list[str] | None = None) -> dict[str, Any]:
    """The same in-script index decomposition `storm_campaign.slurm` uses
    (seed varies fastest, then n_artifacts, then age, then mix, then store,
    then ttf mode slowest) — restated here so a merge can cross-check that
    the records it was handed are the cells it thinks they are, not
    silently shuffled.

    `ttf_modes` is additive (storm-v1 C6 freeze: the grid scores both TTF
    modes per cell, `storm_campaign.slurm`'s own added outermost axis) —
    default `["sum"]` keeps a pre-freeze single-mode campaign's task
    numbering unchanged."""
    modes = ttf_modes or ["sum"]
    rem = task_id
    seed_idx = rem % n_seeds
    rem //= n_seeds
    n_idx = rem % len(n_list)
    rem //= len(n_list)
    age_idx = rem % len(ages)
    rem //= len(ages)
    mix_idx = rem % len(mixes)
    rem //= len(mixes)
    store_idx = rem % len(stores)
    rem //= len(stores)
    ttf_idx = rem % len(modes)
    return {
        "store": stores[store_idx], "mix": mixes[mix_idx], "age": ages[age_idx],
        "n_artifacts": n_list[n_idx], "seed": base_seed + seed_idx,
        "measure_ttf": modes[ttf_idx],
    }


def load_tasks(records_dir: Path, n_tasks: int, stores: list[str], mixes: list[str],
               ages: list[str], n_list: list[int], n_seeds: int, base_seed: int,
               ttf_modes: list[str] | None = None,
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (task_manifests, per_task_meta). Raises on any missing task
    directory/manifest or a cell mismatch — a partial or reshuffled
    campaign must not silently merge as if it were complete and correctly
    ordered."""
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
        expected = _expected_cell(task_id, stores, mixes, ages, n_list, n_seeds, base_seed,
                                  ttf_modes)
        cfg = manifest.get("config", {})
        got = {
            "store": manifest.get("dataset", {}).get("name"),
            "mix": cfg.get("mix") or "c1", "age": cfg.get("age") or "none",
            "n_artifacts": cfg.get("n_artifacts"), "seed": manifest.get("seed", {}).get("value"),
            "measure_ttf": cfg.get("measure_ttf") or "sum",
        }
        # `dataset.name` is the store's directory basename; `--store` may
        # have named a path -- compare basenames, not the raw strings.
        if Path(expected["store"]).name != got["store"]:
            raise ValueError(f"task-{task_id}: store {got['store']!r} != expected "
                             f"{expected['store']!r}")
        for key in ("mix", "age", "n_artifacts", "seed", "measure_ttf"):
            if got[key] != expected[key]:
                raise ValueError(f"task-{task_id}: {key} {got[key]!r} != expected "
                                 f"{expected[key]!r} (cell {expected})")
        manifest.setdefault("_task_id", task_id)
        manifests.append(manifest)
        tasks_meta.append({
            "task_id": task_id, **expected,
            "n_registered": cfg.get("n_registered"), "batches_realized": manifest.get(
                "summary", {}).get("batches"),
            "wall_s": cfg.get("wall_s"),
        })
    if missing:
        raise FileNotFoundError(
            f"missing task record(s), campaign is incomplete: {missing}")
    return manifests, tasks_meta


def load_node_meta(node_meta_dir: Path | None, n_tasks: int) -> dict[str, Any]:
    """Best-effort, restated from `crash_campaign_merge.py::load_node_meta`
    verbatim — node metadata is provenance, never a result, and must never
    abort the merge."""
    hosts: set[str] = set()
    platform_line = ""
    cpus = None
    if node_meta_dir and node_meta_dir.is_dir():
        for task_id in range(n_tasks):
            hp = node_meta_dir / f"task-{task_id}.host"
            if hp.exists():
                hosts.add(hp.read_text().strip())
            up = node_meta_dir / f"task-{task_id}.uname"
            if up.exists() and not platform_line:
                lines = up.read_text().splitlines()
                if lines:
                    platform_line = lines[0].strip()
                if len(lines) > 1 and lines[1].strip().isdigit():
                    cpus = int(lines[1].strip())
    return {
        "host": ",".join(sorted(hosts)) if hosts else "unknown",
        "platform": platform_line or "unknown",
        "cpus": cpus if cpus else 4,
    }


def per_cell_table(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per task/cell: the arm summary table plus the DAG/cascade
    outcome (if that task ran one), and this cell's own G-S1/G-S2 verdicts —
    §8's gates are pass/fail *per cell*, never averaged away."""
    rows: list[dict[str, Any]] = []
    for m in manifests:
        cfg = m.get("config", {})
        summary = m.get("summary", {})
        arms = summary.get("arms", {})
        g_s1 = all(arms.get(arm, {}).get("false_fresh", 0) == 0 for arm in TGMS_ARMS
                  if arm in arms)
        dag = m.get("dag")
        g_s2 = True if dag is None else dag.get("cascade", {}).get("false_safe_count", 0) == 0
        rows.append({
            "task_id": m.get("_task_id"), "store": m.get("dataset", {}).get("name"),
            "mix": cfg.get("mix") or "c1", "age": cfg.get("age") or "none",
            "n_artifacts": cfg.get("n_artifacts"), "seed": m.get("seed", {}).get("value"),
            "measure_ttf": cfg.get("measure_ttf") or "sum",
            "batches_realized": summary.get("batches"), "wall_s": cfg.get("wall_s"),
            "g_s1_false_fresh_zero": g_s1, "g_s2_false_safe_zero": g_s2,
            "arms": {arm: {
                "false_fresh": row.get("false_fresh"), "false_stale": row.get("false_stale"),
                "avoided_recompute_decision": row.get("avoided_recompute_decision"),
                "avoided_recompute_wall": row.get("avoided_recompute_wall"),
                "ttf_p50_ms": row.get("ttf_p50_ms"), "ttf_p95_ms": row.get("ttf_p95_ms"),
            } for arm, row in arms.items()},
            "dag": None if dag is None else {
                "shape": dag.get("shape"), "depth": dag.get("depth"),
                "fanout": dag.get("fanout"), "cascade_k": dag.get("cascade_k"),
                "nodes_visited": dag.get("cascade", {}).get("nodes_visited"),
                "unnecessary_invalidations_count": dag.get("cascade", {}).get(
                    "unnecessary_invalidations_count"),
                "false_safe_count": dag.get("cascade", {}).get("false_safe_count"),
                "quiescent": dag.get("cascade", {}).get("quiescent"),
            },
        })
    return rows


def result_digest(manifests: list[dict[str, Any]]) -> str:
    """A digest over every task's own `result_digest`, sorted by cell — this
    campaign never re-hashes the raw per-batch rows (module note: those
    stay on iTiger's stage directory), so this is the digest of *what each
    task already claimed*, chained together."""
    canon = sorted(
        ({"task_id": m.get("_task_id"), "result_digest": m.get("result_digest")}
         for m in manifests),
        key=lambda r: r["task_id"])
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dataset_digest(stores: list[str], mixes: list[str], ages: list[str], n_list: list[int],
                   n_seeds: int, base_seed: int, ttf_modes: list[str]) -> str:
    """A digest of the campaign *recipe* (the grid), restated from
    `crash_campaign_merge.py::dataset_digest`'s own reasoning: there is no
    single fixed input dataset to hash across a multi-store campaign, only
    the generative parameters."""
    recipe = {"stores": stores, "mixes": mixes, "ages": ages, "n_artifacts_list": n_list,
             "n_seeds": n_seeds, "base_seed": base_seed, "ttf_modes": ttf_modes}
    blob = json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _csv_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records-dir", type=Path, required=True)
    ap.add_argument("--node-meta-dir", type=Path, default=None)
    ap.add_argument("--stores", required=True, help="comma-separated, same order as the "
                    "slurm script's TGMS_STORES")
    ap.add_argument("--mixes", required=True)
    ap.add_argument("--ages", required=True)
    ap.add_argument("--n-artifacts-list", required=True)
    ap.add_argument("--n-seeds", type=int, required=True)
    ap.add_argument("--base-seed", type=int, default=0)
    ap.add_argument("--ttf-modes", default="sum",
                    help="comma-separated, same order/outermost-axis convention as "
                         "storm_campaign.slurm's TGMS_MEASURE_TTF_LIST; default 'sum' "
                         "matches a pre-freeze single-mode campaign")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--array-job-id", default=None,
                    help="Slurm array job id, recorded in config for provenance")
    ap.add_argument("--freeze-id", default=None,
                    help="campaign.yaml's freeze_id (storm-v1 C6 freeze), stamped into "
                         "config for provenance")
    ap.add_argument("--freeze-sha256", default=None,
                    help="campaign.yaml's own sha256, stamped into config so this record "
                         "names exactly which frozen grid it ran")
    ap.add_argument("--ram-gb", type=float, default=16.0)
    ap.add_argument("--wall-per-task-s", type=int, default=4 * 3600)
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args(argv)

    stores = _csv_list(args.stores)
    mixes = _csv_list(args.mixes)
    ages = _csv_list(args.ages)
    n_list = [int(x) for x in _csv_list(args.n_artifacts_list)]
    ttf_modes = _csv_list(args.ttf_modes)
    n_tasks = len(stores) * len(mixes) * len(ages) * len(n_list) * args.n_seeds * len(ttf_modes)

    manifests, tasks_meta = load_tasks(args.records_dir, n_tasks, stores, mixes, ages,
                                       n_list, args.n_seeds, args.base_seed, ttf_modes)
    machine_extra = load_node_meta(args.node_meta_dir, n_tasks)

    cells = per_cell_table(manifests)
    total_batches = sum(c["batches_realized"] or 0 for c in cells)
    total_wall_s = round(sum(c["wall_s"] or 0.0 for c in cells), 2)
    g_s1_failures = [c for c in cells if not c["g_s1_false_fresh_zero"]]
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
        "machine": {
            "host": machine_extra["host"], "platform": machine_extra["platform"],
            "cpus": machine_extra["cpus"], "ram_gb": args.ram_gb,
        },
        "config": {
            "harness": "scripts/bench_correction_storm.py",
            "slurm_script": "scripts/storm_campaign.slurm",
            "array_job_id": args.array_job_id, "freeze_id": args.freeze_id,
            "freeze_sha256": args.freeze_sha256, "n_tasks": n_tasks,
            "stores": stores, "mixes": mixes, "ages": ages, "n_artifacts_list": n_list,
            "n_seeds": args.n_seeds, "base_seed": args.base_seed, "ttf_modes": ttf_modes,
            "concurrency": args.concurrency, "wall_per_task_s": args.wall_per_task_s,
        },
        "seed": {"value": args.base_seed},
        "dataset": {
            "name": "correction-storm campaign grid",
            "digest": dataset_digest(stores, mixes, ages, n_list, args.n_seeds, args.base_seed,
                                     ttf_modes),
            "digest_kind": "manifest",
        },
        "result_digest": result_digest(manifests),
        "protocol": {
            "warmups": 0, "reps": total_batches,
            "ceilings": {"wall_per_task_s": args.wall_per_task_s,
                        "array_concurrency": args.concurrency},
        },
        "record": record_path,
        "total_tasks": n_tasks, "total_batches": total_batches, "total_wall_s": total_wall_s,
        "gates": {
            "g_s1_false_fresh_zero": len(g_s1_failures) == 0,
            "g_s1_failing_cells": [c["task_id"] for c in g_s1_failures],
            "g_s2_false_safe_zero": len(g_s2_failures) == 0,
            "g_s2_failing_cells": [c["task_id"] for c in g_s2_failures],
        },
        "per_cell": cells,
        "tasks": tasks_meta,
    }

    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"merged {n_tasks} cells ({total_batches} batches, {total_wall_s}s) -> {args.out}")
    print(f"wrote {rows_path}")
    print(f"G-S1 (false-fresh=0): {'PASS' if manifest['gates']['g_s1_false_fresh_zero'] else 'FAIL'}"
         f" ({len(g_s1_failures)} failing cell(s))")
    print(f"G-S2 (false-safe=0):  {'PASS' if manifest['gates']['g_s2_false_safe_zero'] else 'FAIL'}"
         f" ({len(g_s2_failures)} failing cell(s))")
    return 1 if (g_s1_failures or g_s2_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
