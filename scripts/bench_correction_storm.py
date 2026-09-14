#!/usr/bin/env python3
"""CLI for the Correction Storm benchmark (Lane C, C1+C3;
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`).

    uv run python scripts/bench_correction_storm.py \\
        --store stores/collegemsg --n-artifacts 50 --batches 5 --seed 0 \\
        --out benchmarks/storm-v1

Writes `<out>/storm-<store>-<seed>.json` (the manifest, arm summary table,
and check-cost curve — `benchmarks/schema/result_manifest.schema.json`'s
required fields, checked in this repo by `scripts/check_result_manifest.py`)
and `<out>/storm-<store>-<seed>-rows.jsonl` (one `BatchResult.to_json()` line
per batch, `manifest["record"]` names it).

**Isolation.** The named store is never written in place: it is copied into
a scratch directory first (`tgms.eval.corrections`/`bench_freshness.py`'s own
"copy the store, inject" discipline, §4.1) — a storm run registers an
artifact population and republishes every one of them every batch, which
would otherwise permanently grow a store this repo tracks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgms.eval.storm import ARMS, Storm, summarize  # noqa: E402

SCHEMA_VERSION = "1.0.0"


def _git_commit() -> str:
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # pragma: no cover - environment without git
        return "0" * 40


def _ram_gb() -> float:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 2)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return 1.0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_json(obj: Any) -> str:
    from tgms.core.model import canonical_json
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _print_table(store_label: str, summary: dict[str, Any]) -> None:
    print(f"\nCorrection Storm — {store_label}")
    header = (f"{'arm':<16}{'false_fresh':>12}{'false_stale':>12}{'invalidated':>12}"
             f"{'avoid_dec':>10}{'avoid_wall':>11}{'ttf_p50_ms':>12}{'ttf_p95_ms':>12}")
    print(header)
    print("-" * len(header))
    for arm in ARMS:
        row = summary["arms"].get(arm)
        if row is None:
            continue

        def fmt(x: float | None) -> str:
            return "-" if x is None else f"{x:.3f}"

        print(f"{arm:<16}{row['false_fresh']:>12}{row['false_stale']:>12}"
             f"{row['invalidated']:>12}{fmt(row['avoided_recompute_decision']):>10}"
             f"{fmt(row['avoided_recompute_wall']):>11}{fmt(row['ttf_p50_ms']):>12}"
             f"{fmt(row['ttf_p95_ms']):>12}")
    print()
    for arm in ARMS:
        row = summary["arms"].get(arm)
        if row is None:
            continue
        if arm.startswith("tgms-") and row["false_fresh"] != 0:
            print(f"** G-S1 VIOLATION: {arm} false_fresh={row['false_fresh']} (must be 0) **")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, help="store directory, or a name under stores/")
    ap.add_argument("--n-artifacts", type=int, default=100)
    ap.add_argument("--batches", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--arms", nargs="*", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--backend", default="native")
    ap.add_argument("--max-attempts-factor", type=int, default=4)
    ap.add_argument("--keep-work-dir", action="store_true",
                    help="do not delete the isolated store copy afterward (debugging)")
    args = ap.parse_args(argv)

    store_path = Path(args.store)
    if not store_path.exists():
        store_path = ROOT / "stores" / args.store
    if not store_path.exists():
        ap.error(f"no such store: {args.store}")
    store_label = store_path.name

    work_root = Path(tempfile.mkdtemp(prefix="storm-"))
    work_store = work_root / "store"
    shutil.copytree(store_path, work_store)

    t_start = time.time()
    storm = Storm(work_store, n_artifacts=args.n_artifacts, seed=args.seed,
                 backend=args.backend, arms=tuple(args.arms))
    n_registered = len(storm.artifacts)
    n_skipped = storm.n_registration_skipped
    results = storm.run(args.batches, max_attempts_factor=args.max_attempts_factor)
    eventlog_digest = _sha256_file(storm.store.eventlog.path)
    storm.close()
    wall_s = time.time() - t_start

    if not args.keep_work_dir:
        shutil.rmtree(work_root, ignore_errors=True)

    args.out.mkdir(parents=True, exist_ok=True)
    base = f"storm-{store_label}-{args.seed}"
    rows_path = args.out / f"{base}-rows.jsonl"
    with open(rows_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_json(), sort_keys=True) + "\n")

    summary = summarize(results)
    rows_json = [r.to_json() for r in results]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                    "cpus": os.cpu_count() or 1, "ram_gb": _ram_gb()},
        "config": {"store": str(args.store), "n_artifacts": args.n_artifacts,
                  "batches": args.batches, "seed": args.seed, "arms": list(args.arms),
                  "backend": args.backend, "n_registered": n_registered,
                  "n_registration_skipped": n_skipped, "wall_s": wall_s},
        "seed": {"value": args.seed},
        "dataset": {"name": store_label, "digest": eventlog_digest,
                   "digest_kind": "eventlog_sha"},
        "result_digest": _sha256_json(rows_json),
        "protocol": {"warmups": 0, "reps": 1, "ceilings": {}},
        "record": str(rows_path.relative_to(ROOT)) if rows_path.is_relative_to(ROOT)
        else str(rows_path),
        "summary": summary,
    }
    manifest_path = args.out / f"{base}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"wrote {manifest_path}")
    print(f"wrote {rows_path}")
    _print_table(store_label, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
