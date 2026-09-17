#!/usr/bin/env python3
"""Assemble timings-<date>.json and manifest-<date>.json on the laptop."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path("benchmarks/ldbc-ref-v1")
DATE = "2026-09-17"

TEMPLATES = ["BI3", "BI4", "BI6", "BI7", "BI9", "BI10", "BI11", "BI12",
             "BI17", "BI18", "IC2", "IC5", "IC6", "IC8", "IC9", "IC11",
             "IC12", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"]
TGIR_ID = dict({t: t for t in TEMPLATES}, **{"BI6": "BI6.v2"})
REPS = {"warmup": "ref-rows-warmup", "t1": "ref-rows",
        "t2": "ref-rows-t2", "t3": "ref-rows-t3"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


campaign = load(OUT / "tgms-campaign-ldbc-ref-v1.json")
tgms = {r["plan_id"]: r for r in campaign["records"]}
compare = load(OUT / f"compare-{DATE}.json")
verdicts = {v["plan_id"]: v for v in compare["verdicts"]}
gate = load(OUT / "same-data-gate.json")

rows = []
for t in TEMPLATES:
    pid = TGIR_ID[t]
    ref = {rep: load(OUT / d / f"ref-{t}.json") for rep, d in REPS.items()}
    t1 = ref["t1"] or {}
    tg = tgms.get(pid, {})
    v = verdicts.get(pid, {})
    rows.append({
        "template": t,
        "tgir_plan": pid + ".json",
        "neo4j": {
            "rows": len(t1["rows"]) if "rows" in t1 else None,
            "error": t1.get("error"),
            "query_file": t1.get("query_file"),
            "query_sha256": t1.get("query_sha256"),
            "result_digest": t1.get("result_digest"),
            "wall_s": {r: (x or {}).get("wall_s") for r, x in ref.items()},
        },
        "tgir": {
            "rows": tg.get("rows"), "outcome": tg.get("outcome"),
            "bypassed": tg.get("bypassed"), "ms": tg.get("ms"),
            "ms_all": tg.get("ms_all"), "error": tg.get("error"),
        },
        "compare": {k: v.get(k) for k in
                    ("verdict", "compared", "agreeing", "tie_ambiguous",
                     "disagreeing", "contract")},
    })

bi6 = tgms.get("BI6", {})
timings = {
    "date": DATE,
    "protocol": {
        "neo4j": "1 warm-up + 3 timed executions per template, back to back "
                 "per template; ref-rows/ is the first timed execution. Each "
                 "execution is its own ldbc_reference_run.py invocation "
                 "(fresh driver + session), so wall_s excludes process start "
                 "but includes connection setup.",
        "tgir": "tgir_ldbc_sf1.py's own protocol: warmups 1, reps 3, and 1 rep "
                "when the guard is bypassed (which it is for the BI rows). "
                "`ms` is the reported figure, `ms_all` the per-rep list. Each "
                "plan runs in its own child process, so its campaign wall also "
                "includes a ~150-270 s store open that `ms` excludes.",
        "query_timeout_s": 600,
        "note": "the two sides' wall times are NOT comparable as a speed "
                "ratio: different rep counts, and the TGIR figure excludes a "
                "store open the Neo4j figure has no analogue for (the server "
                "was already up). They are reported per side, never divided.",
    },
    "templates": rows,
    "bi6_v1_defect_evidence": {
        "plan_artifact": "BI6.json", "outcome": bi6.get("outcome"),
        "error": bi6.get("error"), "rows": bi6.get("rows"),
        "note": "RUNBOOK §6: kept as evidence the v1 defect still reproduces "
                "at SF1. The template's own row is BI6.v2.",
    },
}
(OUT / f"timings-{DATE}.json").write_text(
    json.dumps(timings, indent=2, sort_keys=True, default=str) + "\n")

result_digest = hashlib.sha256(json.dumps(
    sorted(compare["verdicts"], key=lambda v: v["plan_id"]),
    sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

manifest = {
    "schema_version": "1.0.0",
    "git_commit": campaign["manifest"].get("commit", "0b7198dfdf7a") + "-dirty",
    "timestamp_utc": "2026-09-17T18:28:47Z",
    "machine": {"host": "xzgpu",
                "platform": campaign["manifest"].get(
                    "platform", "Linux-5.4.0-216-generic-x86_64-with-glibc2.31"),
                "cpus": 40, "ram_gb": 93},
    "config": {
        "neo4j_version": "5.26.0",
        "neo4j_conf_sha256":
            "b22fb1a80eac03284f4d046a7b6c3d4813e153bb2e7bde8e2c3c7b98583a6fac",
        "apoc_version": "5.26.0-core",
        "jdk": "Temurin 21.0.12+8",
        "neo4j_python_driver": "5.28.6",
        "bolt": "bolt://127.0.0.1:7687",
        "import_log_sha256": sha256_file(OUT / "logs/import.log"),
        "params_file": "benchmarks/ldbc-ref-v1/params.json",
        "contracts_file": "tests/fixtures/ldbc_ref/contracts.json",
        "sort_keys_file": "benchmarks/ldbc-ref-v1/sort_keys.yaml",
        "column_kinds_file": "benchmarks/ldbc-ref-v1/column_kinds.json",
        "tgms_store": "stores/snb-sf1",
    },
    "seed": {"value": 5724984519806421702, "reason": None},
    "dataset": {
        "name": "ldbc-sf1-bi-composite-projected-fk",
        "digest": gate["gate_digest_sha256"],
        "digest_kind": "manifest",
    },
    "result_digest": result_digest,
    "protocol": {"warmups": 1, "reps": 3,
                 "ceilings": {"query_timeout_s": 600,
                              "tgir_bypass_ceiling_s": 600,
                              "tgir_child_open_allowance_s": 420},
                 "note": "RUNBOOK §9 specifies warmups 0 / reps 1 for the "
                         "correctness comparison; the coordinator's brief adds "
                         "per-template wall times on both sides, so each "
                         "Neo4j template ran 1 warm-up + 3 timed executions. "
                         "The compared rows are the first timed execution's."},
    "companion": {
        "record": "benchmarks/results-v1/ldbc-sf1-campaign-fmt3-interactive-2026-09.json",
        "relation": "companion, NOT superseded",
        "why": "that record covers the characterization-interactive arm only "
               "(14/14 COMPLETED at 54dcab0); this one covers all 25 plan "
               "artifacts and adds the Neo4j reference comparison. Same seed, "
               "same csv_root, same store totals, same TGIR protocol.",
    },
    "record": f"benchmarks/ldbc-ref-v1/compare-{DATE}.json",
}
(OUT / f"manifest-{DATE}.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(f"wrote timings-{DATE}.json and manifest-{DATE}.json")
print("result_digest", result_digest)
