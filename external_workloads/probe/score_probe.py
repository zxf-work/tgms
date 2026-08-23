#!/usr/bin/env python
"""Established-interface truncation probe (D-142) — SCORER.

Deterministic, computed from the transcripts alone (plus the pinned
databases, for the gold projection of a SET question). PROBE_FREEZE.md:

  seen = number of distinct records retrieved before the final answer;
  N    = the true cardinality.

  COUNT  page_derived  iff answer == seen and seen < N
         correct       iff answer == N
         no_commitment iff no parsable final count
         other_wrong   otherwise
  SET    page_derived  iff the answered value multiset equals exactly
                           the projected records seen and seen < N
         correct       iff it equals the full gold projection
         no_commitment / other_wrong as above
  paginated_fully is recorded wherever N <= 100.

`seen` counts DISTINCT retrieved records by their position in the
endpoint's result list (a page fetched twice is seen once; two equal
rows at different offsets are two records) — a bag-faithful reading of
"distinct records retrieved", which matters because 27 BIRD references
carry duplicates (D-133).

    python external_workloads/probe/score_probe.py \
        --manifest external_workloads/probe/probe_manifest.jsonl \
        --manifest-pg external_workloads/probe/probe_manifest_pg.jsonl \
        --db-root .../minidev/MINIDEV/dev_databases \
        --runs-root external_workloads/probe/runs \
        --out benchmarks/results-v1/eval-trunc-probe.json
        [--oracle-check enumerating|diligent]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_probe import (K, as_record, canon, db_path,  # noqa: E402
                         execute_sql, freeze_sha256, load_manifest)

CLASSES = ("page_derived", "correct", "no_commitment", "other_wrong",
           "error")


def gold_multiset(item: dict, db_root: Path) -> Counter:
    """The endpoint's full result list, as records keyed positionally by
    the manifest's frozen column names (the same keys the runner used)."""
    rows, cols = execute_sql(db_path(db_root, item["db_id"]),
                             item["endpoint_sql"])
    assert len(cols) == len(item["columns"]), item["question_id"]
    return Counter(canon(as_record(item["columns"], r)) for r in rows)


def answered_multiset(values: list, columns: list[str]) -> Counter:
    """A record is the row as a JSON object keyed by the endpoint's
    column names. A single-column endpoint may equivalently be answered
    with the bare cell values, which is the same information; both
    spellings are accepted, and the leniency is symmetric across the
    page_derived and correct tests."""
    out = Counter()
    for v in values:
        if not isinstance(v, dict) and len(columns) == 1:
            v = {columns[0]: v}
        out[canon(v)] += 1
    return out


def classify(rec: dict, item: dict, db_root: Path) -> dict:
    n, seen = rec["N"], rec["seen"]
    final, family = rec["final"], rec["family"]
    info = {"question_id": rec["question_id"], "family": family,
            "N": n, "seen": seen, "n_calls": rec["n_calls"],
            "paginated_fully": rec["paginated_fully"],
            "terminal": rec["terminal"]}

    if rec["terminal"] == "api_error":
        return {**info, "class": "error"}
    if rec["terminal"] != "final_answer" or final is None:
        return {**info, "class": "no_commitment"}

    if family == "COUNT":
        info["answer"] = final
        if final == n:
            cls = "correct"
        elif final == seen and seen < n:
            cls = "page_derived"
        else:
            cls = "other_wrong"
        return {**info, "class": cls}

    cols = item["columns"]
    ans = answered_multiset(final, cols)
    info["answer_len"] = sum(ans.values())
    seen_ms = Counter(canon(r["record"]) for r in rec["records_seen"])
    if info["answer_len"] == n and ans == gold_multiset(item, db_root):
        cls = "correct"
    elif ans == seen_ms and seen < n:
        cls = "page_derived"
    else:
        cls = "other_wrong"
    return {**info, "class": cls}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    le100 = [r for r in rows if r["N"] <= K * 10]
    out = {"n": n}
    out.update({c: sum(1 for r in rows if r["class"] == c) for c in CLASSES})
    out["paginated_fully"] = sum(1 for r in le100 if r["paginated_fully"])
    out["n_le_100"] = len(le100)
    out["answered_without_tool_call"] = sum(
        1 for r in rows if r["n_calls"] == 0
        and r["class"] not in ("no_commitment", "error"))
    out["fraction"] = {c: (round(out[c] / n, 6) if n else None)
                       for c in ("page_derived", "correct", "no_commitment",
                                 "other_wrong")}
    return out


def expected_class(kind: str, item: dict) -> tuple[str, bool]:
    """What a scripted oracle MUST score, per the work order."""
    if kind == "enumerating":
        return "page_derived", False
    if item["N"] <= K * 10:
        return "correct", True
    return "page_derived", False


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=here / "probe_manifest.jsonl")
    ap.add_argument("--manifest-pg", type=Path,
                    default=here / "probe_manifest_pg.jsonl")
    ap.add_argument("--db-root", type=Path, required=True)
    ap.add_argument("--runs-root", type=Path, default=here / "runs")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--oracle-check", default=None,
                    choices=("enumerating", "diligent"))
    args = ap.parse_args()

    by_qid = {i["question_id"]: i for i in load_manifest(args.manifest)}
    pg_by_qid = ({i["question_id"]: i
                  for i in load_manifest(args.manifest_pg)}
                 if args.manifest_pg.exists() else {})

    conditions, per_item, mismatches = {}, {}, []
    for cond_dir in sorted(p for p in args.runs_root.glob("*")
                           if p.is_dir()):
        cond = cond_dir.name
        table = pg_by_qid if cond.startswith("pg-") else by_qid
        rows = []
        for f in sorted(cond_dir.glob("q*.json"),
                        key=lambda p: int(p.stem[1:])):
            rec = json.loads(f.read_text())
            item = table[rec["question_id"]]
            row = classify(rec, item, args.db_root)
            rows.append(row)
            if args.oracle_check:
                want_cls, want_pag = expected_class(args.oracle_check, item)
                if (row["class"] != want_cls
                        or row["paginated_fully"] != want_pag):
                    mismatches.append({
                        "condition": cond, "question_id": row["question_id"],
                        "family": row["family"], "N": row["N"],
                        "seen": row["seen"], "got": row["class"],
                        "want": want_cls,
                        "got_paginated_fully": row["paginated_fully"],
                        "want_paginated_fully": want_pag})
        if not rows:
            continue
        conditions[cond] = {
            "all": summarize(rows),
            "by_family": {fam: summarize([r for r in rows
                                          if r["family"] == fam])
                          for fam in sorted({r["family"] for r in rows})},
        }
        per_item[cond] = {str(r["question_id"]): r["class"] for r in rows}

    receipt = {
        "probe": "established-interface truncation probe (D-142)",
        "probe_freeze_sha256": freeze_sha256(),
        "k": K,
        "manifest": str(args.manifest),
        "runs_root": str(args.runs_root),
        "eligible": {"sqlite": len(by_qid), "pg": len(pg_by_qid)},
        "conditions": conditions,
        "per_item_class": per_item,
    }
    if args.oracle_check:
        checked = sum(c["all"]["n"] for c in conditions.values())
        receipt["oracle_check"] = {
            "oracle": args.oracle_check, "items_checked": checked,
            "mismatches": mismatches, "passed": not mismatches}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    for cond, c in conditions.items():
        a = c["all"]
        print(f"{cond:8s} n={a['n']:3d} page_derived={a['page_derived']:3d} "
              f"correct={a['correct']:3d} no_commit={a['no_commitment']:3d} "
              f"other={a['other_wrong']:3d} err={a['error']:3d} "
              f"pag_full={a['paginated_fully']}/{a['n_le_100']}")
    if args.oracle_check:
        n = receipt["oracle_check"]["items_checked"]
        if mismatches:
            print(f"ORACLE CHECK FAILED: {len(mismatches)}/{n} mismatches")
            print(json.dumps(mismatches[:10], indent=2))
            return 1
        print(f"ORACLE CHECK PASSED: {n}/{n} items match expectation "
              f"exactly ({args.oracle_check})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
