#!/usr/bin/env python3
"""Per-template triage evidence for ldbc-ref-v1.

Diagnostic only. Produces, for each of the 24 templates, the facts a human
needs to classify its verdict — it does NOT decide the verdict, and nothing it
computes is fed back into compare-<date>.json.

For each template it answers:
  A. is the reference side invalid because a temporal parameter reached the
     driver as a string? (detected from params.json, not from the result)
  B. do the two sides declare the same columns? same count? same order?
  C. if the columns correspond positionally, do the row VALUES agree once
     RUNBOOK §4.3's µs/ms rule is applied to the temporal columns?
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

OUT = Path("benchmarks/ldbc-ref-v1")
ISO = "%Y-%m-%dT%H:%M:%S.%f+00:00"

TEMPLATES = ["BI3", "BI4", "BI6.v2", "BI7", "BI9", "BI10", "BI11", "BI12",
             "BI17", "BI18", "IC2", "IC5", "IC6", "IC8", "IC9", "IC11",
             "IC12", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"]

params = json.loads((OUT / "params.json").read_text())["rows"]
kinds = json.loads((OUT / "column_kinds.json").read_text())["plans"]


def temporal_params(pid):
    row = params.get(pid, {})
    out = []
    for k, v in (row.get("cypher") or {}).items():
        if isinstance(v, str):
            try:
                datetime.datetime.strptime(v, ISO)
                out.append(k)
            except ValueError:
                pass
    return out


def norm(rows, cols, ts_cols, side):
    """rows -> list of value-tuples in declared column order, µs on both."""
    out = []
    for r in rows:
        t = []
        for c in cols:
            v = r.get(c)
            if c in ts_cols and isinstance(v, int) and side == "ref":
                v = v * 1000
            t.append(v)
        out.append(tuple(t))
    return out


print(f"{'tpl':7s} {'refparam':9s} {'cols':18s} {'rows t/r':10s} positional-value-agreement")
print("-" * 100)
for pid in TEMPLATES:
    tp, rp = OUT / f"tgms-rows/tgms-{pid}.json", OUT / f"ref-rows/ref-{pid}.json"
    if not tp.exists() or not rp.exists():
        print(f"{pid:7s} {'-':9s} {'-':18s} {'-':10s} NO TGMS-SIDE EXPORT (timeout/error)")
        continue
    t, r = json.loads(tp.read_text()), json.loads(rp.read_text())
    tc, rc = t.get("columns") or [], r.get("columns") or []
    tps = temporal_params(pid)
    ref_invalid = "STRING-DT" if tps else "ok"

    if len(tc) != len(rc):
        colnote = f"COUNT {len(tc)}v{len(rc)}"
    elif tc == rc:
        colnote = "identical"
    elif sorted(tc) == sorted(rc):
        colnote = "same names, reordered"
    else:
        colnote = "NAMES DIFFER"

    k = kinds.get(pid, {})
    ts_t = {c for c, v in k.items() if v == "ts"}
    # positional: the i-th reference column is typed by the i-th TGMS column
    if len(tc) == len(rc):
        ts_r = {rc[i] for i, c in enumerate(tc) if c in ts_t}
        a = sorted(norm(t["rows"], tc, ts_t, "tgms"))
        b = sorted(norm(r["rows"], rc, ts_r, "ref"))
        if a == b:
            verdict = "IDENTICAL under positional+µs/ms"
        else:
            inter = len(set(a) & set(b))
            verdict = (f"differs: {inter}/{max(len(a), len(b))} value-tuples "
                       f"shared (tgms {len(a)} rows, ref {len(b)} rows)")
    else:
        verdict = "not positionally comparable (different projections)"

    print(f"{pid:7s} {ref_invalid:9s} {colnote:18s} "
          f"{len(t['rows'])}/{len(r['rows']):<8} {verdict}")
