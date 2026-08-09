#!/usr/bin/env python
"""Unsupported-claim composition, plan depth, and context tokens (D-127).

Three questions from the motivation/visual review, all answered from
the frozen m8 run records without new inference:

1. COMPOSITION — what kinds of claims are proposed without support?
   The SQL enforced arm (b6e) stores per-claim verdicts in
   meta.claim_verdicts, so its breakdown is verdict-exact. The
   operator arms store only per-run observational UCR, so their
   breakdown is by proposed-claim kind over the determinate runs
   (u == 0 or u == 1); runs with fractional u are counted separately
   as ambiguous, never allocated.

2. DEPTH — does unsupported incidence grow with plan depth?
   Mean per-run pre-gate unsupported fraction by len(plan_ops)
   bucket (1 / 2 / 3+), operator arms (SQL plans are single
   statements).

3. TOKENS — what would support reasoning from raw context ride on,
   versus the descriptor the verifier consumes? Median/p95 model
   input tokens per task-run (the agent loop's actual context) and
   median/p95 tokens of the real serialized ECQR descriptors stored
   in the SQL arm records, under the frozen model tokenizer. The
   verifier itself consumes 0 model tokens; its CPU cost is in
   eval-verifier-scaling.json.

    python scripts/eval_unsupported_composition.py --runs runs \
        --json benchmarks/results-v1/eval-unsupported-composition.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import statistics
import subprocess
from pathlib import Path

DS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def rows_for(runs: Path, ds: str, sub: str, system: str):
    out = []
    for f in glob.glob(str(runs / f"m8-{ds}-{sub}" / "results" / "*.json")):
        r = json.loads(open(f).read())
        if r.get("system") == system:
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    ap.add_argument("--json", required=True)
    args = ap.parse_args()
    out: dict = {"host": platform.node()}

    # 1a. operator arms: claim-kind composition over determinate runs
    comp = {}
    for ds in DS:
        rows = rows_for(args.runs, ds, "tgms", "ours-noverify")
        kinds: dict[str, int] = {}
        det_u1 = det_u0 = ambiguous = no_claims = 0
        for r in rows:
            u = r.get("ucr")
            cl = (r.get("answer_object") or {}).get("claims") or []
            if u is None or not cl:
                no_claims += 1
                continue
            if u == 0:
                det_u0 += 1
            elif u == 1:
                det_u1 += 1
                for c in cl:
                    k = c.get("type", "?")
                    kinds[k] = kinds.get(k, 0) + 1
            else:
                ambiguous += 1
        comp[f"{ds}|operators"] = {
            "unsupported_claims_by_kind": kinds,
            "runs_all_supported": det_u0,
            "runs_all_unsupported": det_u1,
            "runs_ambiguous_fraction": ambiguous,
            "runs_without_claims": no_claims,
        }

    # 1b. SQL enforced arm: verdict-exact composition
    for ds in DS:
        rows = rows_for(args.runs, ds, "sql", "b6e")
        verdicts: dict[str, int] = {}
        kinds_unsup: dict[str, int] = {}
        for r in rows:
            cvs = (r.get("meta") or {}).get("claim_verdicts") or []
            cl = {c.get("id"): c
                  for c in (r.get("answer_object") or {}).get("claims") or []}
            for cv in cvs:
                v = cv.get("ecqr_verdict", "?")
                verdicts[v] = verdicts.get(v, 0) + 1
                if v != "SUPPORTED":
                    k = (cl.get(cv.get("id")) or {}).get("type", "?")
                    kinds_unsup[k] = kinds_unsup.get(k, 0) + 1
        comp[f"{ds}|sql"] = {
            "claim_verdicts": verdicts,
            "unsupported_claims_by_kind": kinds_unsup,
        }
    out["composition"] = comp

    # 2. depth: pre-gate unsupported fraction by plan-op count
    depth: dict[str, dict] = {}
    for ds in DS:
        rows = rows_for(args.runs, ds, "tgms", "ours-noverify")
        for r in rows:
            u = r.get("ucr")
            ops = r.get("plan_ops") or []
            if u is None or not ops:
                continue
            b = "1" if len(ops) == 1 else ("2" if len(ops) == 2 else "3+")
            d = depth.setdefault(b, {"n": 0, "sum_u": 0.0})
            d["n"] += 1
            d["sum_u"] += u
    out["depth_operators_pooled"] = {
        b: {"n": d["n"], "mean_pre_gate_ucr": round(d["sum_u"] / d["n"], 3)}
        for b, d in sorted(depth.items())}

    # 3. tokens: agent-loop input context vs serialized descriptor
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-14B-Instruct-AWQ")
    toks_in: dict[str, dict] = {}
    for ds in DS:
        for sub, system, label in [("tgms", "ours", "operators"),
                                   ("sql", "b6e", "sql")]:
            xs = [r["tokens_in"] for r in
                  rows_for(args.runs, ds, sub, system)
                  if isinstance(r.get("tokens_in"), (int, float))]
            toks_in[f"{ds}|{label}"] = {
                "n": len(xs),
                "median": statistics.median(xs) if xs else None,
                "p95": pct(xs, 0.95)}
    out["run_input_tokens"] = toks_in

    desc_tokens = []
    for ds in DS:
        for r in rows_for(args.runs, ds, "sql", "b6e"):
            e = (r.get("meta") or {}).get("ecqr")
            if e:
                desc_tokens.append(len(tok.encode(
                    json.dumps(e, sort_keys=True, separators=(",", ":")))))
    out["descriptor_tokens_sql_frozen"] = {
        "n": len(desc_tokens),
        "median": statistics.median(desc_tokens) if desc_tokens else None,
        "p95": pct(desc_tokens, 0.95),
        "tokenizer": "Qwen/Qwen2.5-14B-Instruct-AWQ"}
    out["verifier_model_tokens"] = 0
    out["verifier_model_calls"] = 0

    print(json.dumps(out, indent=1))
    commit = os.environ.get("COMMIT", "")
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = "unknown"
    out["commit"] = commit
    Path(args.json).write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
