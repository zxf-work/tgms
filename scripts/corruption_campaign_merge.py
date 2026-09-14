"""Merge EXP-A3 corruption-sweep task records into one committed manifest
(Lane A task A4), and EXP-A5's disk-full campaign — same shape, one script,
selected with `--kind`.

`scripts/corruption_campaign.slurm` / `scripts/diskfull_campaign.slurm` each
run a Slurm array of `scripts/eval_corruption.py` / `scripts/
eval_diskfull.py` invocations and write one `task-<id>.json` per array task
to iTiger scratch storage (already itself a schema-conformant single-task
manifest — see both harnesses' own `build_manifest`). This script merges
those task files (scp'd down locally — never committed individually) into
one `benchmarks/corruption-v1/eval-corruption-campaign-<date>.json` or
`benchmarks/diskfull-v1/eval-diskfull-campaign-<date>.json` that also
conforms to `benchmarks/schema/result_manifest.schema.json` (validate
afterward with `scripts/check_result_manifest.py`).

Every number in the merged file is *computed from the task records*, never
hand-typed: total trial count, the corruption detection matrix (or the
disk-full fired/hang counts), and `result_digest` (sha256 over the sorted
raw trial records) all come from `results`, embedded verbatim.

    python scripts/corruption_campaign_merge.py --kind corruption \\
        --records-dir /path/to/scpd/records \\
        --node-meta-dir /path/to/scpd/node_meta \\
        --base-seed 1000 --n-tasks 40 --trials-per-task 250 \\
        --commit <sha> \\
        --out benchmarks/corruption-v1/eval-corruption-campaign-2026-09-13.json

    python scripts/corruption_campaign_merge.py --kind diskfull \\
        --records-dir /path/to/scpd/records --base-seed 1000 \\
        --n-tasks 20 --trials-per-task 100 --commit <sha> \\
        --out benchmarks/diskfull-v1/eval-diskfull-campaign-2026-09-13.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORRUPTION_QUESTION = "verdict"  # every corruption trial record's own verdict field
DISKFULL_GATE_KEYS = ("q1_acked_survive", "q2_deterministic",
                      "q3_single_generation", "q4_orphans_reclaimed")


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
        got_seed = data.get("seed", {}).get("value")
        if got_seed != want_seed:
            raise ValueError(f"task-{task_id}.json: seed {got_seed!r} != expected {want_seed}")
        task_results = data["results"]
        if len(task_results) != trials_per_task:
            raise ValueError(
                f"task-{task_id}.json: {len(task_results)} trial records, "
                f"expected {trials_per_task}")
        for r in task_results:
            r.setdefault("task_id", task_id)
        results.extend(task_results)
        tasks_meta.append({"task_id": task_id, "seed": got_seed,
                           "commit": data.get("git_commit"), "n_trials": len(task_results)})
    if missing:
        raise FileNotFoundError(f"missing task record(s), campaign is incomplete: {missing}")
    return results, tasks_meta


def load_node_meta(node_meta_dir: Path | None, n_tasks: int) -> dict[str, Any]:
    """Best-effort: host + a representative uname/nproc, from the .host/
    .uname sidecar files scp'd down alongside the task records. Absent
    entirely, the caller falls back to a placeholder — node metadata is
    provenance, not a result, and must never abort the merge."""
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
    return {"host": ",".join(sorted(hosts)) if hosts else "unknown",
           "platform": platform_line or "unknown", "cpus": cpus if cpus else 4}


def corruption_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    pair: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"trials": 0, "detected": 0})
    verdict_counts: dict[str, int] = defaultdict(int)
    benign_by_class: dict[str, set[str]] = defaultdict(set)
    for r in results:
        verdict_counts[r["verdict"]] += 1
        key = (r["class"], r["mutation"])
        pair[key]["trials"] += 1
        if r["verdict"] == "DETECTED":
            pair[key]["detected"] += 1
        if r["verdict"] == "BENIGN":
            benign_by_class[r["class"]].add(r["reason"])
    matrix = {
        f"{c}|{m}": {"trials": v["trials"], "detected": v["detected"],
                     "detection_rate": round(v["detected"] / v["trials"], 4)}
        for (c, m), v in sorted(pair.items())
    }
    return {
        "verdict_counts": dict(verdict_counts),
        "detection_matrix": matrix,
        "benign_reasons_by_class": {c: sorted(rs) for c, rs in benign_by_class.items()},
        "silent_trials": [r for r in results if r["verdict"] == "SILENT"],
    }


def diskfull_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    def ok(r: dict[str, Any]) -> bool:
        return bool(r.get("clean_error")) and not r.get("hang") and all(
            r.get(k, False) for k in DISKFULL_GATE_KEYS)

    bad = [r for r in results if not ok(r)]
    hangs = [r for r in results if r.get("hang")]
    by_mode: dict[str, dict[str, int]] = defaultdict(lambda: {"trials": 0, "fired": 0})
    for r in results:
        by_mode[r["mode"]]["trials"] += 1
        if r.get("fault_fired"):
            by_mode[r["mode"]]["fired"] += 1
    return {
        "total_problems": len(bad),
        "total_hangs": len(hangs),
        "n_fault_fired": sum(1 for r in results if r.get("fault_fired")),
        "by_mode": dict(by_mode),
        "problem_trials": bad[:20],
    }


def result_digest(results: list[dict[str, Any]], kind: str) -> str:
    key_fn = ((lambda r: (r["class"], r["mutation"], r.get("task_id", -1), r["trial"]))
             if kind == "corruption" else
             (lambda r: (r["mode"], r.get("task_id", -1), r["trial"])))
    canon = sorted(results, key=key_fn)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dataset_digest(kind: str, base_seed: int, n_tasks: int, trials_per_task: int) -> str:
    """A digest of the *recipe* that generates the per-trial synthetic
    workloads — there is no fixed input dataset to hash, each trial derives
    its own seed deterministically from (seed, trial) — so a later run can
    confirm it used the same generative parameters."""
    recipe = {"kind": kind, "base_seed": base_seed, "n_tasks": n_tasks,
             "trials_per_task": trials_per_task}
    blob = json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=["corruption", "diskfull"], required=True)
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

    results, tasks_meta = load_tasks(args.records_dir, args.n_tasks, args.base_seed,
                                     args.trials_per_task)
    machine_extra = load_node_meta(args.node_meta_dir, args.n_tasks)
    total = len(results)

    if args.kind == "corruption":
        stats = corruption_stats(results)
        bad_count = stats["verdict_counts"].get("SILENT", 0)
        harness, slurm_script = "scripts/eval_corruption.py", "scripts/corruption_campaign.slurm"
        subdir = "corruption-v1"
    else:
        stats = diskfull_stats(results)
        bad_count = stats["total_problems"]
        harness, slurm_script = "scripts/eval_diskfull.py", "scripts/diskfull_campaign.slurm"
        subdir = "diskfull-v1"

    wall_s = round(sum(r.get("wall_s", 0.0) for r in results), 2)
    record_path = f"benchmarks/{subdir}/{args.out.name}"
    manifest = {
        "schema_version": "1.0.0",
        "git_commit": args.commit,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": {"host": machine_extra["host"], "platform": machine_extra["platform"],
                   "cpus": machine_extra["cpus"], "ram_gb": args.ram_gb},
        "config": {
            "harness": harness, "slurm_script": slurm_script,
            "array_job_id": args.array_job_id, "n_tasks": args.n_tasks,
            "trials_per_task": args.trials_per_task, "concurrency": args.concurrency,
            "wall_per_task_s": args.wall_per_task_s,
            "seeds_by_task": {t["task_id"]: t["seed"] for t in tasks_meta},
            "tmpdir": "node-local /tmp (not /project — NFS4 sillyrename broke "
                     "tempfile cleanup in the crash-v1 campaign; see "
                     "benchmarks/crash-v1/README.md, same fix reused here)",
            "commit_resolution": "git shim answering TGMS_COMMIT — no git binary "
                                 "on iTiger compute nodes",
        },
        "seed": {"value": args.base_seed},
        "dataset": {"name": "seeded synthetic workload",
                   "digest": dataset_digest(args.kind, args.base_seed, args.n_tasks,
                                            args.trials_per_task),
                   "digest_kind": "manifest"},
        "result_digest": result_digest(results, args.kind),
        "protocol": {"warmups": 0, "reps": total,
                    "ceilings": {"wall_per_task_s": args.wall_per_task_s,
                                "array_concurrency": args.concurrency}},
        "record": record_path,
        "total_trials": total,
        "total_problems": bad_count,
        "wall_s": wall_s,
        "stats": stats,
        "tasks": tasks_meta,
        "results": results,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"merged {total} trials ({bad_count} with problems) -> {args.out}")
    if args.kind == "corruption":
        for pair_key, s in stats["detection_matrix"].items():
            print(f"  {pair_key:40s}: {s['trials']} trials, "
                 f"detection_rate={s['detection_rate']}")
        if stats["verdict_counts"].get("SILENT", 0):
            print(f"  *** {stats['verdict_counts']['SILENT']} SILENT trials — see "
                 f"stats.silent_trials in {args.out} ***")
    else:
        for mode, s in stats["by_mode"].items():
            print(f"  mode={mode:12s}: {s['trials']} trials, {s['fired']} fired")
        if stats["total_hangs"]:
            print(f"  *** {stats['total_hangs']} HANGS — see stats.problem_trials "
                 f"in {args.out} ***")
    return 1 if bad_count else 0


if __name__ == "__main__":
    sys.exit(main())
