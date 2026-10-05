#!/usr/bin/env python3
"""Lane N1 -- timed grid: full per-cell lifecycle for one exported cell
(design memo EXTERNAL_BASELINES_DESIGN_2026-10-02.md secs 2.2/2.5/2.7).

Given one exported cell bundle directory (artifacts.jsonl, deltas.jsonl,
oracle.jsonl, versions-epoch0.jsonl, export-manifest.json) and a fresh,
cell-specific data/logs/conf triple, this script:

  1. writes a fresh neo4j.conf (schema.conf_lines, memo Sec2.7 settings) into
     its own conf dir -- never reused across cells;
  2. CSV-exports the cell's versions-epoch0.jsonl and runs
     `neo4j-admin database import full` into the cell's own data dir;
  3. starts Neo4j (NEO4J_CONF pointed at the cell's conf dir), waits for
     bolt, applies the DDL, awaits indexes online;
  4. runs the full recompute protocol (runner.run_cell: epoch-0 pass, then
     every registered delta burst in the bundle -- no epoch cap, this is
     the timed grid, never the --max-epochs=5 smoke posture);
  5. stops the server;
  6. writes result.json + run.log via record.py into --out-dir.

Per-burst progress lines go to stderr (and run.log) as they happen, so the
caller's host-snapshot wrapper and PROGRESS.log line can be written right
after this process exits, from result.json/run.log alone -- this script
itself never writes PROGRESS.log or touches another cell's directory.

No `nice` is used or checked here; the host protocol decides that
(standing rule: timed cells run without nice), and whoever invokes this
script is responsible for the host-level pre/post snapshots this script
does not take itself.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cell_dir", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--logs-dir", type=Path, required=True)
    ap.add_argument("--conf-dir", type=Path, required=True)
    ap.add_argument("--csv-dir", type=Path, required=True)
    ap.add_argument("--neo4j-home", type=Path,
                    default=Path("/mnt/project/xzhang/neo4j/neo4j-community-5.26.0"))
    ap.add_argument("--java-home", type=Path,
                    default=Path("/mnt/project/xzhang/neo4j/jdk-21"))
    ap.add_argument("--pydeps", type=Path,
                    default=Path("/mnt/project/xzhang/neo4j/pydeps"))
    ap.add_argument("--pkg-dir", type=Path,
                    default=Path("/mnt/project/xzhang/tgms/external-v1/n1-work/neo4j-recompute"))
    ap.add_argument("--bolt-addr", default="127.0.0.1:7687")
    ap.add_argument("--http-addr", default="127.0.0.1:7475")
    ap.add_argument("--git-commit", default="unknown")
    args = ap.parse_args(argv)

    os.environ["JAVA_HOME"] = str(args.java_home)
    os.environ["PATH"] = f"{args.java_home}/bin:" + os.environ.get("PATH", "")
    os.environ["TMPDIR"] = "/mnt/project/xzhang/tgms/tmp"

    sys.path.insert(0, str(args.pkg_dir))
    sys.path.insert(0, str(args.pydeps))

    from neo4j import GraphDatabase
    from neo4j_recompute.csv_export import export_csv
    from neo4j_recompute.loader import (
        Neo4jLayout,
        apply_ddl,
        await_indexes_online,
        run_import,
        start_server,
        stop_server,
        wait_for_bolt,
        write_conf,
    )
    from neo4j_recompute.record import (
        build_result,
        machine_snapshot,
        setup_run_log,
        write_result,
    )
    from neo4j_recompute.runner import run_cell

    out_dir = args.out_dir
    logger = setup_run_log(out_dir)
    logger.info("cell_dir=%s out_dir=%s data_dir=%s", args.cell_dir, out_dir, args.data_dir)

    layout = Neo4jLayout(
        neo4j_home=args.neo4j_home, conf_dir=args.conf_dir,
        data_dir=args.data_dir, logs_dir=args.logs_dir,
        bolt_addr=args.bolt_addr,
    )
    # schema.conf_lines's http_addr defaults to 127.0.0.1:7475, which is
    # also this script's own --http-addr default; write_conf does not take
    # an http_addr override, so --http-addr only documents what the conf
    # will actually contain (no duplicate-key hazard from appending).
    conf_path = write_conf(layout)
    logger.info("wrote conf %s (http addr is schema.py's default, %s)",
               conf_path, args.http_addr)

    t_import0 = _utc_now()
    paths = export_csv(args.cell_dir / "versions-epoch0.jsonl", args.csv_dir)
    import_res = run_import(layout, paths)
    logger.info("import done rc=%s stderr_tail=%s", import_res.returncode,
               (import_res.stderr or "")[-500:])
    logger.info("import_started=%s import_finished=%s", t_import0, _utc_now())

    start_server(layout)
    logger.info("server start issued at %s", _utc_now())

    def driver_factory():
        return GraphDatabase.driver(f"bolt://{args.bolt_addr}", auth=None)

    wait_for_bolt(driver_factory, timeout_s=180.0)
    logger.info("bolt ready at %s", _utc_now())

    drv = driver_factory()
    try:
        with drv.session() as s:
            apply_ddl(s)
            await_indexes_online(s, timeout_s=600)
        logger.info("DDL + indexes online at %s", _utc_now())
    finally:
        drv.close()

    def session_factory():
        d = driver_factory()
        return d.session()

    def on_burst(row):
        logger.info(
            "epoch=%d apply_ms=%.1f recompute_ms=%.1f agree=%d disagree=%s not_answered=%s",
            row.epoch, row.apply_ms, row.recompute_ms, row.agree, row.disagree,
            row.not_answered,
        )

    rows = run_cell(session_factory, args.cell_dir, max_epochs=None, on_burst=on_burst)

    stop_server(layout)
    logger.info("server stop issued at %s", _utc_now())

    manifest_path = args.cell_dir / "export-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    result = build_result(
        cell_id=manifest.get("cell_id", args.cell_dir.name),
        cell_digest=manifest.get("cell_digest", ""),
        equality_level=manifest.get("equality_level", "unknown"),
        export_manifest_sha256="",
        git_commit=args.git_commit,
        timestamp_utc=_utc_now(),
        machine=machine_snapshot(os.environ.get("HOSTNAME", "xzgpu")),
        rows=rows,
        ceilings={"db.transaction.timeout_s": 600},
        conf_path=conf_path,
    )
    path = write_result(out_dir, result)
    logger.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
