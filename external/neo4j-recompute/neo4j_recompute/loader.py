"""Process control: `neo4j-admin database import full` / `dump` / `load`,
server start/stop/await-indexes, and the Bolt driver session wrapper
`corrections.py`/`reachability.py` write/read through (design memo §2.2,
§2.7). Subprocess- and driver-shaped, so it only runs on a host with a
Neo4j 5.26 install (xzgpu) — `tests/` never imports this module without
mocking `subprocess.run`/the driver.
"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .csv_export import export_csv
from .schema import DDL_STATEMENTS


@dataclass(frozen=True)
class Neo4jLayout:
    """Paths for one lane-N1 Neo4j deployment (memo §2.7: a dedicated
    `conf-ext-v1`/`data-ext-v1`/`logs-ext-v1`, never the LDBC install's
    own `data-ldbc-ref`/`logs-ldbc-ref` — the two must never collide on
    disk, and never run at the same time as the T1 control (§2.7, §4.6)."""

    neo4j_home: Path          # e.g. /mnt/project/xzhang/neo4j/neo4j-community-5.26.0
    conf_dir: Path            # .../conf-ext-v1
    data_dir: Path            # .../data-ext-v1
    logs_dir: Path            # .../logs-ext-v1
    bolt_addr: str = "127.0.0.1:7687"
    database: str = "neo4j"


def write_conf(layout: Neo4jLayout) -> Path:
    from .schema import conf_lines
    layout.conf_dir.mkdir(parents=True, exist_ok=True)
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    conf_path = layout.conf_dir / "neo4j.conf"
    conf_path.write_text(conf_lines(str(layout.data_dir), str(layout.logs_dir),
                                    bolt_addr=layout.bolt_addr))
    return conf_path


def _admin_bin(layout: Neo4jLayout) -> Path:
    return layout.neo4j_home / "bin" / "neo4j-admin"


def _neo4j_bin(layout: Neo4jLayout) -> Path:
    return layout.neo4j_home / "bin" / "neo4j"


def run_import(layout: Neo4jLayout, csv_paths: dict[str, Path],
              run: Callable[..., subprocess.CompletedProcess] = subprocess.run
              ) -> subprocess.CompletedProcess:
    """`neo4j-admin database import full` from the CSV files `csv_export.py`
    wrote. Two corrections proven on the LDBC run apply here too
    (`benchmarks/ldbc-ref-v1/README.md` §3): no trailing `<database>`
    argument (picocli parses it as one more `--relationships` file), and
    every part-file must be headerless, which `csv_export.py` already
    writes that way (the header lives in its own `*-header.csv`).
    """
    cmd = [
        str(_admin_bin(layout)), "database", "import", "full",
        f"--nodes=E={csv_paths['entities_header']},{csv_paths['entities']}",
        f"--nodes=NV={csv_paths['node_versions_header']},{csv_paths['node_versions']}",
        f"--relationships=EV={csv_paths['edge_versions_header']},{csv_paths['edge_versions']}",
        "--overwrite-destination=true",
        f"--additional-config={layout.conf_dir / 'neo4j.conf'}",
    ]
    return run(cmd, check=True, capture_output=True, text=True)


def dump_pristine(layout: Neo4jLayout, dump_path: Path,
                  run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                  ) -> subprocess.CompletedProcess:
    cmd = [str(_admin_bin(layout)), "database", "dump", layout.database,
           f"--to-path={dump_path.parent}",
           f"--additional-config={layout.conf_dir / 'neo4j.conf'}"]
    return run(cmd, check=True, capture_output=True, text=True)


def load_from_dump(layout: Neo4jLayout, dump_path: Path,
                   run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                   ) -> subprocess.CompletedProcess:
    """Per cell: `database load --overwrite-destination` from the pristine
    per-store dump (memo §2.2) — never drop or reuse an existing database
    directory across cells (lane rule); the caller picks a fresh
    `layout.database`/`layout.data_dir` name per cell."""
    cmd = [str(_admin_bin(layout)), "database", "load", layout.database,
           f"--from-path={dump_path.parent}", "--overwrite-destination=true",
           f"--additional-config={layout.conf_dir / 'neo4j.conf'}"]
    return run(cmd, check=True, capture_output=True, text=True)


def start_server(layout: Neo4jLayout,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                 ) -> subprocess.CompletedProcess:
    env_prefix = f"NEO4J_CONF={layout.conf_dir} "
    cmd = ["bash", "-lc", f"{env_prefix}{_neo4j_bin(layout)} start"]
    return run(cmd, check=True, capture_output=True, text=True)


def stop_server(layout: Neo4jLayout,
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                ) -> subprocess.CompletedProcess:
    env_prefix = f"NEO4J_CONF={layout.conf_dir} "
    cmd = ["bash", "-lc", f"{env_prefix}{_neo4j_bin(layout)} stop"]
    return run(cmd, check=True, capture_output=True, text=True)


def wait_for_bolt(driver_factory: Callable[[], Any], timeout_s: float = 60.0) -> None:
    """Poll until a session can run `RETURN 1`, or raise after `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            drv = driver_factory()
            with drv.session() as s:
                s.run("RETURN 1").consume()
            drv.close()
            return
        except Exception as e:  # noqa: BLE001
            last_exc = e
            time.sleep(1.0)
    raise TimeoutError(f"Neo4j bolt not reachable within {timeout_s}s") from last_exc


def apply_ddl(session: Any) -> None:
    for stmt in DDL_STATEMENTS:
        session.run(stmt).consume()


def await_indexes_online(session: Any, timeout_s: int = 600) -> None:
    session.run("CALL db.awaitIndexes($timeout)", {"timeout": timeout_s}).consume()


def build_pristine_database(layout: Neo4jLayout, versions_epoch0: Path,
                            csv_out_dir: Path, driver_factory: Callable[[], Any],
                            run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                            ) -> None:
    """Full one-time build (memo §2.2): CSV export, `database import full`,
    start, DDL + `awaitIndexes`, ready for `dump_pristine`."""
    write_conf(layout)
    paths = export_csv(versions_epoch0, csv_out_dir)
    run_import(layout, paths, run=run)
    start_server(layout, run=run)
    wait_for_bolt(driver_factory)
    drv = driver_factory()
    try:
        with drv.session(database=layout.database) as s:
            apply_ddl(s)
            await_indexes_online(s)
    finally:
        drv.close()


def run_write_tx(session: Any, statements: Iterable[tuple[str, dict]]) -> None:
    """All of `statements` inside one write transaction (one commit) —
    `corrections.py::apply_burst` calls this once per burst."""
    def work(tx: Any) -> None:
        for cypher, params in statements:
            tx.run(cypher, params).consume()
    session.execute_write(work)
