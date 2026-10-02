"""`python -m neo4j_recompute.cli` — run N1's recompute protocol on one
exported cell. Defaults to **smoke mode**: `--max-epochs` capped at 5 and
`nice`d (memo/pre-registration: "development smoke runs are untimed and
nice 19"). The full 20/5-epoch timed grid needs `--timed` *and* the
coordinator's go-ahead recorded out of band — this flag does not itself
grant that permission, it only removes the smoke cap once given it, since
software cannot know the campaign's run order (X1 -> T1 -> N1 -> D1 -> C1,
Addendum EXT-A) has actually been honored.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cell_dir", type=Path, help="exported cell bundle directory "
                   "(artifacts.jsonl, deltas.jsonl, oracle.jsonl, versions-epoch0.jsonl)")
    p.add_argument("--bolt-uri", default="bolt://127.0.0.1:7687")
    p.add_argument("--database", default="neo4j")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--host", default=os.environ.get("HOSTNAME", "unknown"))
    p.add_argument("--max-epochs", type=int, default=None,
                   help="cap on bursts applied (smoke default: 5)")
    p.add_argument("--timed", action="store_true",
                   help="lift the smoke cap — only after the coordinator's "
                        "go-ahead (memo §4.6); this flag alone is not that "
                        "go-ahead")
    p.add_argument("--git-commit", default="unknown")
    args = p.parse_args(argv)

    if not args.timed and args.max_epochs is None:
        args.max_epochs = 5
    if args.timed and os.nice(0) < 19:
        print("refusing --timed: process niceness is not >= 19's effect "
              "(run under `nice -n 19` for smoke; the real timed grid runs "
              "without nice per the host protocol, but only after the "
              "coordinator's word — this CLI cannot verify that, so it "
              "insists on the smoke posture by default)", file=sys.stderr)

    from neo4j import GraphDatabase  # local import: only needed to actually run

    from .record import build_result, machine_snapshot, setup_run_log, write_result
    from .runner import run_cell

    out_dir = args.out_dir
    logger = setup_run_log(out_dir)
    logger.info("cell_dir=%s bolt_uri=%s max_epochs=%s timed=%s",
               args.cell_dir, args.bolt_uri, args.max_epochs, args.timed)

    driver = GraphDatabase.driver(args.bolt_uri, auth=None)

    def session_factory():
        return driver.session(database=args.database)

    try:
        rows = run_cell(session_factory, args.cell_dir, args.max_epochs,
                        on_burst=lambda r: logger.info("epoch=%d apply_ms=%.1f "
                                                       "recompute_ms=%.1f agree=%d "
                                                       "disagree=%s not_answered=%s",
                                                       r.epoch, r.apply_ms, r.recompute_ms,
                                                       r.agree, r.disagree, r.not_answered))
    finally:
        driver.close()

    manifest_path = args.cell_dir / "export-manifest.json"
    import json as _json
    manifest = _json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    result = build_result(
        cell_id=manifest.get("cell_id", args.cell_dir.name),
        cell_digest=manifest.get("cell_digest", ""),
        equality_level=manifest.get("equality_level", "unknown"),
        export_manifest_sha256="",
        git_commit=args.git_commit,
        timestamp_utc=_utc_now(),
        machine=machine_snapshot(args.host),
        rows=rows,
        ceilings={"db.transaction.timeout_s": 600},
    )
    path = write_result(out_dir, result)
    logger.info("wrote %s", path)
    return 0


def _utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
