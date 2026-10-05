#!/usr/bin/env python3
"""Build/refresh external-v1/n1/INDEX-N1.json from whatever cells N1 has
finished so far (safe to run repeatedly while n1_control.sh is still
running other cells -- analogous to scripts/t1_build_index.py; only reads
each cell's own PROGRESS.log line + result.json/check.json, never touches
a cell still in flight's own files except to read them)."""
import json
import re
import sys
from pathlib import Path

N1 = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/project/xzhang/tgms/external-v1/n1")
PROGRESS = N1 / "PROGRESS.log"

LINE_RE = re.compile(
    r"^(?P<cell>\S+) wall=(?P<wall>\d+)s "
    r"(?:bursts=(?P<bn>\d+)/(?P<bt>\d+) oracle=(?P<agree>\d+)/(?P<total>\d+) "
    r"equality=(?P<eq>\S+)|exit=(?P<exit>\d+) FAILED)$"
)


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


index = {}
if PROGRESS.exists():
    for line in PROGRESS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            index[line.split()[0]] = {"raw_progress_line": line, "status": "unparsed"}
            continue
        cell = m.group("cell")
        if m.group("exit") is not None:
            index[cell] = {"status": "FAILED", "wall_s": int(m.group("wall")),
                           "exit": int(m.group("exit"))}
            continue

        cell_dir = N1 / cell
        result_path = cell_dir / "result.json"
        check_path = cell_dir / "check.json"
        recompute_ms_median = None
        apply_ms_median = None
        artifacts_total = None
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
                per_burst = result.get("per_cell", {}).get("per_burst", [])
                recompute_ms_median = median(
                    [r["recompute_ms"] for r in per_burst if r.get("epoch", 0) != 0])
                apply_ms_median = median(
                    [r["apply_ms"] for r in per_burst if r.get("epoch", 0) != 0])
                if per_burst:
                    g = result.get("gates", {}).get("oracle_agreement", {})
                    artifacts_total = (g.get("agree", 0) + g.get("disagree", 0)
                                      + g.get("not_answered", 0))
            except Exception:
                pass
        check_verdict = None
        if check_path.exists():
            try:
                check_verdict = json.loads(check_path.read_text()).get("verdict")
            except Exception:
                check_verdict = "unreadable"

        index[cell] = {
            "status": "done",
            "wall_s": int(m.group("wall")),
            "bursts_realized": int(m.group("bn")),
            "bursts_requested": int(m.group("bt")),
            "oracle_agree": int(m.group("agree")),
            "oracle_total": int(m.group("total")),
            "equality_level_vs_committed_grid": m.group("eq"),
            "recompute_ms_median_per_burst": recompute_ms_median,
            "apply_ms_median_per_burst": apply_ms_median,
            "artifacts_total": artifacts_total,
            "check_verdict": check_verdict,
            "result_path": str(result_path) if result_path.exists() else None,
            "check_path": str(check_path) if check_path.exists() else None,
        }

out_path = N1 / "INDEX-N1.json"
out_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
print(f"wrote {out_path} ({len(index)} cells recorded)")
