#!/usr/bin/env python
"""Predeclared claim-form annotation of the frozen BIRD 500 (D-129).

The mapping protocol is FROZEN in external_workloads/FREEZE.md before
any agent run. Inputs per record: the natural-language question, the
gold SQL AST (sqlglot), and the gold result shape from
gold_validation.jsonl. Agent output and verifier results are NEVER
inputs to this stage.

Interpretation 1 (declared): the gold query, including its ORDER BY,
semantic LIMIT, grouping, and aggregates, is the trusted semantic
query Q under A1. The claim form classifies the ANSWER SHAPE relative
to R*(Q); the relational machinery used to compute it is recorded
separately as `semantic_property`.

Rule order (first match wins; AUTO or QUEUE for adjudication):
  R1 arity-1 single-row results:
     R1a outer projection is COUNT(...) and wording is a how-many /
         number-of question                      -> EXACT_COUNT
     R1b otherwise                               -> SCALAR
  R2 single-row multi-column results             -> SCALAR_TUPLE
     (one Scalar claim per cited column; in-fragment)
  R3 multi-row results                           -> COMPLETE_SET
     (set semantics; ordering assertions go to semantic_property)
  R4 yes/no wording with 0/1-row boolean shape   -> EXISTS / NOT_EXISTS
  R5 wording/shape mismatch or none of the above -> QUEUE
     (manual adjudication by two annotators; disagreements recorded)

semantic_property (from AST, independent of claim form):
  plain | aggregate | grouped | ranked_extremal (ORDER BY + LIMIT 1)
  | top_k (ORDER BY + LIMIT k>1) | set_op | windowed

    python external_workloads/scripts/annotate_bird_sql.py \
        --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
        --gold external_workloads/bird/gold_validation.jsonl \
        --out external_workloads/bird/claim_annotation.jsonl \
        --receipt benchmarks/results-v1/eval-bird-annotation.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

HOWMANY = re.compile(r"\b(how many|number of|total number|count of)\b",
                     re.I)
YESNO = re.compile(r"^\s*(is|are|was|were|does|do|did|has|have|can)\b",
                   re.I)


def ast_features(sql: str) -> dict:
    tree = sqlglot.parse_one(sql, read="sqlite")
    feats = {
        "parse_ok": True,
        "aggregates": sorted({f.key.upper() for f in tree.find_all(
            exp.AggFunc)}),
        "has_group_by": tree.find(exp.Group) is not None,
        "has_order_by": tree.find(exp.Order) is not None,
        "has_window": tree.find(exp.Window) is not None,
        "has_set_op": any(tree.find_all(exp.Union, exp.Intersect,
                                        exp.Except)),
        "has_distinct": tree.find(exp.Distinct) is not None,
        "n_subqueries": sum(1 for _ in tree.find_all(exp.Subquery)),
    }
    lim = tree.args.get("limit") or tree.find(exp.Limit)
    feats["limit"] = None
    if lim is not None:
        try:
            feats["limit"] = int(lim.expression.name)
        except Exception:
            feats["limit"] = -1
    outer = tree
    while isinstance(outer, (exp.Subquery,)):
        outer = outer.this
    sels = outer.expressions if isinstance(outer, exp.Select) else []
    feats["n_projections"] = len(sels)
    feats["outer_count"] = bool(
        sels and isinstance(sels[0].unalias()
                            if hasattr(sels[0], "unalias") else sels[0],
                            exp.Count)
        or (sels and sels[0].find(exp.Count) is not None
            and len(list(sels[0].find_all(exp.AggFunc))) == 1
            and feats["n_projections"] == 1))
    return feats


def semantic_property(f: dict) -> str:
    if f.get("has_window"):
        return "windowed"
    if f.get("has_set_op"):
        return "set_op"
    if f.get("has_order_by") and f.get("limit") == 1:
        return "ranked_extremal"
    if f.get("has_order_by") and (f.get("limit") or 0) > 1:
        return "top_k"
    if f.get("has_group_by"):
        return "grouped"
    if f.get("aggregates"):
        return "aggregate"
    return "plain"


def classify(question: str, f: dict, cols: int, rows: int):
    howmany = bool(HOWMANY.search(question))
    yesno = bool(YESNO.match(question))
    if rows == 1 and cols == 1:
        if yesno:
            return "QUEUE", "yes/no wording with scalar shape"
        if f.get("outer_count") and howmany:
            return "EXACT_COUNT", "R1a"
        if f.get("outer_count") and not howmany:
            return "QUEUE", "COUNT projection without how-many wording"
        if howmany and not f.get("outer_count"):
            return "QUEUE", "how-many wording without COUNT projection"
        return "SCALAR", "R1b"
    if rows == 1 and cols > 1:
        if yesno or howmany:
            return "QUEUE", "wording/shape mismatch (multi-col)"
        return "SCALAR_TUPLE", "R2"
    if rows > 1:
        if yesno or howmany:
            return "QUEUE", "wording/shape mismatch (multi-row)"
        return "COMPLETE_SET", "R3"
    # rows == 0 never occurs in this gold set (receipt: 0 empty)
    return "QUEUE", "empty gold result"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()

    frozen = {json.loads(l)["question_id"]: json.loads(l)
              for l in open(args.frozen)}
    gold = {json.loads(l)["question_id"]: json.loads(l)
            for l in open(args.gold)}

    out_rows, counts, sem_counts, queue = [], {}, {}, 0
    for qid, rec in sorted(frozen.items()):
        g = gold[qid]
        row = {"question_id": qid, "db_id": rec["db_id"]}
        try:
            f = ast_features(rec["gold_sql"])
        except Exception as e:
            f = {"parse_ok": False, "error": str(e)[:120]}
        row["ast"] = f
        if not f.get("parse_ok"):
            claim, rule = "QUEUE", "gold SQL failed to parse"
        elif g["status"] != "GOLD_VALID":
            claim, rule = "GOLD_INVALID", g["status"]
        else:
            claim, rule = classify(rec["question"], f,
                                   len(g.get("columns", [])),
                                   g.get("row_count", 0))
        row["auto_claim"] = claim
        row["rule"] = rule
        row["semantic_property"] = (semantic_property(f)
                                    if f.get("parse_ok") else "unparsed")
        counts[claim] = counts.get(claim, 0) + 1
        sem_counts[row["semantic_property"]] = \
            sem_counts.get(row["semantic_property"], 0) + 1
        if claim == "QUEUE":
            queue += 1
        out_rows.append(row)

    with open(args.out, "w") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    receipt = {"host": platform.node(), "n": len(out_rows),
               "auto_claim_counts": counts,
               "semantic_property_counts": sem_counts,
               "adjudication_queue": queue}
    commit = os.environ.get("COMMIT", "")
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = "unknown"
    receipt["commit"] = commit
    args.receipt.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
