"""`result.json` (memo §2.8/§4.1 record layout, one file per cell) and
`run.log`. The merged multi-cell record (`benchmarks/external-v1/
neo4j-recompute-<date>.json` + `-rows.jsonl`, sha-gated) is lane C1's job
(memo §4.4: "C1 | merge -> records, READMEs, macros"); this module writes
only the per-cell artifact N1 itself is responsible for.
"""
from __future__ import annotations

import json
import logging
import platform
from pathlib import Path
from typing import Any

from .canon import sha256_hex
from .queries import FAMILY_TEXT
from .runner import BurstRow
from .schema import DDL_STATEMENTS, conf_lines

SCHEMA_VERSION = "1.0.0"
NEO4J_VERSION = "5.26.0"
APOC_VERSION = "5.26.0-core"
JDK_VERSION = "Temurin 21.0.12"
DRIVER_VERSION = "5.28.6"


def build_result(cell_id: str, cell_digest: str, equality_level: str,
                 export_manifest_sha256: str, git_commit: str,
                 timestamp_utc: str, machine: dict[str, Any],
                 rows: list[BurstRow], ceilings: dict[str, Any],
                 conf_path: Path | None = None) -> dict[str, Any]:
    """Assemble one cell's `result.json`. Matches
    `benchmarks/schema/result_manifest.schema.json`'s required top level
    (`schema_version, git_commit, timestamp_utc, machine, config, seed,
    dataset, result_digest, protocol, record`) plus the memo's own
    `per_cell`/`gates`/`predictions_measured` fields.
    """
    per_burst = [r.to_json() for r in rows]
    total_agree = sum(r.agree for r in rows)
    total_disagree = sum(len(r.disagree) for r in rows)
    total_not_answered = sum(len(r.not_answered) for r in rows)

    conf_sha256 = sha256_hex(conf_path.read_text()) if conf_path and conf_path.exists() else None

    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": machine,
        "config": {
            "neo4j_version": NEO4J_VERSION, "apoc_version": APOC_VERSION,
            "jdk_version": JDK_VERSION, "driver_version": DRIVER_VERSION,
            "ddl": list(DDL_STATEMENTS),
            "cypher": FAMILY_TEXT,
            "neo4j_conf_sha256": conf_sha256,
            "export_manifest_sha256": export_manifest_sha256,
            "ceilings": ceilings,
        },
        "seed": {"value": None, "reason": "cell identity carries the seed; see cell_id"},
        "dataset": {"name": cell_id, "digest": cell_digest, "digest_kind": "eventlog_sha"},
        "result_digest": sha256_hex(json.dumps(per_burst, sort_keys=True)),
        "protocol": {
            "warmups": 1, "reps": 1, "ceilings": ceilings,
            "description": "epoch-0 pass is the warmup (also epoch 0's correctness "
                           "check); one recompute pass per burst thereafter "
                           "(bursts are stateful; seeds are the replication) — "
                           "memo §2.5",
        },
        "record": f"benchmarks/external-v1/per-cell/{cell_id}/result.json",
        "cell_id": cell_id, "cell_digest": cell_digest, "equality_level": equality_level,
        "per_cell": {
            "per_burst": per_burst,
            "recompute_ms_median": _median([r.recompute_ms for r in rows[1:]]),
            "apply_ms_median": _median([r.apply_ms for r in rows[1:]]),
        },
        "gates": {
            "oracle_agreement": {
                "agree": total_agree, "disagree": total_disagree,
                "not_answered": total_not_answered,
                "pass": total_disagree == 0,
            },
        },
        "predictions_measured": {},  # coordinator/C1 fills verdicts, not this lane
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def write_result(out_dir: Path, result: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "result.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return path


def setup_run_log(out_dir: Path, name: str = "run.log") -> logging.Logger:
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"neo4j_recompute.{out_dir}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(out_dir / name)
    fh.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fh.formatter)
    logger.addHandler(sh)
    return logger


def machine_snapshot(host: str) -> dict[str, Any]:
    import os
    return {
        "host": host,
        "platform": platform.platform(),
        "cpus": os.cpu_count() or 1,
        "ram_gb": _ram_gb(),
    }


def _ram_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except FileNotFoundError:
        pass
    return 0.0


__all__ = ["build_result", "conf_lines", "machine_snapshot", "setup_run_log", "write_result"]
