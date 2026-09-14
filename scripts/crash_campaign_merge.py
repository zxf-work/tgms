"""Merge EXP-A1 crash-campaign task records into one committed manifest.

`scripts/crash_campaign.slurm` runs a Slurm array of `scripts/
eval_durability.py` invocations (P0.7's seeded crash/recovery harness) and
writes one `task-<id>.json` per array task to iTiger's scratch storage. This
script merges those task files (scp'd down locally — never committed
individually) into a single `benchmarks/crash-v1/eval-crash-campaign-
<date>.json` that conforms to `benchmarks/schema/result_manifest.schema.json`
(validate afterward with `scripts/check_result_manifest.py`).

Every number in the merged file's manifest is *computed from the task
records*, never hand-typed: per-boundary trial/problem counts, the total
trial count, total wall time, and `result_digest` (sha256 over the sorted
raw trial records) all come from `results`, which this script also embeds
verbatim (the `record` field self-references the output file — the raw
per-trial rows live in the same file, not a separate one).

    python scripts/crash_campaign_merge.py \\
        --records-dir /path/to/scpd/records \\
        --node-meta-dir /path/to/scpd/node_meta \\
        --base-seed 1000 --n-tasks 20 --trials-per-task 50 \\
        --commit cb0e6af \\
        --out benchmarks/crash-v1/eval-crash-campaign-2026-09-13.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUESTIONS = ("q1_acked_survive", "q2_deterministic",
             "q3_single_generation", "q4_orphans_reclaimed")

BOUNDARIES = [
    "py_torn_wal_append", "py_after_wal_fsync", "py_before_engine_commit",
    "after_seal", "after_close_runs", "after_dict", "after_manifest",
    "after_current", "compact_before_install", "gc_mid_delete",
]


def load_tasks(records_dir: Path, n_tasks: int, base_seed: int,
               trials_per_task: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (all_result_records, per_task_meta). Raises on any missing
    task file, seed mismatch, or short trial count — a partial campaign
    must not silently merge as if it were complete."""
    results: list[dict[str, Any]] = []
    tasks_meta: list[dict[str, Any]] = []
    missing: list[int] = []
    for task_id in range(n_tasks):
        path = records_dir / f"task-{task_id}.json"
        if not path.exists():
            missing.append(task_id)
            continue
        data = json.loads(path.read_text())
        want_seed = base_seed + task_id
        got_seed = data.get("manifest", {}).get("seed")
        if got_seed != want_seed:
            raise ValueError(
                f"task-{task_id}.json: seed {got_seed!r} != expected {want_seed}")
        task_results = data["results"]
        want_n = trials_per_task * len(BOUNDARIES)
        if len(task_results) != want_n:
            raise ValueError(
                f"task-{task_id}.json: {len(task_results)} trial records, "
                f"expected {want_n} ({trials_per_task} x {len(BOUNDARIES)} boundaries)")
        for r in task_results:
            r.setdefault("task_id", task_id)
        results.extend(task_results)
        tasks_meta.append({
            "task_id": task_id,
            "seed": got_seed,
            "commit": data.get("manifest", {}).get("commit"),
            "n_trials": len(task_results),
        })
    if missing:
        raise FileNotFoundError(
            f"missing task record(s), campaign is incomplete: {missing}")
    return results, tasks_meta


def load_node_meta(node_meta_dir: Path | None, n_tasks: int) -> dict[str, Any]:
    """Best-effort: host + a representative uname/nproc, from the .host/
    .uname sidecar files scp'd down alongside the task records. Absent
    entirely, the caller falls back to a placeholder — this must never
    abort the merge, since node metadata is provenance, not a result."""
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


def per_boundary_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for b in BOUNDARIES:
        rows = [r for r in results if r["boundary"] == b]
        entry = {"trials": len(rows), "problems_total": 0}
        for q in QUESTIONS:
            entry[f"failed_{q}"] = sum(1 for r in rows if not r.get(q, False))
        entry["problems_total"] = sum(1 for r in rows if r.get("problems"))
        entry["sample_problems"] = [
            p for r in rows for p in r.get("problems", [])
        ][:5]
        stats[b] = entry
    return stats


def result_digest(results: list[dict[str, Any]]) -> str:
    canon = sorted(
        results,
        key=lambda r: (r["boundary"], r.get("task_id", -1), r["trial"]),
    )
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dataset_digest(base_seed: int, n_tasks: int, trials_per_task: int) -> str:
    """A digest of the *recipe* that generates the per-trial synthetic
    workloads (there is no fixed input dataset to hash — each trial's
    workload is derived deterministically from (seed, boundary, trial), see
    derive_trial_seed in scripts/eval_durability.py), so a later run can
    confirm it used the same generative parameters."""
    recipe = {
        "base_seed": base_seed, "n_tasks": n_tasks,
        "trials_per_task": trials_per_task, "boundaries": BOUNDARIES,
    }
    blob = json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records-dir", type=Path, required=True)
    ap.add_argument("--node-meta-dir", type=Path, default=None)
    ap.add_argument("--base-seed", type=int, required=True)
    ap.add_argument("--n-tasks", type=int, required=True)
    ap.add_argument("--trials-per-task", type=int, required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--array-job-id", default=None,
                     help="Slurm array job id, recorded in config for provenance")
    ap.add_argument("--ram-gb", type=float, default=8.0)
    ap.add_argument("--wall-per-task-s", type=int, default=4 * 3600)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args(argv)

    results, tasks_meta = load_tasks(
        args.records_dir, args.n_tasks, args.base_seed, args.trials_per_task)
    machine_extra = load_node_meta(args.node_meta_dir, args.n_tasks)

    total = len(results)
    bad = [r for r in results if not all(r.get(q, False) for q in QUESTIONS)]
    boundary_stats = per_boundary_stats(results)
    wall_s = round(sum(r.get("wall_s", 0.0) for r in results), 2)

    record_path = f"benchmarks/crash-v1/{args.out.name}"
    manifest = {
        "schema_version": "1.0.0",
        "git_commit": args.commit,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": {
            "host": machine_extra["host"],
            "platform": machine_extra["platform"],
            "cpus": machine_extra["cpus"],
            "ram_gb": args.ram_gb,
        },
        "config": {
            "harness": "scripts/eval_durability.py",
            "slurm_script": "scripts/crash_campaign.slurm",
            "array_job_id": args.array_job_id,
            "n_tasks": args.n_tasks,
            "trials_per_task": args.trials_per_task,
            "boundaries": BOUNDARIES,
            "concurrency": args.concurrency,
            "wall_per_task_s": args.wall_per_task_s,
            "seeds_by_task": {t["task_id"]: t["seed"] for t in tasks_meta},
            "tmpdir": "node-local /tmp (not /project — NFS4 sillyrename "
                      "broke tempfile cleanup; see benchmarks/crash-v1/README.md)",
            "commit_resolution": "git shim answering TGMS_COMMIT — no git "
                                 "binary on iTiger compute nodes",
        },
        "seed": {"value": args.base_seed},
        "dataset": {
            "name": "seeded synthetic workload",
            "digest": dataset_digest(args.base_seed, args.n_tasks, args.trials_per_task),
            "digest_kind": "manifest",
        },
        "result_digest": result_digest(results),
        "protocol": {
            "warmups": 0,
            "reps": total,
            "ceilings": {
                "child_timeout_s": 120,
                "wall_per_task_s": args.wall_per_task_s,
                "array_concurrency": args.concurrency,
            },
        },
        "record": record_path,
        "total_trials": total,
        "total_problems": len(bad),
        "wall_s": wall_s,
        "per_boundary": boundary_stats,
        "tasks": tasks_meta,
        "results": results,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"merged {total} trials ({len(bad)} with problems) -> {args.out}")
    for b, s in boundary_stats.items():
        print(f"  {b:>24}: {s['trials']} trials, {s['problems_total']} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
