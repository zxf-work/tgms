#!/usr/bin/env python
"""Run the fault × claim matrix and write its receipt (M4, D-104).

    python scripts/eval_fault_matrix.py --json benchmarks/results-v1/eval-fault-matrix.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import subprocess
from pathlib import Path

from tgms.evidence.faultbench import NOT_YET_COVERED, run_matrix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    r = run_matrix()
    per_fault = collections.Counter(c["fault"] for c in r.cells)
    print(f"cells={r.n}  false_certifications={r.false_certifications}  "
          f"false_rejections={r.false_rejections}")
    for c in r.cells:
        mark = "ok " if c["ok"] else "FAIL"
        print(f"  {mark} {c['claim']:17s} {c['fault']:22s} -> {c['verdict']}")
    print("declared not covered:", ", ".join(NOT_YET_COVERED))

    if args.json:
        commit = os.environ.get("COMMIT", "")
        if not commit:
            try:
                commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                        capture_output=True,
                                        text=True).stdout.strip()
            except OSError:
                commit = "unknown"
        args.json.write_text(json.dumps({
            "cells": r.cells,
            "summary": {"n_cells": r.n, "per_fault": dict(per_fault),
                        "false_certifications": r.false_certifications,
                        "false_rejections": r.false_rejections},
            "not_covered": NOT_YET_COVERED,
            "manifest": {"commit": commit, "host": platform.node()},
        }, indent=1) + "\n")
        print(f"record → {args.json}")
    return 1 if (r.false_certifications or r.false_rejections) else 0


if __name__ == "__main__":
    raise SystemExit(main())
