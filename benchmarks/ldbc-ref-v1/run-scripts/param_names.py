#!/usr/bin/env python3
"""What each vendored query actually asks for, vs what params.json supplies."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import ldbc_snb_params as P  # noqa: E402

doc = json.loads(Path("benchmarks/ldbc-ref-v1/params.json").read_text())
BI = Path("external_workloads/ldbc/bi/neo4j/queries")
IV = Path("external_workloads/ldbc/interactive_v1/cypher/queries")

import ldbc_reference_run as R  # noqa: E402

for pid in sorted(doc["rows"]):
    row = doc["rows"][pid]
    if "error" in row:
        continue
    base = pid[:-3] if pid.endswith(".v2") else pid
    d = BI if base.startswith("BI") else IV
    try:
        f = d / R._default_cypher_name(base)
    except KeyError:
        continue
    if not f.exists():
        continue
    wanted = sorted(set(re.findall(r"\$(\w+)", f.read_text())))
    supplied = sorted(row["cypher"])
    plan_keys = sorted(row["tgir"])
    missing = [w for w in wanted if w not in supplied]
    print("%-7s query=%-28s wants=%-34s supplied=%-40s plan_keys=%-34s %s"
          % (pid, f.name, wanted, supplied, plan_keys,
             "MISSING:" + str(missing) if missing else "ok"))
