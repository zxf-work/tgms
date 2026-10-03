#!/usr/bin/env python3
"""T1 (same-host TGMS control) equality checker.

Compares one T1 harness run (scripts/bench_correction_storm.py output:
`storm-<store_label>-<seed>.json` + `-rows.jsonl`) against the matching
cell's export bundle (`external-v1/export/<cell_id>/{deltas,oracle}.jsonl`,
`digests.json`, `export-manifest.json`). Both ran at the same commit
(fdd393c) on the same host against the same scratch-store bytes, so a
faithful replay should match at L1 (final event-log digest) and at every
per-batch field.

Writes <out_dir>/t1-equality.json. Never touches the export tree (read-only).
"""
import argparse
import json
import sys
from pathlib import Path


def sha_of_rows_digest(manifest: dict) -> str | None:
    return manifest.get("dataset", {}).get("digest")


def load_oracle_changed(oracle_path: Path) -> list[list[str]]:
    """Per-epoch changed-artifact-name lists, epoch 1..N, derived by diffing
    consecutive {name: digest} maps (epoch 0 is the baseline)."""
    lines = [json.loads(l) for l in oracle_path.read_text().splitlines() if l.strip()]
    changed_per_epoch = []
    for k in range(1, len(lines)):
        prev = lines[k - 1]["digests"]
        cur = lines[k]["digests"]
        names = set(prev) | set(cur)
        changed = sorted(n for n in names if prev.get(n) != cur.get(n))
        changed_per_epoch.append(changed)
    return changed_per_epoch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-id", required=True)
    ap.add_argument("--t1-dir", required=True, type=Path,
                     help="this cell's T1 output dir (contains storm-*.json)")
    ap.add_argument("--export-dir", required=True, type=Path,
                     help="external-v1/export/<cell_id>/ (read-only)")
    args = ap.parse_args()

    t1_dir = args.t1_dir
    export_dir = args.export_dir

    manifests = sorted(t1_dir.glob("storm-*.json"))
    manifests = [m for m in manifests if not m.name.endswith("-rows.json")]
    if not manifests:
        result = {"cell_id": args.cell_id, "verdict": "FAIL",
                   "reason": "no harness manifest found in t1_dir"}
        (t1_dir / "t1-equality.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 1
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text())
    rows_path = t1_dir / (manifest_path.stem + "-rows.jsonl")
    rows = [json.loads(l) for l in rows_path.read_text().splitlines() if l.strip()]

    batches_realized = len(rows)

    out = {
        "cell_id": args.cell_id,
        "t1_manifest": str(manifest_path),
        "batches_realized": batches_realized,
        "git_commit": manifest.get("git_commit"),
    }

    # ---- L1: final event-log digest ----
    digests_path = export_dir / "digests.json"
    export_digests = json.loads(digests_path.read_text()) if digests_path.exists() else {}
    export_final_log_sha = export_digests.get("checks", {}).get("final_log_sha256")
    t1_final_log_sha = sha_of_rows_digest(manifest)
    l1_match = (export_final_log_sha is not None and t1_final_log_sha is not None
                and export_final_log_sha == t1_final_log_sha)
    out["l1_eventlog_sha_match"] = l1_match
    out["t1_eventlog_sha256"] = t1_final_log_sha
    out["export_final_log_sha256"] = export_final_log_sha

    # ---- per-batch L2 comparison ----
    deltas_path = export_dir / "deltas.jsonl"
    oracle_path = export_dir / "oracle.jsonl"
    per_batch_results = []
    if deltas_path.exists() and oracle_path.exists():
        deltas = [json.loads(l) for l in deltas_path.read_text().splitlines() if l.strip()]
        changed_per_epoch = load_oracle_changed(oracle_path)
        n = min(len(rows), len(deltas), len(changed_per_epoch))
        for i in range(n):
            row = rows[i]
            delta = deltas[i]
            exp_changed = changed_per_epoch[i]
            t1_changed = sorted(row.get("changed", []))
            field_match = {
                "correction_class": row.get("correction_class") == delta.get("correction_class"),
                "generator": row.get("correction_generator") == delta.get("generator"),
                "placement": row.get("correction_placement") == delta.get("placement"),
                "changed": t1_changed == exp_changed,
            }
            per_batch_results.append({
                "batch_index": row.get("batch_index"),
                "epoch": delta.get("epoch"),
                **field_match,
                "all_match": all(field_match.values()),
            })
    out["per_batch"] = per_batch_results
    out["n_batches_compared"] = len(per_batch_results)
    out["n_batches_all_match"] = sum(1 for b in per_batch_results if b["all_match"])

    l2_full_match = (len(per_batch_results) > 0
                      and out["n_batches_all_match"] == len(per_batch_results))

    if l1_match:
        verdict = "L1"
    elif l2_full_match:
        verdict = "L2"
    elif out["n_batches_all_match"] > 0:
        verdict = "L2-partial"
    else:
        verdict = "FAIL"
    out["verdict"] = verdict

    (t1_dir / "t1-equality.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"cell_id": args.cell_id, "verdict": verdict,
                       "batches_realized": batches_realized,
                       "n_batches_all_match": out["n_batches_all_match"],
                       "n_batches_compared": out["n_batches_compared"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
