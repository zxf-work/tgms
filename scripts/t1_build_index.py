#!/usr/bin/env python3
"""Build/refresh external-v1/t1/INDEX-T1.json from whatever cells T1 has
finished so far (safe to run repeatedly while t1_control.sh is still
running other cells -- it only reads each cell's own output dir and the
shared PROGRESS.log, never touches a cell still in flight's own files
except to read them)."""
import json
import re
import sys
from pathlib import Path

T1 = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/project/xzhang/tgms/external-v1/t1")
PROGRESS = T1 / "PROGRESS.log"

LINE_RE = re.compile(
    r"^(?P<cell>\S+) wall=(?P<wall>\d+)s "
    r"(?:batches_realized=(?P<br>\d+)/(?P<bt>\d+) equality=(?P<eq>\S+)|exit=(?P<exit>\d+) FAILED)$"
)

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
        cell_dir = T1 / cell
        manifests = [p for p in cell_dir.glob("storm-*.json")] if cell_dir.exists() else []
        manifest_path = str(manifests[0]) if manifests else None
        arms_scored = []
        if manifest_path:
            try:
                manifest = json.loads(Path(manifest_path).read_text())
                arms_scored = manifest.get("config", {}).get("arms", [])
            except Exception:
                pass
        eq_path = cell_dir / "t1-equality.json"
        equality = None
        if eq_path.exists():
            try:
                equality = json.loads(eq_path.read_text()).get("verdict")
            except Exception:
                equality = "unreadable"
        index[cell] = {
            "status": "done",
            "manifest_path": manifest_path,
            "wall_s": int(m.group("wall")),
            "batches_realized": int(m.group("br")),
            "batches_requested": int(m.group("bt")),
            "arms_scored": arms_scored,
            "equality_vs_export": equality or m.group("eq"),
        }

out_path = T1 / "INDEX-T1.json"
out_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
print(f"wrote {out_path} ({len(index)} cells recorded)")
