#!/usr/bin/env python
"""Bag-versus-set audit of every CompleteSet-encoded BIRD item (D-133).

`CompleteSet(S,f)` compares pi_f(R) as a SET, so it certifies an
unordered, DUPLICATE-INSENSITIVE projection. Where the reference
result repeats a projected row, the set representation is still a
true statement about the result but no longer carries the
multiplicity the result contract may require. This script records,
per item, whether the reference (gold) result is duplicate-free, so
the paper can restrict its full-contract coverage claim to the items
where the question of multiplicity does not arise.

    python external_workloads/scripts/audit_bird_multiplicity.py \
        --db-root <dev_databases> \
        --out external_workloads/bird/multiplicity_audit.jsonl \
        --receipt benchmarks/results-v1/eval-bird-multiplicity.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from run_bird_agent import canonical_json, execute_sql

B = Path("external_workloads/bird")
ENC = {"Scalar": "SCALAR", "Scalar (Boolean)": "SCALAR",
       "Scalar bundle over one row": "SCALAR_TUPLE",
       "CompleteSet": "COMPLETE_SET",
       "CompleteSet (tuple projection)": "COMPLETE_SET",
       "CompleteSet (unordered projection)": "COMPLETE_SET",
       "Existence": "EXISTS", "CompleteSet + ExactCount": "SET_AND_COUNT",
       "OUTSIDE_CURRENT_FRAGMENT": "OUTSIDE_FRAGMENT"}


def forms() -> dict:
    f = {json.loads(l)["question_id"]: json.loads(l)["auto_claim"]
         for l in open(B / "claim_annotation.jsonl")}
    for l in open(B / "annotation_errata.jsonl"):
        r = json.loads(l)
        f[r["question_id"]] = r["to"]
    for l in open(B / "adjudication_final.jsonl"):
        r = json.loads(l)
        f[r["question_id"]] = ENC[r["ecqr_encoding"]]
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()

    frozen = {json.loads(l)["question_id"]: json.loads(l)
              for l in open(B / "bird_500_select_sqlite.jsonl")}
    fm = forms()
    out, dup = [], 0
    for qid, form in sorted(fm.items()):
        if form not in ("COMPLETE_SET", "SET_AND_COUNT"):
            continue
        rec = frozen[qid]
        db = args.db_root / rec["db_id"] / f"{rec['db_id']}.sqlite"
        rows, _c = execute_sql(db, rec["gold_sql"])
        ser = [canonical_json(r) for r in rows]
        distinct = len(set(ser))
        free = distinct == len(ser)
        dup += not free
        out.append({"question_id": qid, "db_id": rec["db_id"],
                    "encoding": form, "reference_rows": len(ser),
                    "reference_distinct_rows": distinct,
                    "duplicate_free_reference": free,
                    "rows_lost_to_set_semantics": len(ser) - distinct})
    with open(args.out, "w") as f:
        for r in out:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    receipt = {
        "n_set_encoded": len(out),
        "duplicate_free": len(out) - dup,
        "duplicate_bearing": dup,
        "max_rows_lost": max((r["rows_lost_to_set_semantics"]
                              for r in out), default=0),
        "rule": "CompleteSet certifies an unordered duplicate-insensitive "
                "projection; a duplicate-bearing reference result means the "
                "full result contract is not covered, and the item is "
                "recorded with full_question_contract_covered = false",
        "commit": subprocess.run(["git", "rev-parse", "HEAD"],
                                 capture_output=True,
                                 text=True).stdout.strip(),
    }
    args.receipt.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
