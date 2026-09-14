#!/usr/bin/env python
"""Validate BIRD Mini-Dev gold SQL against the frozen SQLite package.

Fixed-universe rule (D-129): every one of the 500 frozen records gets
a terminal status; nothing is replaced or dropped. Gold SQL executes
VERBATIM on sqlite3 (the dialect it was written for). Statuses:

    GOLD_VALID              executed, result recorded
    GOLD_EXECUTION_ERROR    sqlite raised; error text recorded
    GOLD_DATABASE_MISSING   db_id has no database file
    GOLD_TIMEOUT            exceeded the per-query ceiling

Per record we keep: status, column names, row count, wall time, and a
canonical SHA-256 digest of the full result (sorted-key JSON of the
row list with columns), so later stages can bind to the gold result
without re-executing.

    python external_workloads/scripts/validate_bird_gold.py \
        --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
        --dbroot external_workloads/bird/package/minidev/MINIDEV/dev_databases \
        --out external_workloads/bird/gold_validation.jsonl \
        --receipt benchmarks/results-v1/eval-bird-gold.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import time
from pathlib import Path

TIMEOUT_S = 600


def canonical_digest(cols, rows):
    def norm(v):
        if isinstance(v, bytes):
            return "0x" + v.hex()
        if isinstance(v, float):
            return repr(v)
        return v
    payload = {"columns": list(cols),
               "rows": [[norm(v) for v in r] for r in rows]}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", type=Path, required=True)
    ap.add_argument("--dbroot", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()

    records = [json.loads(line) for line in open(args.frozen)]
    out_rows, counts = [], {}
    for rec in records:
        db = args.dbroot / rec["db_id"] / f"{rec['db_id']}.sqlite"
        row = {"question_id": rec["question_id"], "db_id": rec["db_id"],
               "difficulty": rec["difficulty"]}
        if not db.exists():
            row["status"] = "GOLD_DATABASE_MISSING"
        else:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.text_factory = lambda b: b.decode("utf-8", "replace")
            deadline = time.time() + TIMEOUT_S
            conn.set_progress_handler(
                lambda: 1 if time.time() > deadline else 0, 100_000)
            t0 = time.perf_counter()
            try:
                cur = conn.execute(rec["gold_sql"])
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchall()
                dt = time.perf_counter() - t0
                digest, n = canonical_digest(cols, rows)
                row.update(status="GOLD_VALID", columns=cols,
                           row_count=n, result_sha256=digest,
                           wall_s=round(dt, 3))
            except sqlite3.OperationalError as e:
                if "interrupted" in str(e).lower():
                    row.update(status="GOLD_TIMEOUT",
                               error=str(e)[:200])
                else:
                    row.update(status="GOLD_EXECUTION_ERROR",
                               error=str(e)[:200])
            except sqlite3.Error as e:
                row.update(status="GOLD_EXECUTION_ERROR",
                           error=str(e)[:200])
            finally:
                conn.close()
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        out_rows.append(row)

    with open(args.out, "w") as f:
        for r in out_rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    empty_valid = sum(1 for r in out_rows
                      if r.get("status") == "GOLD_VALID"
                      and r.get("row_count") == 0)
    receipt = {
        "host": platform.node(),
        "frozen_jsonl_sha256": hashlib.sha256(
            open(args.frozen, "rb").read()).hexdigest(),
        "n_records": len(out_rows),
        "status_counts": counts,
        "gold_valid_empty_result": empty_valid,
        "timeout_s": TIMEOUT_S,
        "sqlite_version": sqlite3.sqlite_version,
    }
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
