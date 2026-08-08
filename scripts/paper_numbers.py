#!/usr/bin/env python
"""paper_numbers.json — every headline number, regenerated from receipts
(outline review item 6; the denominator audit is built in as text).

Committed-receipt sections always regenerate; sections that need cached
run rows (M6 frontier, A4') fill only where `runs/` is present and mark
themselves absent otherwise — a number that cannot be regenerated does
not get invented.

    python scripts/paper_numbers.py --out benchmarks/results-v1/paper_numbers.json
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import platform
import statistics
import subprocess
from pathlib import Path

RES = Path("benchmarks/results-v1")
DATASETS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]


def cp_upper_zero(n: int, alpha: float = 0.05) -> float:
    """Clopper-Pearson 95% (two-sided) upper bound for 0 events in n."""
    return 1.0 - math.exp(math.log(alpha / 2) / n)


def _load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def fault_matrix() -> dict:
    d = _load(RES / "eval-fault-matrix.json")
    s = d["summary"]
    return {
        "cells": s["n_cells"],
        "false_certifications": s["false_certifications"],
        "false_rejections": s["false_rejections"],
        "fc_cp95_upper": round(cp_upper_zero(
            sum(1 for c in d["cells"]
                if c["expectation"] == "must_not_certify")), 4),
        "not_covered": d["not_covered"],
        "denominators": "cells are constructed verifier cases (fault "
                        "injections + clean controls), NOT task runs; the "
                        "CP bound is over the must-not-certify cells only",
    }


def frozen_2x2() -> dict:
    d = _load(RES / "m8" / "m8-tables.json")
    t, c = d["table"], d["contrasts"]
    pre_tgms = [t[f"{ds}|ours"]["ucr_pre_gate"] for ds in DATASETS]
    pre_sql = [t[f"{ds}|b6e"]["ucr_pre_gate_e"] for ds in DATASETS]
    gated = [t[f"{ds}|ours"]["ucr"] for ds in DATASETS]
    ev_deltas = {k: v for k, v in c.items() if "evidence" in k}
    return {
        "primary_rows": d["manifest"]["n_rows"],
        "em": {f"{ds}|{arm}": t[f"{ds}|{arm}"]["em"]
               for ds in DATASETS for arm in ("ours", "b6e")},
        "interface_contrasts": {k: v for k, v in c.items()
                                if "interface" in k},
        "evidence_em_deltas": ev_deltas,
        "max_evidence_em_cost": min(v["delta_em"]
                                    for v in ev_deltas.values()),
        "ucr_pre_gate_tgms": pre_tgms,
        "ucr_pre_gate_sql": pre_sql,
        "ucr_pre_range_all": [round(min(pre_sql + pre_tgms), 4),
                              round(max(pre_sql + pre_tgms), 4)],
        "ucr_gated": gated,
        "denominators": "primary_rows = end-to-end task runs (test splits "
                        "x 4 arms x 3 seeds x 3 datasets). ucr values are "
                        "MEANS OVER TASK-RUNS of the per-answer fraction "
                        "of emitted claims judged unsupported (harness "
                        "'ucr' counts all claim types; b6e's pre-gate uses "
                        "the ECQR witness mapping). Gated rates exist only "
                        "for evidence-enabled arms (ours, b6e). EM deltas "
                        "are per-task seed-averaged paired differences.",
    }


def model_axis() -> dict:
    out = {}
    for tag in ("7b", "32b"):
        d = _load(RES / f"m8-{tag}" / "m8-tables.json")
        if d is None:
            continue
        out[tag] = {f"{ds}|{arm}": {
            "em": d["table"][f"{ds}|{arm}"]["em"],
            "probes": d["table"][f"{ds}|{arm}"]["probe_em"]}
            for ds in DATASETS for arm in ("ours", "b6e")
            if f"{ds}|{arm}" in d["table"]}
    out["denominators"] = ("seed 0 only, test splits; '7b' = "
                           "Qwen2.5-7B-Instruct fp16, '32b' = "
                           "Qwen2.5-32B-Instruct-AWQ — model "
                           "CONFIGURATIONS, not a size-only axis; July's "
                           "frozen 32B number was fp16 and is not "
                           "comparable")
    return out


def guardrail() -> dict:
    xz = _load(RES / "eval-guardrail-frontier-d087.json")
    it = _load(RES / "guard-frontier-itiger-scaled.json")

    def fr_row(d, budget):
        row = d["frontier"][f"budget_{budget}ms"]
        return {"FA": row["at_default"]["false_admissions"],
                "FR": row["at_default"]["false_rejections"],
                "n": row["n_cells"]}

    # host scale recomputed from the two receipts (reproducible, not quoted)
    xz_map = {(c["op"], c["frac"], c["qid"]): c["actual_ms"]
              for c in xz["cells"] if c["store"] == "collegemsg"}
    ratios = []
    for c in it["cells"]:
        if c["store"] != "collegemsg":
            continue  # the only store measured on both hosts
        key = (c["op"], c["frac"], c["qid"])
        if key in xz_map and xz_map[key] >= 0.5 and c["actual_ms"] >= 0.1:
            ratios.append(c["actual_ms"] / xz_map[key])
    xz2 = fr_row(xz, 2000)
    return {
        "xzgpu_at_2s": xz2,
        "xzgpu_fa_cp95_upper": round(cp_upper_zero(xz2["n"]), 4),
        "itiger_scaled_at_2s": fr_row(it, 2000),
        "itiger_scaled_at_10s": fr_row(it, 10000),
        "itiger_fa_cp95_upper_54": round(cp_upper_zero(54), 4),
        "host_scale_median": round(statistics.median(ratios), 2)
        if ratios else None,
        "host_scale_n_cells": len(ratios),
        "denominators": "cells are guardrail frontier calls (operator x "
                        "window-fraction grids), not task runs; FA/FR at "
                        "the DEFAULT ceilings for the stated budget; host "
                        "scale = median per-cell actual-time ratio over "
                        "paired CollegeMsg cells above timer noise",
    }


def overhead() -> dict:
    d = _load(RES / "evidence-overhead-itiger.json")
    return {
        "descriptor_us": {k: round(v * 1000, 1)
                          for k, v in d["build_ecqr_ms"].items()
                          if k.endswith("envelope")},
        "plan_overhead_ms": d["plan_overhead"]["overhead_ms"],
        "plan_overhead_note": "fixed cost on a ~0.8 ms two-step microplan "
                              f"({d['plan_overhead']['overhead_pct']}% of "
                              "THAT plan; negligible on real plans)",
        "sql_certificate_over_page": d["sql_certificate"]
        ["certificate_over_page"],
        "denominators": "medians over repeated in-process calls on the "
                        "stated host; certificate ratio = COUNT-wrapped "
                        "query time / page query time on the superuser "
                        "replica's distinct-src shape",
    }


def oracle_v3() -> dict:
    """Reads the v3.1 inventories when present, else v3 — the fixed
    universe and split hashes are identical by construction (D-116);
    v3.1 adds gold_source and the labeled residue."""
    root = Path("benchmarks/oracle-v3.1")
    if not (root / f"suite-{DATASETS[0]}.json").exists():
        root = Path("benchmarks/oracle-v3")
    out = {"schema": None}
    for ds in DATASETS:
        s = _load(root / f"suite-{ds}.json")
        recs = s["records"]
        out["schema"] = s.get("schema", "oracle-v3")
        by_status = collections.Counter(r["oracle_status"] for r in recs)
        resolved = by_status["resolved"]
        rule = sum(1 for r in recs
                   if r.get("gold_source") == "empty_result_rule")
        # answerable-but-not-admitted = the ORACLE LANE resolved real gold
        # where production yielded none (the D-091 lost class). Empty-rule
        # resolutions don't qualify: their production runs failed on the
        # same empty window, which is not a policy shadow. v3 files carry
        # no gold_source; there, resolved + non-admitted implies the
        # oracle lane by construction.
        key = sum(1 for r in recs
                  if (r.get("production_admission") or {}).get("outcome")
                  in ("refused", "timeout", "failed")
                  and r["oracle_status"] == "resolved"
                  and r.get("gold_source", "oracle") == "oracle")
        out[ds] = {"records": len(recs), "resolved": resolved,
                   "resolved_by_empty_rule": rule,
                   "budget_exceeded": by_status.get("budget_exceeded", 0),
                   "oracle_unsupported": by_status.get(
                       "oracle_unsupported", 0),
                   "not_attempted": by_status.get("not_attempted", 0),
                   "resolution_coverage": round(resolved / len(recs), 3),
                   "answerable_not_admitted": key,
                   "test_split_sha": s["test_split_sha"][:16]}
    out["denominators"] = ("records = fixed 116-draw universe per dataset "
                           "(every draw, incl. not-applicable templates); "
                           "coverage = resolved/all records — under v3.1 "
                           "'resolved' includes empty-rule resolutions, "
                           "which stay OUT of the LLM suites")
    return out


def _level_excludes(ds: str, lvl: str) -> set[str] | None:
    """The level's excluded operators, from the campaign config that ran
    it — expressibility at a level is 'the oracle plan touches no
    excluded op', the same rule as the D-107 oracle-plan-ops check."""
    p = Path("configs/campaign") / f"m6-{ds}-{lvl}.yaml"
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("exclude_ops:"):
            body = line.split(":", 1)[1].strip()
            return {s.strip().strip("'\"") for s in
                    body.strip("[]").split(",") if s.strip()}
    return set()


def m6_frontier(runs: Path) -> dict | None:
    lvl_ops = {"a1": 5, "a2": 11, "a3": 13, "a4": 15}
    if not (runs / "m6-sx-mathoverflow-a1" / "results").exists():
        return None
    suites = {}
    for ds in DATASETS:
        s = _load(Path("benchmarks/oracle-v3") / f"suite-{ds}.json")
        for t in s["dev"] + s["test"]:
            suites[t["id"]] = {st["op"] for st in
                              (t.get("oracle_plan") or {}).get("steps", [])}
    out = {}
    for lvl in ("a1", "a2", "a3", "a4", "a4p"):
        rows, expr_rows = [], []
        for ds in DATASETS:
            ds_rows = [json.loads(Path(f).read_text()) for f in glob.glob(
                str(runs / f"m6-{ds}-{lvl}" / "results" / "*.json"))]
            ds_rows = [r for r in ds_rows if r.get("system") == "ours"]
            rows += ds_rows
            excl = _level_excludes(ds, lvl)
            if excl is not None:
                expr_rows += [r for r in ds_rows
                              if not (suites.get(r["task_id"], set()) & excl)]
        if not rows:
            continue
        n = len(rows)
        em = sum(r.get("em") or 0 for r in rows) / n
        out[lvl] = {"n": n, "ops": lvl_ops.get(lvl, 15),
                    "em": round(em, 4),
                    "first_plan_valid": round(
                        sum(1 for r in rows
                            if r.get("first_emission_valid")) / n, 3),
                    "exec_ok": round(
                        sum(1 for r in rows if r.get("executed_ok")) / n,
                        3)}
        if expr_rows:
            out[lvl]["n_expressible"] = len(expr_rows)
            out[lvl]["em_given_expressible"] = round(
                sum(r.get("em") or 0 for r in expr_rows) / len(expr_rows), 4)
        confusions = collections.Counter()
        for r in rows:
            want = suites.get(r["task_id"], set())
            for op in set(r.get("plan_ops") or []) - want:
                confusions[op] += 1
        out[lvl]["agg_series_confusions"] = sum(
            v for k, v in confusions.items()
            if k in ("aggregate_events", "graph_metric_timeseries"))
    out["denominators"] = ("dev split, ours only, seed 0; em over all "
                           "rows at the level; em_given_expressible over "
                           "rows whose oracle plan touches no op excluded "
                           "at the level (D-107 oracle-plan-ops rule); "
                           "a4p = A4 + disambiguated descriptions, all "
                           "else identical")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    args = ap.parse_args()
    commit = os.environ.get("COMMIT", "")
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = "unknown"
    payload = {
        "fault_matrix": fault_matrix(),
        "frozen_2x2": frozen_2x2(),
        "model_axis_robustness": model_axis(),
        "guardrail": guardrail(),
        "overhead": overhead(),
        "oracle_v3": oracle_v3(),
        "m6_frontier": m6_frontier(args.runs)
        or "requires runs/ (regenerate on the eval host)",
        "manifest": {"commit": commit, "host": platform.node()},
    }
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
