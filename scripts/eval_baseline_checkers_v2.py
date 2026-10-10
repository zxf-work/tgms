#!/usr/bin/env python
"""Five deterministic checkers over EvidenceBench v2 (D-170 lane A).

The v1 matrix (eval-baseline-checkers.json) ran two simple checkers over
the 27 single-step cells. v2 adds the composition family (multi-step
plans executed by the real Executor, whose cited step's own metadata is
clean while an upstream input is incomplete or read at another basis)
and two stronger checkers:

  value_only           B1 of v1, unchanged
  taint_all            B2 of v1, made transitive over the dependency closure
  metadata_rules       the strongest per-step rule set over ordinary
                       result metadata (faultbench.check_metadata_rules)
  ecqr_no_propagation  the real verifier on the descriptor the adapter
                       emits with Lemma 3.10 propagation off
  ecqr                 the shipped system (propagation on)

Pure Python and deterministic; no model, no randomness, no store.

    python scripts/eval_baseline_checkers_v2.py \
        --json benchmarks/results-v1/eval-baseline-checkers-v2.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
from pathlib import Path

from tgms.evidence.faultbench import CHECKERS, run_matrix_v2


def _commit() -> str:
    c = os.environ.get("COMMIT", "")
    if c:
        return c
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args()
    if args.json.exists():
        raise SystemExit(f"{args.json} exists; receipts are never "
                         f"overwritten")

    r = run_matrix_v2()
    cells = r["cells"]
    n_scope: dict[str, dict[str, int]] = {}
    for c in cells:
        d = n_scope.setdefault(c["scope"], {"n": 0, "must_certify": 0,
                                            "must_not_certify": 0})
        d["n"] += 1
        d[c["expectation"]] += 1

    print(f"{'checker':22s} {'single FA/FR':>13s} {'comp FA/FR':>11s}")
    for name in CHECKERS:
        s = r["summary"][name]["by_scope"]
        ss, cc = s["single_step"], s["composition"]
        print(f"{name:22s} {ss['false_accepts']:>6d}/{ss['false_rejects']:<6d}"
              f" {cc['false_accepts']:>5d}/{cc['false_rejects']:<5d}")

    out = {
        "protocol": {
            "matrix": "EvidenceBench v2 = v1 single-step cells "
                      "(faultbench.all_cases order) + "
                      "faultbench.composition_cases()",
            "checkers": list(CHECKERS),
            "integrity_precheck": "result digest vs descriptor result_id "
                                  "(A4) before every checker",
            "composition_execution": "tgms.agent.executor.Executor over a "
                                     "scripted router; propagate=True for "
                                     "ecqr, propagate=False for the rest",
            "false_accept": "certified on a must_not_certify cell",
            "false_reject": "not certified on a must_certify cell",
            "deterministic": True,
        },
        "cells_by_scope": n_scope,
        "summary": r["summary"],
        "cells": cells,
        "v1_receipt_untouched": "eval-baseline-checkers.json",
        "manifest": {
            "commit": _commit(), "host": platform.node(),
            "date": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"),
            "python": platform.python_version()},
    }
    args.json.write_text(json.dumps(out, indent=1) + "\n")
    print(f"record -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
