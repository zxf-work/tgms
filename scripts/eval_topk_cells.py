#!/usr/bin/env python
"""Run the top-k EvidenceBench cells and write their receipt (D-170, I2).

The cells are tgms/evidence/faultbench_topk.py's `cases()`: one fixed
candidate relation, each cell a (TopK claim, descriptor, result) triple
with its expectation. `run_cells()` judges every cell under the shipped
verifier (ecqr; a step blocked by Lemma 3.10 rule (a) has no descriptor
and is reported BLOCKED), the value-only checker and the taint checker,
all behind the A4 integrity precheck. Deterministic and model-free.

    python scripts/eval_topk_cells.py --json benchmarks/results-v1/eval-topk-cells.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
from pathlib import Path

from tgms.evidence.faultbench_topk import CHECKERS, cases, run_cells
from tgms.evidence.verify import Verdict


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

    cells = cases()
    r = run_cells(cells)
    S = Verdict.SUPPORTED.value
    n_exp = {"must_certify": 0, "must_not_certify": 0}
    for c in cells:
        n_exp[c.expectation] += 1
    # each expectation must agree with the reference oracle `truth()`
    truth_disagree = [row["fault"] for row in r["cells"]
                      if row["truth"] != (row["expectation"]
                                          == "must_certify")]
    summary = {}
    for ch in CHECKERS:
        fc = r["checkers"][ch]["false_certifications"]
        fr = r["checkers"][ch]["false_rejections"]
        summary[ch] = {
            "false_certifications": len(fc),
            "false_rejections": len(fr),
            "false_certification_cells": fc,
            "false_rejection_cells": fr,
            "verdict_differs_from_expected": [
                row["fault"] for row in r["cells"]
                if row["verdicts"][ch] != row["expected"][ch]],
            "certified": sum(1 for row in r["cells"]
                             if row["verdicts"][ch] == S),
        }

    print(f"cells={r['n_cells']}  must_certify={n_exp['must_certify']}  "
          f"must_not_certify={n_exp['must_not_certify']}  "
          f"truth_disagreements={len(truth_disagree)}")
    for ch in CHECKERS:
        s = summary[ch]
        print(f"  {ch:15s} false_cert={s['false_certifications']:2d} "
              f"false_reject={s['false_rejections']:2d} "
              f"verdict!=expected={len(s['verdict_differs_from_expected'])}")

    out = {
        "protocol": {
            "cells": "tgms.evidence.faultbench_topk.cases() (claim kind "
                     "top_k; sequence and set forms; one candidate "
                     "relation, faultbench_topk.CANDIDATES)",
            "checkers": list(CHECKERS),
            "integrity_precheck": "result digest vs descriptor result_id "
                                  "(A4) before every checker",
            "blocked_cell": "ecqr=None: the step is blocked by Lemma 3.10 "
                            "rule (a) and emits no descriptor (BLOCKED); "
                            "the baselines judge the descriptor a "
                            "non-blocking executor would emit "
                            "(Cell.unblocked)",
            "false_certification": "SUPPORTED on a must_not_certify cell",
            "false_rejection": "not SUPPORTED on a must_certify cell",
            "reference_oracle": "faultbench_topk.truth(); every "
                                "expectation is checked against it",
            "deterministic": True,
        },
        "n_cells": r["n_cells"],
        "cells_by_expectation": n_exp,
        "truth_disagreements": truth_disagree,
        "summary": summary,
        "cells": r["cells"],
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
