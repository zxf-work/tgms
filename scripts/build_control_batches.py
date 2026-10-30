#!/usr/bin/env python3
"""Build the per-batch and per-check record files of a `tgms-control` loop.

A control loop is one directory per cell (`<campaign-dir>/<cell_id>/`), each
holding the storm CLI's per-batch rows `storm-<store>-0-rows.jsonl`. This
script reads those per-cell row files (and nothing else; plain files in the
campaign directory, such as progress logs, are skipped) and writes four files,
one line per batch, values copied unchanged from the per-cell rows:

- `tgms-control-<label>-batches.jsonl`: per batch the per-configuration
  `check_wall_ms`, `false_fresh_count`, `false_stale_count`,
  `invalidated_count`, `refresh_wall_ms` and `ttf_ms`, with `batch_index`,
  `cell_id`, `global_recompute_wall_ms` and `ttf_mode`;
- `tgms-control-<label>-batches.SOURCES.txt`: one line per cell,
  `<sha256>  <cell_id>  <path>`;
- `tgms-control-checks-<label>.jsonl`: per batch `candidate_survivors`,
  `intersects_calls` and the `tgms-L0` / `tgms-L1` `check_wall_ms`, with the
  `control` label;
- `tgms-control-checks-<label>.SOURCES.txt`: one line per cell,
  `<sha256>  <label>  <cell_id>  <path>`.

Cells are visited in sorted name order and rows in file order; JSON is
written with sorted keys, so the output is deterministic. The sha256 and path
in the SOURCES files are those of the row file as passed, so a rebuild that
must reproduce a published SOURCES file has to be given the same
`--campaign-dir` spelling (the published ones cite absolute paths).

    scripts/build_control_batches.py \\
        --campaign-dir /mnt/project/xzhang/tgms/external-v1/coarse \\
        --label 2026-10-30-coarse --out-dir benchmarks/external-v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ARM_FIELDS = ("check_wall_ms", "false_fresh_count", "false_stale_count",
              "invalidated_count", "refresh_wall_ms", "ttf_ms")


def build(campaign_dir: Path, label: str) -> dict[str, str]:
    """Return the four files' contents, keyed by file name."""
    batches: list[str] = []
    batch_sources: list[str] = []
    checks: list[str] = []
    check_sources: list[str] = []
    for cell in sorted(p.name for p in Path(campaign_dir).iterdir() if p.is_dir()):
        rows_path = next(Path(campaign_dir, cell).glob("storm-*-rows.jsonl"))
        raw = rows_path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        batch_sources.append(f"{sha}  {cell}  {rows_path}")
        check_sources.append(f"{sha}  {label}  {cell}  {rows_path}")
        for line in raw.decode("utf-8").splitlines():
            r = json.loads(line)
            batches.append(json.dumps({
                "arms": {a: {k: v[k] for k in ARM_FIELDS}
                         for a, v in r["arms"].items()},
                "batch_index": r["batch_index"], "cell_id": cell,
                "global_recompute_wall_ms": r["global_recompute_wall_ms"],
                "ttf_mode": r["ttf_mode"]}, sort_keys=True))
            checks.append(json.dumps({
                "batch_index": r["batch_index"],
                "candidate_survivors": r["candidate_survivors"],
                "cell_id": cell, "control": label,
                "intersects_calls": r["intersects_calls"],
                "tgms_l0_check_wall_ms": r["arms"]["tgms-L0"]["check_wall_ms"],
                "tgms_l1_check_wall_ms": r["arms"]["tgms-L1"]["check_wall_ms"]},
                sort_keys=True))
    out = {
        f"tgms-control-{label}-batches.jsonl": batches,
        f"tgms-control-{label}-batches.SOURCES.txt": batch_sources,
        f"tgms-control-checks-{label}.jsonl": checks,
        f"tgms-control-checks-{label}.SOURCES.txt": check_sources,
    }
    return {name: "\n".join(lines) + "\n" for name, lines in out.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--campaign-dir", type=Path, required=True,
                    help="directory of per-cell output directories")
    ap.add_argument("--label", required=True,
                    help="loop label, e.g. 2026-10-30-coarse; names the files "
                         "and is the `control` value of the checks rows")
    ap.add_argument("--out-dir", type=Path, required=True,
                    help="directory the four files are written to")
    args = ap.parse_args(argv)
    files = build(args.campaign_dir, args.label)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out_dir / name).write_bytes(text.encode("utf-8"))
        print(f"wrote {args.out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
