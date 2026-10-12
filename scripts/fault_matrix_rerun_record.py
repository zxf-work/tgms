"""Build the F1-8 re-run addendum record from the driver's raw output.

The 2026-09-13 campaign and its D-160 re-run both recorded F1-8 at 0 of 300
trials because the driver's candidate filter read an operator registry that
is populated lazily and had not been populated (fixed in e0527ea5). This
tool turns the one raw `eval_trust_boundary_matrix.py` record of the re-run
into a result manifest in the campaign's schema, reusing the campaign
merge tool's own table, digest and silent-violation code so no count is
re-typed:

    python scripts/fault_matrix_rerun_record.py \\
        --raw benchmarks/faults-v1/f1-8-rerun-2026-10-12/F1-8-collegemsg.json \\
        --meta benchmarks/faults-v1/f1-8-rerun-2026-10-12/run-meta.json \\
        --out benchmarks/faults-v1/faults-f1-8-rerun-2026-10-12.json \\
        --rows-out benchmarks/faults-v1/faults-f1-8-rerun-2026-10-12-rows.jsonl

`--meta` holds the measured run facts the raw record does not carry (wall,
host snapshot, the defect-reproduction summary); its source is the run log
committed beside it. The rows file is the raw record's `per_case` array,
one trial per line, verbatim and in order.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fault_matrix_campaign_merge as fm  # noqa: E402

ADDENDUM_TO = [
    "benchmarks/faults-v1/fault-matrix-campaign-2026-09-13.json",
    "benchmarks/faults-v1/fault-matrix-campaign-2026-09-15-d160.json",
]


def build(raw_path: Path, meta: dict, out: Path, rows_out: Path, record_rel: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / f"task-0--{raw_path.name}"
        shutil.copyfile(raw_path, staged)
        records = fm.parse_records_dir(Path(tmp))
    (r,) = records
    if r["cell"] != "F1-8" or r["strict_gate"]:
        raise ValueError("expected the primary-arm F1-8 record")
    if r["git_commit"] == "n/a":
        raise ValueError("raw record carries no commit")

    reasons: dict[str, int] = {}
    for c in r["per_case"]:
        key = f'{c["outcome"]}/{c["reason"]}'
        reasons[key] = reasons.get(key, 0) + 1

    rows_out.write_text(
        "".join(json.dumps(c, sort_keys=True, separators=(",", ":")) + "\n"
                for c in r["per_case"]), encoding="utf-8")

    snap = meta["host_snapshot"]
    manifest = {
        "schema_version": "1.0.0",
        "git_commit": r["git_commit"],
        "timestamp_utc": r["timestamp_utc"],
        "machine": {
            "host": r["machine"]["host"],
            "platform": r["machine"]["platform"],
            "cpus": r["machine"]["cpus"],
            "ram_gb": snap["ram_gb"],
        },
        "config": {
            "harness": "scripts/eval_trust_boundary_matrix.py",
            "campaign": "faults-v1 F1-8 re-run (addendum 2026-10-12)",
            "command": meta["command"],
            "flags_from": "scripts/fault_matrix_campaign.slurm (--suite collegemsg "
                          "--surface A --cells F1-8 --n 300 --seed 0, primary arm)",
            "driver_sha256": meta["driver_sha256"],
            "driver_fix_commit": meta["driver_fix_commit"],
            "defect": "the F1-8 candidate filter read the operator registry, which is "
                      "populated lazily by importing the operator modules, before anything "
                      "in the process had populated it; every base plan then looked "
                      "ineligible and the cell produced no trials",
            "defect_recorded_in": [meta["campaign_record_f1_8"], meta["d160_record_f1_8"]],
            "defect_repro": meta["defect_repro"],
            "addendum_to": ADDENDUM_TO,
            "gate_independent": "F1-8 has no answer and no claim gate (the driver's own "
                                "module docstring and run_cell); its trials are identical "
                                "under the pre-D-160 and the D-160 gate",
            "wall_s": meta["wall_s"],
            "user_s": meta["user_s"],
            "max_rss_kb": meta["max_rss_kb"],
            "host_snapshot": snap,
            "diagnostic": meta["diagnostic"],
            "run_log": "benchmarks/faults-v1/f1-8-rerun-2026-10-12/run-log.txt",
            "raw_record": "benchmarks/faults-v1/f1-8-rerun-2026-10-12/F1-8-collegemsg.json",
            "result_digest_kind": "the raw record's result_digest: sha256 over the "
                                  "canonical JSON of its per_case array",
            "dataset_digest_kind": "sha256 of benchmarks/frozen-v1/suite-collegemsg.json",
        },
        "seed": {"value": r["seed"]},
        "dataset": {"name": r["dataset"]["name"], "digest": r["dataset"]["digest"],
                    "digest_kind": "manifest"},
        "result_digest": r["result_digest"],
        "protocol": {"warmups": 0, "reps": r["n_cases"], "ceilings": {}},
        "record": record_rel,
        "total_trials": r["n_cases"],
        "total_trials_requested": r["n_requested"],
        "n_records": 1,
        "per_cell_gate_table": [{**row, "raw_file": raw_path.name}
                                for row in fm.cell_table(records)],
        "outcome_reasons": dict(sorted(reasons.items())),
        "distinct_tasks": len({c["task_id"] for c in r["per_case"]}),
        "rows_file": rows_out.name,
        "shortfalls": [s for s in (
            {"cell": r["cell"], "n_requested": r["n_requested"], "n_cases": r["n_cases"]},
        ) if s["n_cases"] < s["n_requested"]],
        "silent_violations": fm.silent_violations(records),
        "headline_silent_violation_count": 0,
        "non_headline_silent_violation_count": 0,
    }
    viol = manifest["silent_violations"]
    manifest["headline_silent_violation_count"] = sum(v["count"] for v in viol if v["headline"])
    manifest["non_headline_silent_violation_count"] = sum(
        v["count"] for v in viol if not v["headline"])
    out.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rows-out", type=Path, required=True)
    args = ap.parse_args(argv)
    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    m = build(args.raw, meta, args.out, args.rows_out,
              f"benchmarks/faults-v1/{args.out.name}")
    t = m["per_cell_gate_table"][0]
    print(f"F1-8 re-run: {m['total_trials']}/{m['total_trials_requested']} trials, "
          f"counts {t['counts']}, silent {m['headline_silent_violation_count']} "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
