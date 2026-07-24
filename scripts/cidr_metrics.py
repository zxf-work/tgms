#!/usr/bin/env python3
"""CIDR RQ1/RQ3 metrics from cached per-row results.

RQ3 (guarantee/trade-off): answer coverage, overall accuracy, conditional
accuracy, answer-level unsupported-claim rate — with exact denominators.
RQ1 (interface): first-emission validity, execution success, repairs,
query failure rate, tokens per task.

An answer is *emitted* iff its (post-gating) answer object carries at least
one claim of any type — the arXiv definition (coverage 199/282 on CollegeMsg
ours reproduces). UCR is reported with two denominators: emitted answers,
and the tighter subset carrying at least one gated-type claim
(count/value/entity/ordering).

Usage: python3 scripts/cidr_metrics.py runs/test-collegemsg-main [more dirs...]
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict

GATED = {"count", "value", "entity", "ordering"}


def emitted(row: dict) -> bool:
    obj = row.get("answer_object") or {}
    return bool(obj.get("claims"))


def emitted_gated(row: dict) -> bool:
    obj = row.get("answer_object") or {}
    return any(c.get("type") in GATED for c in obj.get("claims") or [])


def fmt(x, nd=3):
    return "--" if x is None else f"{x:.{nd}f}"


def main(dirs: list[str]) -> None:
    for d in dirs:
        rows_by = defaultdict(list)
        for f in glob.glob(f"{d}/results/*.json"):
            r = json.load(open(f))
            if r.get("task_error"):
                continue  # infra rows are not results
            rows_by[r.get("system")].append(r)
        print(f"\n== {d} ==")
        for sys_name in sorted(rows_by):
            rows = rows_by[sys_name]
            n = len(rows)
            em_all = sum(r.get("em") or 0 for r in rows) / n
            emit = [r for r in rows if emitted(r)]
            cov = len(emit) / n
            cond = (sum(r.get("em") or 0 for r in emit) / len(emit)
                    if emit else None)
            # answer-level UCR (ours-family only: needs verifier verdicts)
            ucr_known = [r for r in rows if r.get("ucr") is not None]
            ucr = (sum(1 for r in ucr_known if r["ucr"] > 0) / len(ucr_known)
                   if ucr_known else None)
            gate = [r for r in rows if emitted_gated(r)]
            ucr_gate = [r for r in gate if r.get("ucr") is not None]
            n_bad_gate = sum(1 for r in ucr_gate if r["ucr"] > 0)
            fev = [r for r in rows if r.get("first_emission_valid") is not None]
            fev_rate = (sum(bool(r["first_emission_valid"]) for r in fev)
                        / len(fev) if fev else None)
            ex = [r for r in rows if r.get("executed_ok") is not None]
            ex_rate = (sum(r["executed_ok"] or 0 for r in ex) / len(ex)
                       if ex else None)
            metas = [r.get("meta") or {} for r in rows]
            reps = [m["repairs"] for m in metas if "repairs" in m]
            fails = [m["failed"] for m in metas if "failed" in m]
            first_ok = (sum(1 for x in reps if x == 0) / len(reps)
                        if reps else None)
            fail_rate = sum(map(bool, fails)) / len(fails) if fails else None
            tok = sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                      for r in rows) / n
            print(f"  {sys_name:14s} n={n:4d} acc={em_all:.3f} "
                  f"coverage={cov:.3f} ({len(emit)}/{n}) "
                  f"cond_acc={fmt(cond)} "
                  f"ucr_ans={fmt(ucr)} ({sum(1 for r in ucr_known if r['ucr']>0)}"
                  f"/{len(ucr_known)}; gated {n_bad_gate}/{len(ucr_gate)}) "
                  f"fev={fmt(fev_rate)} exec={fmt(ex_rate)} "
                  f"first_query_ok={fmt(first_ok)} qfail={fmt(fail_rate)} "
                  f"tok/task={tok:.0f}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["runs/test-collegemsg-main"])
