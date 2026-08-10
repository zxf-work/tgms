#!/usr/bin/env python
"""Extract the 41 LDBC SNB read templates into a frozen manifest.

Sources: the official query-specification YAMLs in ldbc_snb_docs
(pinned commit in MANIFEST.yaml). Templates: Interactive v1 complex
reads 1-13 plus 14-v1, Interactive short reads 1-7, BI reads 1-20.
Inserts and deletes are excluded: the paper's contract is read-only.

    python external_workloads/scripts/extract_ldbc_templates.py \
        --specs external_workloads/ldbc/docs/query-specifications \
        --out external_workloads/ldbc/ldbc_read_templates.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

FILES = (
    [("interactive-v1", f"interactive-complex-read-{i:02d}.yaml", f"IC{i}")
     for i in range(1, 14)]
    + [("interactive-v1", "interactive-complex-read-14-v1.yaml", "IC14")]
    + [("interactive-v1", f"interactive-short-read-{i:02d}.yaml", f"IS{i}")
       for i in range(1, 8)]
    + [("bi", f"bi-read-{i:02d}.yaml", f"BI{i}")
       for i in range(1, 21)]
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows = []
    for workload, fname, qid in FILES:
        spec = yaml.safe_load((args.specs / fname).read_text())
        rows.append({
            "workload": workload,
            "query_id": qid,
            "title": spec.get("title", ""),
            "description": (spec.get("description") or "").strip(),
            "parameters": [p.get("name") for p in
                           spec.get("parameters") or []],
            "result_schema": [r.get("name") for r in
                              spec.get("result") or []],
            "result_arity": len(spec.get("result") or []),
            "sort": [f"{s.get('name')} {s.get('direction', '')}".strip()
                     for s in spec.get("sort") or []],
            "semantic_limit": spec.get("limit"),
            "choke_points": spec.get("choke_points"),
            "spec_file": fname,
        })
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False)
                    + "\n")
    h = hashlib.sha256(open(args.out, "rb").read()).hexdigest()
    print(f"templates: {len(rows)}  sha256: {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
