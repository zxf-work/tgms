#!/usr/bin/env python
"""Seal one blind-corpus submission (M1-A, BLIND_CORPUS_PROTOCOL.md §4).

    python scripts/blind_intake.py benchmarks/blind-v1/raw/c1.txt

Reads a raw submission file, verifies its shape, and appends a receipt
to benchmarks/blind-v1/intake.json: raw-bytes sha256, question count,
per-dataset tag counts, attribution choice, exposure declarations,
receipt date (from the file's mtime — intake runs the day of receipt).

The receipt is the seal: it lets every later session verify integrity
and report corpus size WITHOUT reading question text. Accordingly this
script never prints a question line; validation failures print line
NUMBERS only.

Submission shape (CONTRIBUTOR_PACK.md "How to submit"):
  - question lines start with `1:`, `2:`, `3:` or `any:`
  - one `attribution:` line (named (...) | de-identified)
  - one `exposure:` line and one `software:` line
  - anything else (greetings, signatures) is ignored but counted as
    "other" so a mis-tagged question shows up as a count anomaly.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path("benchmarks/blind-v1")
TAGS = ("1", "2", "3", "any")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", type=Path, help="raw submission file (c<N>.txt)")
    args = ap.parse_args()
    if not args.raw.exists():
        print(f"no such file: {args.raw}", file=sys.stderr)
        return 1
    cid = args.raw.stem
    if not re.fullmatch(r"c\d+", cid):
        print(f"raw file must be named c<N>.txt, got {args.raw.name}",
              file=sys.stderr)
        return 1

    blob = args.raw.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()

    counts = {t: 0 for t in TAGS}
    meta = {"attribution": None, "exposure": None, "software": None}
    other = 0
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(1|2|3|any)\s*:", line, re.IGNORECASE)
        if m:
            counts[m.group(1).lower()] += 1
            continue
        km = re.match(r"^(attribution|exposure|software)\s*:\s*(.+)$",
                      line, re.IGNORECASE)
        if km:
            meta[km.group(1).lower()] = km.group(2).strip()
            continue
        other += 1

    n_q = sum(counts.values())
    problems = []
    if not 12 <= n_q <= 20:
        problems.append(f"question count {n_q} outside 12-20")
    for k, v in meta.items():
        if v is None:
            problems.append(f"missing {k}: line")
    # attribution content is not question text; safe to normalize
    if meta["attribution"] and not re.match(
            r"^(named|de-identified)", meta["attribution"], re.IGNORECASE):
        problems.append("attribution not 'named (...)' or 'de-identified'")

    intake_path = ROOT / "intake.json"
    intake = (json.loads(intake_path.read_text())
              if intake_path.exists() else {"protocol": "M1-A v1.0",
                                            "contributors": {}})
    if cid in intake["contributors"]:
        prev = intake["contributors"][cid]
        if prev["sha256"] != sha:
            print(f"{cid} already sealed with a DIFFERENT hash — refusing "
                  f"to overwrite a seal", file=sys.stderr)
            return 1
        print(f"{cid} already sealed (idempotent re-run)")
        return 0

    intake["contributors"][cid] = {
        "sha256": sha,
        "received": datetime.date.fromtimestamp(
            args.raw.stat().st_mtime).isoformat(),
        "questions": n_q,
        "by_dataset": counts,
        "other_lines": other,
        "attribution": ("de-identified" if meta["attribution"] and
                        meta["attribution"].lower().startswith("de-id")
                        else "named"),
        "exposure": meta["exposure"],
        "software": meta["software"],
        "problems": problems,
    }
    intake_path.parent.mkdir(parents=True, exist_ok=True)
    intake_path.write_text(json.dumps(intake, indent=1, sort_keys=True)
                           + "\n")
    status = "SEALED" if not problems else f"SEALED WITH PROBLEMS {problems}"
    print(f"{cid}: {n_q} questions {counts}, {status}")
    print(f"sha256 {sha}")
    print("Next: commit raw file + intake.json via `git tgi` and append "
          "the sha to docs/DECISIONS.md today.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
