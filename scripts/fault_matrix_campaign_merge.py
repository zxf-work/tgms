"""Merge the trust-boundary fault-matrix campaign's task records into one
committed manifest (Lane E, task E-campaign).

`scripts/fault_matrix_campaign.slurm` runs a 40-task Slurm array of
`scripts/eval_trust_boundary_matrix.py` invocations -- one (cell, surface,
gate) triple per task -- and writes one `task-<id>--<cell>-<suite>
[-strict].json` raw record per array task to iTiger's scratch storage. This
script never re-derives a number from scratch: every count in the merged
manifest and in `benchmarks/faults-v1/README.md` is read back out of the 40
raw records, which are committed alongside it unmodified (the same
"companion raw files" pattern `benchmarks/crash-v1/eval-crash-campaign-
2026-09-13.json` uses for `scripts/crash_campaign_merge.py`).

    python scripts/fault_matrix_campaign_merge.py \\
        --records-dir <scp'd>/records --node-meta-dir <scp'd>/node_meta \\
        --commit 4af2181 --array-job-id 211007 \\
        --out benchmarks/faults-v1/fault-matrix-campaign-2026-09-13.json \\
        --raw-out-dir benchmarks/faults-v1

Every `task-<id>--*.json` file under `--records-dir` is one raw
`eval_trust_boundary_matrix.py` manifest (`build_manifest` in that script);
this tool never touches their `per_case` contents, only reads them to build
per-cell and campaign-wide tables. Silent-violation trials are surfaced
verbatim (never summarized away): the merged manifest's
`silent_violations` section lists, per (cell, gate), every trial's index in
that raw file's `per_case` array plus its `task_id` when the record shape
carries one (F1-9's `run_answer_level_cell` trials do not -- see that raw
file's own `per_case` for the underlying rows).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_FILE_RE = re.compile(r"^task-(\d+)--(.+)\.json$")

OUTCOME_KEYS = ("correct", "safe-refusal", "explicit-failure", "silent-violation")

#: Coordinator Addendum 1 (2026-09-13): --strict-gate is a documented no-op
#: for these cells (`scripts/eval_trust_boundary_matrix.py`'s own module
#: docstring), so the campaign never ran a strict variant for them.
STRICT_NOOP_CELLS = frozenset({"F1-8", "F2-5"})

#: The eight cells §5 pre-registers at N=300 (coordinator Addendum 1 names
#: them explicitly); everything else is N=100.
EIGHT_EXISTING_MACHINERY_CELLS = frozenset({
    "F1-7", "F1-8", "F1-9", "F1-10", "F1-11", "F2-1", "F2-4", "F2-6",
})

#: F2-3 and F1-6's plain (non-oracled) top-level cell id are declared
#: non-headline by the frozen design (§3 for F2-3: "outside the trust
#: model, assumption A2"; CELL_REGISTRY["F2-3"].headline is False) --
#: excluded from the aggregate headline silent-violation count, but never
#: from the per-cell table or the verbatim trial-id listing.
NON_HEADLINE_CELLS = frozenset({"F2-3", "F1-3b", "F1-6"})


def parse_records_dir(records_dir: Path) -> list[dict[str, Any]]:
    """Load every task-<id>--<cell>-<suite>[-strict].json file, tagging each
    with its Slurm array task id and raw filename. Raises if any file fails
    to parse or is missing required keys -- a partial campaign must not
    silently merge as if it were complete."""
    out = []
    files = sorted(records_dir.glob("task-*.json"))
    if not files:
        raise FileNotFoundError(f"no task-*.json records found under {records_dir}")
    for path in files:
        m = TASK_FILE_RE.match(path.name)
        if not m:
            raise ValueError(f"unrecognized record filename: {path.name}")
        task_id = int(m.group(1))
        data = json.loads(path.read_text())
        for key in ("cell", "surface", "n_cases", "n_requested", "outcomes",
                    "strict_gate", "seed", "git_commit", "machine", "per_case",
                    "result_digest", "dataset"):
            if key not in data:
                raise ValueError(f"{path.name}: missing required field {key!r}")
        # The raw manifest's own "surface" and "dataset.name" fields come
        # from CELL_REGISTRY's *static* declaration (e.g. "A/B" for every
        # F1-1 record, regardless of which one actually ran) and from the
        # literal --suite argument (which is a required-but-unused
        # placeholder for surface-B invocations -- see the .slurm script's
        # own comments) -- neither says which surface *this* record is.
        # The renamed filename does: this campaign always renames
        # surface-B output to the "<cell>-ldbc-fixture[-strict].json"
        # convention benchmarks/faults-v1/README.md documents, so recover
        # the effective (suite, surface) from the filename, not the raw
        # record's own (accurate-but-ambiguous) fields.
        cell = data["cell"]
        remainder = m.group(2)  # "<cell>-<suite>[-strict]"
        assert remainder.startswith(cell + "-"), (
            f"{path.name}: cell {cell!r} does not prefix {remainder!r}")
        suite_part = remainder[len(cell) + 1:]
        if suite_part.endswith("-strict"):
            suite_part = suite_part[: -len("-strict")]
        effective_surface = "B" if suite_part == "ldbc-fixture" else "A"
        out.append({"task_id": task_id, "raw_filename": path.name,
                    "raw_path": path, "effective_suite": suite_part,
                    "effective_surface": effective_surface, **data})
    return out


def load_node_meta(node_meta_dir: Path | None, task_ids: list[int]) -> dict[str, Any]:
    hosts: set[str] = set()
    platform_line = ""
    cpus = None
    if node_meta_dir and node_meta_dir.is_dir():
        for tid in task_ids:
            hp = node_meta_dir / f"task-{tid}.host"
            if hp.exists():
                hosts.add(hp.read_text().strip())
            up = node_meta_dir / f"task-{tid}.uname"
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


def cell_table(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per raw record: the per-(cell, surface, gate) outcome table
    the README renders verbatim."""
    rows = []
    for r in records:
        counts = r["outcomes"]["counts"]
        rows.append({
            "cell": r["cell"],
            "surface": r["effective_surface"],
            "strict_gate": r["strict_gate"],
            "suite_or_plans": r["effective_suite"],
            "raw_file": r["raw_filename"],
            "n_requested": r["n_requested"],
            "n_cases": r["n_cases"],
            "counts": {k: counts.get(k, 0) for k in OUTCOME_KEYS},
            "gold_mismatch": r["outcomes"].get("gold_mismatch"),
            "misattributed": r["outcomes"].get("misattributed"),
            "weak_support": r["outcomes"].get("weak_support"),
            "false_refusal_rate": r.get("false_refusal_rate"),
            "result_digest": r["result_digest"],
        })
    rows.sort(key=lambda x: (x["cell"], x["strict_gate"], x["surface"]))
    return rows


def silent_violations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every silent-violation trial, verbatim, across every (cell, gate) --
    reported unconditionally per the campaign's own rule: any silent-
    violation count > 0 in the primary arm is never summarized away or
    rerun to make it disappear. Non-headline cells (F2-3: assumption-A2,
    excluded by CELL_REGISTRY; F1-3b/F1-6: no oracle for this channel, §2)
    are included here too, just tagged `headline: false`."""
    out = []
    for r in records:
        hits = [(i, c) for i, c in enumerate(r["per_case"])
                if c.get("outcome") == "silent-violation"]
        if not hits:
            continue
        out.append({
            "cell": r["cell"],
            "surface": r["effective_surface"],
            "strict_gate": r["strict_gate"],
            "raw_file": r["raw_filename"],
            "headline": r["cell"] not in NON_HEADLINE_CELLS,
            "count": len(hits),
            "n_cases": r["n_cases"],
            "trials": [
                {"per_case_index": i, "task_id": c.get("task_id"),
                 "reason": c.get("reason"), "invariants_violated": c.get("invariants_violated"),
                 "mutator": c.get("mutator")}
                for i, c in hits
            ],
        })
    out.sort(key=lambda x: (not x["headline"], x["cell"], x["strict_gate"]))
    return out


def campaign_result_digest(records: list[dict[str, Any]]) -> str:
    """sha256 over the sorted list of each raw record's own result_digest
    plus its (cell, surface, strict_gate) key -- a digest of the campaign's
    membership and every constituent record's content, without re-hashing
    ~7,000 individual trial rows a second time."""
    canon = sorted(
        ({"cell": r["cell"], "surface": r["surface"], "strict_gate": r["strict_gate"],
          "result_digest": r["result_digest"]} for r in records),
        key=lambda x: (x["cell"], x["strict_gate"], x["surface"]),
    )
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dataset_digest(records: list[dict[str, Any]]) -> str:
    digests = sorted({r["dataset"].get("digest") for r in records if r["dataset"].get("digest")})
    blob = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records-dir", type=Path, required=True)
    ap.add_argument("--node-meta-dir", type=Path, default=None)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--raw-out-dir", type=Path, required=True,
                    help="directory to copy the 40 raw per-(cell,gate) "
                         "records into (benchmarks/faults-v1/)")
    ap.add_argument("--array-job-id", default=None)
    ap.add_argument("--ram-gb", type=float, default=16.0)
    ap.add_argument("--wall-per-task-s", type=int, default=1800)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--base-seed", type=int, default=0)
    args = ap.parse_args(argv)

    records = parse_records_dir(args.records_dir)
    bad_commit = [r["raw_filename"] for r in records if r["git_commit"] != args.commit]
    if bad_commit:
        raise ValueError(f"records not at commit {args.commit}: {bad_commit}")

    task_ids = [r["task_id"] for r in records]
    machine_extra = load_node_meta(args.node_meta_dir, task_ids)

    rows = cell_table(records)
    viol = silent_violations(records)
    headline_viol = [v for v in viol if v["headline"]]

    total_trials = sum(r["n_cases"] for r in records)
    total_requested = sum(r["n_requested"] for r in records)
    shortfalls = [{"cell": r["cell"], "surface": r["surface"], "strict_gate": r["strict_gate"],
                  "n_requested": r["n_requested"], "n_cases": r["n_cases"],
                  "raw_file": r["raw_filename"]}
                 for r in records if r["n_cases"] < r["n_requested"]]

    args.raw_out_dir.mkdir(parents=True, exist_ok=True)
    for r in records:
        dest = args.raw_out_dir / r["raw_filename"].split("--", 1)[1]
        dest.write_text(r["raw_path"].read_text())

    record_path = f"benchmarks/faults-v1/{args.out.name}"
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
            "harness": "scripts/eval_trust_boundary_matrix.py",
            "slurm_script": "scripts/fault_matrix_campaign.slurm",
            "array_job_id": args.array_job_id,
            "n_tasks": len(records),
            "concurrency": args.concurrency,
            "wall_per_task_s": args.wall_per_task_s,
            "design_doc": "docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md",
            "addendum": "Addendum 1 (2026-09-13, coordinator): primary arm "
                        "gates unsupported only (matches "
                        "tgms.eval.harness.run_task_ours); --strict-gate is "
                        "the named secondary arm, dropping unsupported + "
                        "unverifiable.",
            "tmpdir": "node-local /tmp (not /project); the store each task "
                      "reads is also copied node-local before the run -- "
                      "tgms.store.Store takes an OS-level single-writer "
                      "flock even for a read-only workload, and up to 6 "
                      "array tasks run concurrently against what would "
                      "otherwise be one shared store (see the .slurm "
                      "script's own comments).",
            "commit_resolution": "git shim answering TGMS_COMMIT -- no git "
                                 "binary on iTiger compute nodes",
            "out_of_scope": "email-eu: benchmarks/frozen-v1/ carries no raw "
                            "event log for email-eu (tgms/data/loaders.py's "
                            "entry downloads from SNAP, and a fresh `tgms "
                            "ingest` reassigns transaction times per D-023, "
                            "so it cannot reproduce the frozen suite's "
                            "gold). Not run.",
        },
        "seed": {"value": args.base_seed},
        "dataset": {
            "name": "collegemsg (surface A) + 29 runnable TGIR-v1 plans on "
                    "the LDBC-shaped fixture (surface B)",
            "digest": dataset_digest(records),
            "digest_kind": "manifest",
        },
        "result_digest": campaign_result_digest(records),
        "protocol": {
            "warmups": 0,
            "reps": total_trials,
            "ceilings": {
                "child_timeout_s": args.wall_per_task_s,
                "array_concurrency": args.concurrency,
            },
        },
        "record": record_path,
        "total_trials": total_trials,
        "total_trials_requested": total_requested,
        "n_records": len(records),
        "per_cell_gate_table": rows,
        "shortfalls": shortfalls,
        "silent_violations": viol,
        "headline_silent_violation_count": sum(v["count"] for v in headline_viol),
        "non_headline_silent_violation_count": sum(
            v["count"] for v in viol if not v["headline"]),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"merged {len(records)} records, {total_trials}/{total_requested} trials "
         f"-> {args.out}")
    print(f"headline silent-violation trials: {manifest['headline_silent_violation_count']}")
    print(f"non-headline (F2-3/F1-3b/F1-6) silent-violation trials: "
         f"{manifest['non_headline_silent_violation_count']}")
    if shortfalls:
        print(f"{len(shortfalls)} (cell, gate) shortfalls (n_cases < n_requested):")
        for s in shortfalls:
            print(f"  {s['cell']:8} strict={s['strict_gate']!s:5} "
                 f"{s['n_cases']}/{s['n_requested']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
