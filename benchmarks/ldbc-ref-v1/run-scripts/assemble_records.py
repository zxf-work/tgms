#!/usr/bin/env python3
"""Assemble the ldbc-ref-v1 records: the per-template timing table (both
sides, same protocol) and the §9 manifest.

Reads only what the run itself produced; computes nothing it cannot cite.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path("/mnt/project/xzhang/tgms/work/tgms")
OUT = ROOT / "benchmarks/ldbc-ref-v1"
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-17"

TEMPLATES = ["BI3", "BI4", "BI6", "BI7", "BI9", "BI10", "BI11", "BI12",
             "BI17", "BI18", "IC2", "IC5", "IC6", "IC8", "IC9", "IC11",
             "IC12", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"]
#: the id whose TGIR plan artifact answers each template (RUNBOOK §6)
TGIR_ID = {t: t for t in TEMPLATES} | {"BI6": "BI6.v2"}

REP_DIRS = {"warmup": "ref-rows-warmup", "t1": "ref-rows",
            "t2": "ref-rows-t2", "t3": "ref-rows-t3"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def main() -> int:
    campaign = load(OUT / "tgms-campaign.json")
    tgms = {r["plan_id"]: r for r in campaign["records"]} if campaign else {}
    compare = load(OUT / f"compare-{DATE}.json")
    verdicts = {v["plan_id"]: v for v in compare["verdicts"]} if compare else {}
    gate = load(OUT / "same-data-gate.json")

    rows = []
    for t in TEMPLATES:
        ref = {rep: load(OUT / d / f"ref-{t}.json")
               for rep, d in REP_DIRS.items()}
        t1 = ref["t1"]
        tg = tgms.get(TGIR_ID[t], {})
        row = {
            "template": t,
            "tgir_plan": TGIR_ID[t] + ".json",
            "neo4j": {
                "rows": len(t1["rows"]) if t1 and "rows" in t1 else None,
                "error": (t1 or {}).get("error"),
                "query_file": (t1 or {}).get("query_file"),
                "query_sha256": (t1 or {}).get("query_sha256"),
                "result_digest": (t1 or {}).get("result_digest"),
                "wall_s": {rep: (r or {}).get("wall_s")
                           for rep, r in ref.items()},
            },
            "tgir": {
                "rows": tg.get("rows"),
                "outcome": tg.get("outcome"),
                "bypassed": tg.get("bypassed"),
                "ms": tg.get("ms"),
                "ms_all": tg.get("ms_all"),
                "error": tg.get("error"),
            },
            "compare": {
                k: verdicts.get(TGIR_ID[t], {}).get(k)
                for k in ("verdict", "compared", "agreeing", "tie_ambiguous",
                          "disagreeing", "causes", "contract", "note")
            },
        }
        rows.append(row)

    # BI6.json's own record is kept as the v1-defect evidence (RUNBOOK §6),
    # reported alongside BI6's row, never as it.
    bi6_v1 = tgms.get("BI6", {})
    timings = {
        "date": DATE,
        "protocol": {
            "neo4j": "1 warm-up + 3 timed executions per template, back to "
                     "back per template; ref-rows/ is the first timed "
                     "execution",
            "tgir": "tgir_ldbc_sf1.py's own per-rep ms (ms_all); the guard "
                    "bypass is on, per RUNBOOK §5.2",
            "query_timeout_s": 600,
        },
        "templates": rows,
        "bi6_v1_defect_evidence": {
            "plan_artifact": "BI6.json",
            "outcome": bi6_v1.get("outcome"),
            "error": bi6_v1.get("error"),
            "rows": bi6_v1.get("rows"),
            "ms": bi6_v1.get("ms"),
        },
    }
    (OUT / f"timings-{DATE}.json").write_text(
        json.dumps(timings, indent=2, sort_keys=True, default=str) + "\n")

    # ---- §9 manifest -----------------------------------------------------
    conf = Path("/mnt/project/xzhang/neo4j/neo4j-community-5.26.0/conf/neo4j.conf")
    importlog = Path("/mnt/project/xzhang/neo4j/import.log")
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "scripts", "tests"],
                           cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        sha += "-dirty"

    result_digest = hashlib.sha256(
        json.dumps(sorted(compare["verdicts"], key=lambda v: v["plan_id"]),
                   sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest() if compare else None

    manifest = {
        "schema_version": "1.0.0",
        "git_commit": sha,
        "timestamp_utc": timings.get("started_utc", DATE + "T00:00:00Z"),
        "machine": {"host": "xzgpu", "platform": platform.platform(),
                    "cpus": 40, "ram_gb": 93},
        "config": {
            "neo4j_version": "5.26.0",
            "neo4j_conf_sha256": sha256_file(conf),
            "apoc_version": "5.26.0-core",
            "jdk": "Temurin 21.0.12+8",
            "neo4j_driver": "neo4j-python 5.28.6",
            "bolt": "bolt://127.0.0.1:7687",
            "import_log_sha256": (sha256_file(importlog)
                                  if importlog.exists() else None),
            "params_file": "benchmarks/ldbc-ref-v1/params.json",
            "contracts_file": "tests/fixtures/ldbc_ref/contracts.json",
            "sort_keys_file": "benchmarks/ldbc-ref-v1/sort_keys.yaml",
        },
        "seed": {"value": 5724984519806421702, "reason": None},
        "dataset": {
            "name": "ldbc-sf1-bi-composite-projected-fk",
            "digest": gate["gate_digest_sha256"] if gate else None,
            "digest_kind": "manifest",
        },
        "result_digest": result_digest,
        "protocol": {"warmups": 1, "reps": 3,
                     "ceilings": {"query_timeout_s": 600},
                     "note": "RUNBOOK §9 specifies warmups 0 / reps 1 for the "
                             "correctness comparison; the coordinator's brief "
                             "additionally requires per-template wall times on "
                             "both sides, so each template ran 1 warm-up + 3 "
                             "timed executions. The compared rows are the "
                             "first timed execution's — the correctness "
                             "protocol is unchanged, only re-executed."},
        "record": f"benchmarks/ldbc-ref-v1/compare-{DATE}.json",
    }
    (OUT / f"manifest-{DATE}.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"wrote timings-{DATE}.json and manifest-{DATE}.json")
    for r in rows:
        n, g = r["neo4j"], r["tgir"]
        print(f"{r['template']:5s} neo4j_rows={str(n['rows']):>6s} "
              f"tgir_rows={str(g['rows']):>6s} "
              f"verdict={str(r['compare']['verdict']):>14s} "
              f"neo4j_s={n['wall_s']['t1']}/{n['wall_s']['t2']}/{n['wall_s']['t3']} "
              f"tgir_ms={g['ms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
