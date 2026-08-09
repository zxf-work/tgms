#!/usr/bin/env python
"""Deterministic verification baselines over EvidenceBench (review §10).

Two obvious simpler checkers, run over the same 27 (claim, evidence,
result) cells as the ECQR verifier, behind the same integrity precheck:

  B1 value-only   — certifies iff the claimed value/witness appears in
                    the cited result; ignores completeness and basis.
  B2 taint-all    — rejects everything downstream of any delivery or
                    execution incompleteness; otherwise behaves as B1.
                    (The "distrust everything truncated" strawman that
                    Fig. 3 argues against, made concrete.)

Neither consults claim-specific obligations: B1 under-blocks (accepts
page-derived counts, basis mismatches), B2 over-blocks (rejects
witness membership on truncated pages, rejects certified counts that
survive truncation) AND still under-blocks basis faults. The receipt
quantifies both against the ECQR verifier's 0/0.

    python scripts/eval_baseline_checkers.py \
        --json benchmarks/results-v1/eval-baseline-checkers.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess

from tgms.core.model import canonical_json, sha256_hex
from tgms.evidence.claims import (CompleteSet, ExactCount, Existence,
                                  Membership, Nonexistence, Scalar)
from tgms.evidence.faultbench import all_cases
from tgms.evidence.verify import _resolve_path, _row_matches, _rows


def _integrity_ok(case) -> bool:
    return sha256_hex(canonical_json(case.result)) == case.ecqr.result_id


def b1_value_only(claim, evidence, result) -> bool:
    rows = _rows(result)
    if isinstance(claim, Membership):
        return any(_row_matches(r, claim.value, claim.field) for r in rows)
    if isinstance(claim, Scalar):
        ok, got = _resolve_path(result, claim.path)
        return ok and got == claim.value
    if isinstance(claim, ExactCount):
        return claim.n == len(rows) or \
            claim.n == evidence.scope.exact_cardinality
    if isinstance(claim, CompleteSet):
        got = [r.get(claim.field) if claim.field and isinstance(r, dict)
               else (r if not isinstance(r, dict) else str(r)) for r in rows]
        want = {v if not isinstance(v, dict) else str(v)
                for v in (claim.members or [])}
        return want == set(got)
    if isinstance(claim, Existence):
        return bool(rows)
    if isinstance(claim, Nonexistence):
        return not rows
    return False


def b2_taint_all(claim, evidence, result) -> bool:
    s = evidence.scope
    if not (s.delivery_complete and s.execution_complete):
        return False
    return b1_value_only(claim, evidence, result)


def run(name, fn, cases) -> dict:
    fc, fr, cells = [], [], []
    for c in cases:
        if not _integrity_ok(c):
            certified = False   # same precheck as the ECQR harness
        else:
            certified = fn(c.claim, c.ecqr, c.result)
        bad = (certified and c.expectation == "must_not_certify")
        miss = (not certified and c.expectation == "must_certify")
        if bad:
            fc.append(f"{c.claim_kind}/{c.fault}")
        if miss:
            fr.append(f"{c.claim_kind}/{c.fault}")
        cells.append({"claim": c.claim_kind, "fault": c.fault,
                      "certified": certified,
                      "expectation": c.expectation,
                      "ok": not (bad or miss)})
    return {"baseline": name, "n_cells": len(cases),
            "false_certifications": len(fc), "fc_cells": fc,
            "false_rejections": len(fr), "fr_cells": fr,
            "cells": cells}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    args = ap.parse_args()
    cases = all_cases()
    out = {
        "b1_value_only": run("b1_value_only", b1_value_only, cases),
        "b2_taint_all": run("b2_taint_all", b2_taint_all, cases),
        "ecqr_reference": {"false_certifications": 0,
                           "false_rejections": 0,
                           "receipt": "eval-fault-matrix.json"},
        "manifest": {
            "commit": os.environ.get("COMMIT") or subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True).stdout.strip(),
            "host": platform.node()},
    }
    with open(args.json, "w") as f:
        json.dump(out, f, indent=1)
    for k in ("b1_value_only", "b2_taint_all"):
        d = out[k]
        print(f"{k}: FC {d['false_certifications']}/{18} "
              f"{d['fc_cells']}  FR {d['false_rejections']}/9 "
              f"{d['fr_cells']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
