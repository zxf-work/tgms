#!/usr/bin/env python3
"""Prove the two sides of the comparison bound the SAME parameters.

The Neo4j runner reads `benchmarks/ldbc-ref-v1/params.json`. The TGIR runner
does not read it — it re-binds from the params *root* with the same seed and
the same corpus. RUNBOOK.md §5.1's guarantee ("a disagreement can never be
attributed to a parameter drawn differently between the two runs being
compared") therefore rests on determinism, and determinism is checkable:
every plan's recorded TGIR params must equal params.json's `tgir` half, and
every id must decode to the id params.json handed Cypher.

Exit 1 on any mismatch — a mismatch invalidates every comparison in the run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from ldbc_snb_params import ID_HIERARCHY  # noqa: E402

from tgms.data.snb_loader import uid_to_ldbc_id  # noqa: E402

OUT = Path("benchmarks/ldbc-ref-v1")
params = json.loads((OUT / "params.json").read_text())
campaign = json.loads((OUT / "tgms-campaign-ldbc-ref-v1.json").read_text())

bad: list[str] = []
checked = 0
for rec in campaign["records"]:
    pid = rec["plan_id"]
    row = params["rows"].get(pid)
    if row is None or "error" in row:
        bad.append(f"{pid}: absent or failed in params.json")
        continue
    if "params" not in rec:
        bad.append(f"{pid}: no bound params in the campaign record "
                   f"(outcome {rec.get('outcome')}: {rec.get('error')})")
        continue

    # 1. the TGIR halves must be identical
    if rec["params"] != row["tgir"]:
        bad.append(f"{pid}: TGIR params differ\n"
                   f"    campaign: {rec['params']}\n"
                   f"    params.json: {row['tgir']}")
        continue

    # 2. every id the two sides hold must be the same entity
    for plan_key, value in rec["params"].items():
        if plan_key not in ID_HIERARCHY:
            continue
        tgir_ldbc_id = uid_to_ldbc_id(value)
        # the Cypher side files ids under the name its query reads: the
        # plan_key for the Interactive arm, the ldbc_key for BI.
        cand = [v for k, v in row["cypher"].items()
                if isinstance(v, int) and v == tgir_ldbc_id]
        if not cand:
            bad.append(
                f"{pid}: TGIR {plan_key}={value} decodes to LDBC id "
                f"{tgir_ldbc_id}, which appears nowhere in the Cypher side "
                f"{row['cypher']} — the two sides queried different entities")
    checked += 1

print(f"checked {checked} plans against params.json")
for b in bad:
    print("  MISMATCH " + b)
print("PARAMS_AGREE " + ("PASS" if not bad else "FAIL"))
raise SystemExit(1 if bad else 0)
