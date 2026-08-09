#!/usr/bin/env python
"""Pure-gate counterfactual replay (round-2 review §7).

The arm contrast (Operators+ECQR vs Operators, SQL+ECQR vs SQL) is an
END-TO-END configuration effect: the evidence arms' prompts include
evidence instructions, so planning can differ before the gate ever
acts. This script isolates the DETERMINISTIC gate effect: take the
UNGATED arms' own executions and proposed claims (planning, execution,
values all fixed), apply the gate's decision rule counterfactually,
and measure what changes.

Per ungated row the observational verifier column gives u = the
fraction of proposed claims judged unsupported:
  u = 0        -> gate emits the answer unchanged      (EM unchanged)
  u = 1        -> gate abstains                        (EM -> 0)
  0 < u < 1    -> ambiguous without per-claim verdicts: the answer-
                  carrying claim may or may not survive. Reported as a
                  bound [pessimistic: answer claim dropped, optimistic:
                  answer claim survives].

    python scripts/gate_counterfactual.py --runs runs \
        --json benchmarks/results-v1/eval-gate-counterfactual.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import subprocess
from pathlib import Path

DATASETS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]
UNGATED = {"ours-noverify": "tgms", "b6": "sql"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    ap.add_argument("--json", required=True)
    args = ap.parse_args()
    out = {}
    for arm, sub in UNGATED.items():
        for ds in DATASETS:
            rows = []
            for f in glob.glob(str(args.runs / f"m8-{ds}-{sub}" /
                                   "results" / "*.json")):
                r = json.loads(open(f).read())
                if r.get("system") == arm:
                    rows.append(r)
            n = len(rows)
            em0 = sum(r.get("em") or 0 for r in rows)
            em_opt = em_pes = 0.0
            abstain = ambiguous = 0
            for r in rows:
                em = r.get("em") or 0
                u = r.get("ucr")
                claims = ((r.get("answer_object") or {}).get("claims")
                          or [])
                if u is None or not claims:
                    # no proposal -> gate has nothing to act on
                    em_opt += em
                    em_pes += em
                    continue
                if u == 0:
                    em_opt += em
                    em_pes += em
                elif u == 1:
                    abstain += 1
                else:
                    ambiguous += 1
                    em_opt += em      # answer-carrying claim survives
                    # pessimistic: it does not -> abstain, contribute 0
            out[f"{ds}|{arm}"] = {
                "n_rows": n,
                "em_ungated": round(em0 / n, 4),
                "em_gate_replay_optimistic": round(em_opt / n, 4),
                "em_gate_replay_pessimistic": round(em_pes / n, 4),
                "would_abstain": abstain,
                "ambiguous_rows": ambiguous,
            }
    payload = {
        "note": "deterministic gate applied counterfactually to the "
                "ungated arms' own claims; planning/execution/values "
                "held fixed; per-claim ambiguity bounded, not guessed",
        "cells": out,
        "manifest": {
            "commit": os.environ.get("COMMIT") or subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True).stdout.strip(),
            "host": platform.node()},
    }
    with open(args.json, "w") as f:
        json.dump(payload, f, indent=1)
    for k, v in out.items():
        print(k, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
