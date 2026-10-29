#!/usr/bin/env python3
"""Assemble the P-WH-SYM record from the replay runs of
`scripts/withheld_symmetric.py`.

Inputs: two independent runs of the delayed schedule, one run of the
in-order schedule (the fidelity control) and the control run's per-burst
rows (`storm-<store>-<seed>-rows.jsonl`). The assembler refuses to write a
record unless the two delayed runs agree on every count and every row
(wall time, host and timestamp excluded), and it carries the
reproduction check of the in-order replay against the control run.

Writes `<out-dir>/withheld-symmetric-<date>.json` (counts + provenance)
and `<out-dir>/withheld-symmetric-<date>-rows.jsonl` (one row per artifact
per read point, for the affected artifacts and the control sample).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

CELL_ID = "synth-iv-60k-c4-deep-n1000-s0"
RP_BEFORE, RP_BURST11, RP_AFTER = "R10", "R11", "R11-after-arrival"
VOLATILE = ("wall_s", "host", "timestamp_utc")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def stable(run: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in run.items() if k not in VOLATILE}


def fidelity(inorder: dict[str, Any], control_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the in-order replay's per-burst candidate and invalidation
    counts with the control run's rows, burst by burst."""
    rows = []
    ok = True
    for ep in inorder["epochs"]:
        ctl = control_rows[ep["epoch"] - 1]
        a0, a1 = ctl["arms"]["tgms-L0"], ctl["arms"]["tgms-L1"]
        same = (ep["candidates"] == ctl["candidate_survivors"]
                and ep["invalidated_l0"] == a0["invalidated_count"]
                and ep["invalidated_l1"] == a1["invalidated_count"])
        ok = ok and same
        rows.append({"epoch": ep["epoch"], "candidates": ep["candidates"],
                     "invalidated_l0": ep["invalidated_l0"], "invalidated_l1": ep["invalidated_l1"],
                     "control_candidates": ctl["candidate_survivors"],
                     "control_invalidated_l0": a0["invalidated_count"],
                     "control_invalidated_l1": a1["invalidated_count"], "match": same})
    return {"bursts_compared": len(rows), "all_match": ok, "per_burst": rows,
            "registration_digest_matches_oracle0": inorder["registration_digest_matches_oracle0"],
            "registered": inorder["registered"],
            "end_state_mismatch_vs_oracle11": inorder["end_state_mismatch_vs_oracle11"],
            "log_vs_oracle_mismatch": {rp: v["log_vs_expected_mismatch"]
                                       for rp, v in inorder["read_points"].items()}}


def summarize(delayed: dict[str, Any], inorder: dict[str, Any]) -> dict[str, Any]:
    c = delayed["counts"]
    before, mid, after = c[RP_BEFORE], c[RP_BURST11], c[RP_AFTER]
    aff_b, aff_a = before["affected"], after["affected"]
    ctl_b, ctl_a = before["control"], after["control"]
    rps = delayed["read_points"]
    ff_log = sum(c[rp][g]["false_fresh_log"] for rp in c for g in ("affected", "control"))
    ff_log_registry = sum(rps[rp]["registry"]["false_fresh_log"] for rp in rps)
    ino = inorder["counts"]
    return {
        "cell_id": CELL_ID,
        "held_epoch": 10,
        "affected": len(delayed["affected"]),
        "control": len(delayed["control"]),
        # the four headline counts (affected artifacts, TGMS verdict at level 0)
        "tgms_fresh_before_arrival": aff_b["fresh"],
        "tgms_stale_vs_eventual_before_arrival": aff_b["stale_vs_eventual"],
        "tgms_possibly_stale_after_arrival": aff_a["possibly_stale"],
        "tgms_fresh_after_arrival": aff_a["fresh"],
        "tgms_false_fresh_log_before_arrival": aff_b["false_fresh_log"],
        "tgms_false_fresh_log_after_arrival": aff_a["false_fresh_log"],
        "tgms_false_fresh_log_all_read_points": ff_log,
        "tgms_false_fresh_log_registry_all_read_points": ff_log_registry,
        "falsifier_triggered": aff_a["fresh"] > 0,
        "control_fresh_before_arrival": ctl_b["fresh"],
        "control_possibly_stale_before_arrival": ctl_b["possibly_stale"],
        "control_possibly_stale_after_arrival": ctl_a["possibly_stale"],
        "control_fresh_after_arrival": ctl_a["fresh"],
        "control_false_fresh_log_after_arrival": ctl_a["false_fresh_log"],
        "affected_possibly_stale_at_burst11_before_arrival": mid["affected"]["possibly_stale"],
        "level1": {"affected_fresh_before_arrival": aff_b["fresh_l1"],
                   "affected_possibly_stale_after_arrival": aff_a["possibly_stale_l1"]},
        "counts": c,
        "registry": {rp: v["registry"] for rp, v in rps.items()},
        "log_vs_oracle_mismatch": {rp: v["log_vs_expected_mismatch"] for rp, v in rps.items()},
        "end_state_mismatch_vs_oracle11": delayed["end_state_mismatch_vs_oracle11"],
        "late_tt": delayed["late_tt"],
        "epochs": delayed["epochs"],
        "inorder_control": {
            "affected_possibly_stale_at_R10": ino["R10"]["affected"]["possibly_stale"],
            "affected_fresh_at_R10": ino["R10"]["affected"]["fresh"],
            "affected_false_fresh_log_at_R10": ino["R10"]["affected"]["false_fresh_log"],
            "counts": ino,
        },
    }


def build(runs: list[dict[str, Any]], inorder: dict[str, Any], control_rows: list[dict[str, Any]],
          *, git_commit: str, script_sha256: str, timestamp_utc: str, host_block: dict[str, Any],
          date: str, rows_relpath: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(runs) < 2:
        raise ValueError("need at least two delayed runs to check repeatability")
    first = stable(runs[0])
    for i, r in enumerate(runs[1:], start=2):
        if stable(r) != first:
            raise ValueError(f"delayed run {i} differs from run 1 (counts or rows); "
                             "refusing to write a record")
    fid = fidelity(inorder, control_rows)
    if not fid["all_match"]:
        raise ValueError("in-order replay does not reproduce the control run's per-burst counts")
    summary = summarize(runs[0], inorder)
    summary["repeat"] = {"runs": len(runs), "identical_counts_and_rows": True,
                         "wall_s": [r["wall_s"] for r in runs]}
    summary["fidelity"] = fid
    rows = ([dict(r, schedule="delayed") for r in runs[0]["rows"]]
            + [dict(r, schedule="inorder") for r in inorder["rows"]])
    record = {
        "schema_version": "1.0.0",
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": host_block,
        "config": {"campaign": "withheld-symmetric", "cell_id": CELL_ID,
                   "held_epoch": 10, "arrival": "after the burst-11 reads",
                   "control_sample_size": len(runs[0]["control"]), "control_seed": 0,
                   "check_levels": ["level-0", "level-1"], "date": date,
                   "script_sha256": script_sha256},
        "seed": {"value": 0},
        "dataset": {"name": CELL_ID + " (export bundle; same cell as the withheld-correction cell)",
                    "digest": _sha(first["affected"] + first["control"]),
                    "digest_kind": "manifest"},
        "result_digest": _sha(summary),
        "protocol": {"warmups": 0, "reps": len(runs),
                     "ceilings": {"note": "counts experiment; no timing claim"}},
        "record": rows_relpath,
        "n_cells": 1,
        "summary": summary,
    }
    return record, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--delayed", nargs="+", required=True, type=Path)
    ap.add_argument("--inorder", required=True, type=Path)
    ap.add_argument("--control-rows", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--date", required=True)
    ap.add_argument("--git-commit", required=True)
    ap.add_argument("--script-sha256", required=True)
    ap.add_argument("--timestamp-utc", required=True)
    ap.add_argument("--host", default="xzgpu")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--cpus", type=int, required=True)
    ap.add_argument("--ram-gb", type=float, required=True)
    ap.add_argument("--rows-relpath", required=True)
    a = ap.parse_args(argv)
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in a.delayed]
    inorder = json.loads(a.inorder.read_text(encoding="utf-8"))
    control_rows = [json.loads(line) for line in a.control_rows.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    host_block = {"host": a.host, "platform": a.platform, "cpus": a.cpus, "ram_gb": a.ram_gb}
    record, rows = build(runs, inorder, control_rows, git_commit=a.git_commit,
                         script_sha256=a.script_sha256, timestamp_utc=a.timestamp_utc,
                         host_block=host_block, date=a.date, rows_relpath=a.rows_relpath)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    rec_path = a.out_dir / f"withheld-symmetric-{a.date}.json"
    rows_path = a.out_dir / f"withheld-symmetric-{a.date}-rows.jsonl"
    rec_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                         encoding="utf-8")
    print(f"wrote {rec_path}\nwrote {rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
