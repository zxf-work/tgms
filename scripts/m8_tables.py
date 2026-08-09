#!/usr/bin/env python
"""The M8 table of record, generated from rows (D-109).

No manual transcription: every headline number is computed here from the
cached result rows and written as JSON + markdown with paired-bootstrap
CIs. Re-running regenerates the tables byte-for-byte from the same rows.

    python scripts/m8_tables.py --runs runs --out benchmarks/results-v1/m8
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import platform
import subprocess
from pathlib import Path

DATASETS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]
ARMS = ["ours", "ours-noverify", "b6", "b6e"]


def _load(runs: Path, prefix: str, suffix: str = "") -> list[dict]:
    rows = []
    for ds in DATASETS:
        for kind in ("tgms", "sql"):
            for f in glob.glob(str(runs / f"{prefix}-{ds}-{kind}{suffix}" /
                                   "results" / "*.json")):
                r = json.load(open(f))
                r["_dataset"] = ds
                rows.append(r)
    return rows


def _boot_ci(diffs_by_task: dict[str, list[float]], n: int = 10_000,
             seed: int = 0) -> tuple[float, float, float]:
    """Paired bootstrap over tasks; each task's per-seed diffs are averaged
    first so seeds do not masquerade as independent tasks."""
    import random
    tasks = [sum(v) / len(v) for v in diffs_by_task.values()]
    if not tasks:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    point = sum(tasks) / len(tasks)
    stats = []
    for _ in range(n):
        s = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        stats.append(sum(s) / len(s))
    stats.sort()
    return point, stats[int(0.025 * n)], stats[int(0.975 * n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--prefix", default="m8")
    ap.add_argument("--suffix", default="",
                    help="model-axis out_dir suffix, e.g. -7b or -32b")
    args = ap.parse_args()
    rows = _load(args.runs, args.prefix, args.suffix)
    if not rows:
        print("no rows found")
        return 1

    # per-arm per-dataset aggregates over all seeds
    table = {}
    for ds in DATASETS:
        for arm in ARMS:
            sel = [r for r in rows
                   if r["_dataset"] == ds and r.get("system") == arm]
            if not sel:
                continue
            n = len(sel)
            seeds = sorted({r.get("seed") for r in sel})
            em = sum(r.get("em") or 0 for r in sel) / n
            probes = [r for r in sel if r.get("family") == "probe"]
            ucr = [r.get("ucr") for r in sel if r.get("ucr") is not None]
            pre = [r.get("ucr_pre_gate") for r in sel
                   if r.get("ucr_pre_gate") is not None]
            pre_e = [(r.get("meta") or {}).get("ucr_pre_gate_e")
                     for r in sel
                     if (r.get("meta") or {}).get("ucr_pre_gate_e")
                     is not None]
            # coverage/retention/abstention (review P1.2): an answer is
            # "certified" when its final object carries >=1 claim; in
            # gated arms every carried claim is verifier-SUPPORTED, so
            # this is certified-answer coverage. Retention is per-answer
            # 1 - pre-gate-unsupported-fraction over rows that emitted an
            # answer (the gate drops exactly the unsupported claims);
            # answers gated to nothing count as abstentions, never in
            # the retention mean.
            n_final = [len(((r.get("answer_object") or {}).get("claims"))
                           or []) for r in sel]
            abstain = sum(1 for k in n_final if k == 0)
            pre_rows = [r for r in sel
                        if r.get("ucr_pre_gate") is not None
                        and len(((r.get("answer_object") or {})
                                 .get("claims")) or [])]
            retention = ([1 - r["ucr_pre_gate"] for r in pre_rows]
                         if pre_rows else [])
            table[(ds, arm)] = {
                "n_rows": n, "seeds": seeds, "em": round(em, 4),
                "probe_em": round(sum(r.get("em") or 0 for r in probes)
                                  / len(probes), 4) if probes else None,
                "ucr": round(sum(ucr) / len(ucr), 4) if ucr else None,
                "ucr_pre_gate": round(sum(pre) / len(pre), 4)
                if pre else None,
                "ucr_pre_gate_e": round(sum(pre_e) / len(pre_e), 4)
                if pre_e else None,
                "certified_answer_coverage": round(1 - abstain / n, 4),
                "abstention_rate": round(abstain / n, 4),
                "claim_retention": round(sum(retention) / len(retention),
                                         4) if retention else None,
            }

    # paired contrasts (per task, seed-averaged): the two M5 questions
    contrasts = {}
    for ds in DATASETS:
        by = collections.defaultdict(dict)
        for r in rows:
            if r["_dataset"] != ds:
                continue
            key = (r.get("task_id"), r.get("seed"))
            by[key][r.get("system")] = r.get("em") or 0
        for name, a, b in (("interface: b6e - ours", "b6e", "ours"),
                           ("evidence(sql): b6e - b6", "b6e", "b6"),
                           ("evidence(tgms): ours - ours-noverify",
                            "ours", "ours-noverify")):
            diffs = collections.defaultdict(list)
            for (tid, _seed), arms in by.items():
                if a in arms and b in arms:
                    diffs[tid].append(arms[a] - arms[b])
            point, lo, hi = _boot_ci(diffs)
            contrasts[f"{ds} | {name}"] = {
                "delta_em": round(point, 4), "ci95": [round(lo, 4),
                                                     round(hi, 4)],
                "n_tasks": len(diffs)}

    args.out.mkdir(parents=True, exist_ok=True)
    commit = os.environ.get("COMMIT", "")
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = "unknown"
    payload = {"table": {f"{ds}|{arm}": v for (ds, arm), v in table.items()},
               "contrasts": contrasts,
               "manifest": {"commit": commit, "host": platform.node(),
                            "n_rows": len(rows)}}
    (args.out / "m8-tables.json").write_text(
        json.dumps(payload, indent=1) + "\n")

    md = ["# M8 table of record (generated — do not edit)\n"]
    md.append("| dataset | arm | rows | em | probes | ucr | ucr_pre |")
    md.append("|---|---|---:|---:|---:|---:|---:|")
    for (ds, arm), v in table.items():
        md.append(f"| {ds} | {arm} | {v['n_rows']} | {v['em']} | "
                  f"{v['probe_em']} | {v['ucr']} | "
                  f"{v['ucr_pre_gate'] if v['ucr_pre_gate'] is not None else v['ucr_pre_gate_e']} |")
    md.append("\n## Paired contrasts (per-task, seed-averaged, 10k bootstrap)\n")
    md.append("| contrast | Δem | 95% CI | n tasks |")
    md.append("|---|---:|---|---:|")
    for k, v in contrasts.items():
        md.append(f"| {k} | {v['delta_em']} | {v['ci95']} | {v['n_tasks']} |")
    (args.out / "m8-tables.md").write_text("\n".join(md) + "\n")
    print(f"wrote {args.out}/m8-tables.{{json,md}} from {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
