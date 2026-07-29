"""PostgreSQL baseline: schema, tuning, and loader (evaluation plan §4.1, §6).

PostgreSQL is a *baseline*, not a backend: it never implements the write
semantics. TGMS produces the canonical version rows — that is the plan's §6
canonical data layer — and this loads them with `COPY`. Doing it that way
keeps transaction times exactly as recorded, which is the whole reason the
reference event log is replayed rather than re-ingested (D-023).

The schema and indexes are what a competent operator would write, because
D-030 makes that the fairness policy: an untuned baseline would flatter TGMS
for reasons that have nothing to do with storage design. Every setting and
index here is meant to be reported in the run manifest.

    uv run --extra eval python scripts/pg_baseline.py --tune-server   # once
    uv run --extra eval python scripts/pg_baseline.py --scale 200000
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

import tgms
from tgms.core.model import OPEN_END, canonical_json

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = "tgms_eval"

#: Bi-temporal version tables, mirroring `tgms.core.model`.
#:
#: `props` is TEXT holding canonical JSON, not JSONB: JSONB normalizes key
#: order and whitespace, so a round trip would not return the bytes that were
#: stored — and the store digest is computed over exactly those bytes.
#:
#: An earlier version carried a generated JSONB column alongside for querying.
#: It was dropped after measuring: an expression index over `props::jsonb`
#: serves the same predicates without storing every blob twice. This also
#: mirrors TGMS, which keeps canonical JSON text and parses above the storage
#: layer (eval_semantics §6) — so both systems pay the parse.
SCHEMA = """
DROP TABLE IF EXISTS edge_versions, node_versions, entities CASCADE;

CREATE TABLE entities (
    dense_id BIGINT PRIMARY KEY,
    uid      TEXT NOT NULL UNIQUE,
    label    TEXT
);

CREATE TABLE node_versions (
    vid            TEXT PRIMARY KEY,
    uid            TEXT   NOT NULL,
    label          TEXT,
    vt_s           BIGINT NOT NULL,
    vt_e           BIGINT NOT NULL,
    tt_s           BIGINT NOT NULL,
    tt_e           BIGINT NOT NULL,
    props          TEXT   NOT NULL,
    source         TEXT,
    provenance_ref TEXT
);

CREATE TABLE edge_versions (
    vid            TEXT PRIMARY KEY,
    eid            TEXT   NOT NULL,
    src            TEXT   NOT NULL,
    dst            TEXT   NOT NULL,
    src_id         BIGINT NOT NULL,
    dst_id         BIGINT NOT NULL,
    rel_type       TEXT   NOT NULL,
    disc           TEXT,
    vt_s           BIGINT NOT NULL,
    vt_e           BIGINT NOT NULL,
    tt_s           BIGINT NOT NULL,
    tt_e           BIGINT NOT NULL,
    props          TEXT   NOT NULL,
    source         TEXT,
    provenance_ref TEXT
);
"""

#: Indexes chosen for the registry's access patterns, not sprinkled at random.
#:
#: The partial indexes are the important ones: over 95% of queries ask about
#: current beliefs, so a partial index over exactly those rows is both smaller
#: and the planner's obvious choice — the closest relational analogue of the
#: engine's `all_current` segment flag.
#:
#: They are only reachable if current-belief SQL is spelled `tt_e = OPEN_END`.
#: The general form `tt_s <= T AND T < tt_e` is what an as-of query needs, and
#: the planner cannot prove it implies the partial predicate — it does not in
#: general, only because tt_e never exceeds OPEN_END, which is a fact about the
#: data rather than the schema. Measured: the range spelling falls back to
#: `ev_vt`. Registry SQL must branch on `as_of_tt`, which is exactly the branch
#: the engine makes when it checks `all_current`.
INDEXES = f"""
CREATE INDEX ev_vt         ON edge_versions (vt_s, vid);
CREATE INDEX ev_vt_current ON edge_versions (vt_s, vid) WHERE tt_e = {OPEN_END};
CREATE INDEX ev_vte        ON edge_versions (vt_e);
CREATE INDEX ev_belief     ON edge_versions (tt_s, tt_e);
CREATE INDEX ev_eid        ON edge_versions (eid, vt_s);
CREATE INDEX ev_src        ON edge_versions (src_id, vt_s) WHERE tt_e = {OPEN_END};
CREATE INDEX ev_dst        ON edge_versions (dst_id, vt_s) WHERE tt_e = {OPEN_END};
CREATE INDEX ev_rel        ON edge_versions (rel_type, vt_s) WHERE tt_e = {OPEN_END};

CREATE INDEX nv_uid        ON node_versions (uid, vt_s);
CREATE INDEX nv_uid_cur    ON node_versions (uid, vt_s) WHERE tt_e = {OPEN_END};
CREATE INDEX nv_vt         ON node_versions (vt_s, vid);
CREATE INDEX nv_belief     ON node_versions (tt_s, tt_e);
CREATE INDEX nv_name       ON node_versions ((props::jsonb ->> 'name'));
"""

#: Server-level settings are applied once with ALTER SYSTEM (see the module
#: docstring) and read back here, because a number measured against an untuned
#: server measures the default rather than PostgreSQL.
#:
#: `effective_io_concurrency` is reported because macOS pins it to 0 — the
#: platform lacks posix_fadvise, so the baseline cannot prefetch here at all.
#: On Linux it would be set to 200. That is a reason to run the comparison on
#: one Linux host rather than a reason to discount the baseline.
#: Applied once with `--tune-server`, which needs a restart to take effect.
#: Kept here rather than in a hand-edited postgresql.conf so the baseline is
#: reproducible on another host: an unrecorded tuning is the same problem as
#: no tuning. Sized for a 16 GB host; scale the first two with RAM.
#:
#: `effective_io_concurrency` is deliberately absent — macOS rejects any value
#: but 0, lacking posix_fadvise. On Linux, set it to 200.
SERVER_TUNING = {
    "shared_buffers": "4GB",
    "effective_cache_size": "12GB",
    "random_page_cost": "1.1",
    "wal_buffers": "16MB",
    "max_wal_size": "4GB",
    "checkpoint_completion_target": "0.9",
    "max_parallel_workers_per_gather": "4",
}

SERVER_TUNING_REPORTED = [
    "shared_buffers", "effective_cache_size", "random_page_cost",
    "effective_io_concurrency", "max_wal_size", "server_version",
]

SESSION_TUNING = {
    "work_mem": "256MB",
    "maintenance_work_mem": "512MB",
    "jit": "on",
    "max_parallel_workers_per_gather": "4",
}


def tune_server() -> None:
    """Write SERVER_TUNING to postgresql.auto.conf. Requires a restart."""
    with connect() as conn:
        for k, v in SERVER_TUNING.items():
            conn.execute(f"ALTER SYSTEM SET {k} = '{v}'")
    print("server tuning written to postgresql.auto.conf; restart to apply:")
    print("  brew services restart postgresql@16")


def _index_used(plan_json: str) -> str:
    """Name the index a plan actually scanned, or say it went sequential."""
    for name in ("ev_vt_current", "ev_vt", "ev_src", "ev_dst", "ev_eid",
                 "ev_belief", "ev_rel", "edge_versions_pkey"):
        if f'"{name}"' in plan_json:
            return name
    return "seq scan"


def connect(dbname: str = DB) -> psycopg.Connection:
    return psycopg.connect(f"dbname={dbname}", autocommit=True)


def ensure_database() -> None:
    with psycopg.connect("dbname=postgres", autocommit=True) as c:
        exists = c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB,)).fetchone()
        if not exists:
            c.execute(f'CREATE DATABASE "{DB}"')


def apply_tuning(conn: psycopg.Connection) -> dict[str, str]:
    for k, v in SESSION_TUNING.items():
        # SET takes no bind parameters; the values are module constants, so
        # there is nothing here that user input could reach
        conn.execute(f"SET {k} = '{v}'")
    keys = list(SESSION_TUNING) + SERVER_TUNING_REPORTED
    return {k: conn.execute(f"SHOW {k}").fetchone()[0] for k in keys}


def _copy(conn: psycopg.Connection, table: str, columns: list[str], rows) -> int:
    """Bulk-load with COPY. Text format, tab-separated, explicit NULL marker."""
    def esc(v: Any) -> str:
        if v is None:
            return r"\N"
        s = v if isinstance(v, str) else str(v)
        return (s.replace("\\", "\\\\").replace("\t", "\\t")
                 .replace("\n", "\\n").replace("\r", "\\r"))

    n = 0
    with conn.cursor().copy(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN"
    ) as cp:
        buf = io.StringIO()
        for row in rows:
            buf.write("\t".join(esc(v) for v in row) + "\n")
            n += 1
            if buf.tell() > 8 << 20:
                cp.write(buf.getvalue())
                buf = io.StringIO()
        if buf.tell():
            cp.write(buf.getvalue())
    return n


def load(conn: psycopg.Connection, adapter) -> dict[str, int]:
    """Copy the canonical version rows out of a TGMS store.

    Transaction times come across unchanged, so belief queries mean the same
    thing on both systems — which is the only way `hist.asof` can compare.
    """
    counts: dict[str, int] = {}
    n_ent = adapter.num_entities()
    uids = adapter.uids_for(list(range(n_ent)))
    counts["entities"] = _copy(
        conn, "entities", ["dense_id", "uid", "label"],
        ((i, u, "") for i, u in enumerate(uids)),
    )
    counts["node_versions"] = _copy(
        conn, "node_versions",
        ["vid", "uid", "label", "vt_s", "vt_e", "tt_s", "tt_e", "props",
         "source", "provenance_ref"],
        ((v.vid, v.uid, v.label, v.vt_s, v.vt_e, v.tt_s, v.tt_e,
          canonical_json(v.props), v.source, v.provenance_ref)
         for v in adapter.all_node_versions()),
    )
    counts["edge_versions"] = _copy(
        conn, "edge_versions",
        ["vid", "eid", "src", "dst", "src_id", "dst_id", "rel_type", "disc",
         "vt_s", "vt_e", "tt_s", "tt_e", "props", "source", "provenance_ref"],
        ((v.vid, v.eid, v.src, v.dst,
          int(adapter.dense_ids([v.src])[0]), int(adapter.dense_ids([v.dst])[0]),
          v.rel_type, v.disc, v.vt_s, v.vt_e, v.tt_s, v.tt_e,
          canonical_json(v.props), v.source, v.provenance_ref)
         for v in adapter.all_edge_versions()),
    )
    return counts


def build_reference(scale: int):
    """Delegate to the harness generator, so both see identical data.

    This used to be a copy of the generator. It drifted the moment the harness
    one grew a second belief epoch, which is exactly the kind of divergence
    that makes a baseline measure the wrong dataset.
    """
    from eval_harness import build_dataset

    return build_dataset(scale)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=200_000)
    ap.add_argument("--tune-server", action="store_true",
                    help="apply SERVER_TUNING and exit; needs a restart after")
    ap.add_argument("--json", type=Path, help="write the load manifest here")
    args = ap.parse_args()

    if args.tune_server:
        ensure_database()
        tune_server()
        return 0

    print(f"postgres baseline — loading {args.scale:,} events")
    data = build_reference(args.scale)
    adapter = tgms.open(data.log.parent, backend="native").adapter

    ensure_database()
    with connect() as conn:
        settings = apply_tuning(conn)
        print(f"  server {settings['server_version']} | "
              f"shared_buffers {settings['shared_buffers']} | "
              f"work_mem {settings['work_mem']}")

        t0 = time.perf_counter()
        conn.execute(SCHEMA)
        counts = load(conn, adapter)
        load_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        conn.execute(INDEXES)
        conn.execute("ANALYZE edge_versions")
        conn.execute("ANALYZE node_versions")
        index_s = time.perf_counter() - t0

        size = conn.execute(
            "SELECT pg_total_relation_size('edge_versions'), "
            "       pg_total_relation_size('node_versions'), "
            "       pg_indexes_size('edge_versions')"
        ).fetchone()

        # The loader is a transcription; if it drops or duplicates rows the
        # baseline answers a different question. Check against the source.
        for table, n in counts.items():
            in_db = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if in_db != n:
                raise SystemExit(f"{table}: copied {n} rows, {in_db} landed")
            print(f"  {table:<16}{n:>12,} rows")
        expect_edges = len(list(adapter.all_edge_versions()))
        if counts["edge_versions"] != expect_edges:
            raise SystemExit(
                f"edge_versions: store has {expect_edges}, loaded {counts['edge_versions']}")
        print(f"  load  {load_s:6.1f}s   index+analyze {index_s:6.1f}s")
        print(f"  edge table {size[0]/1e6:8.1f} MB (indexes {size[2]/1e6:.1f} MB) | "
              f"node table {size[1]/1e6:.1f} MB")

        # A window scan under each belief spelling. Both are legitimate SQL for
        # the same question when tt_e never exceeds OPEN_END, but only the
        # equality form can reach the partial index — so the registry has to
        # pick per query, and the manifest records which index each got.
        plans = {
            "current_equality": f"tt_e = {OPEN_END}",
            "asof_range": f"tt_s <= {OPEN_END - 1} AND {OPEN_END - 1} < tt_e",
        }
        used = {}
        for label, pred in plans.items():
            plan = conn.execute(
                f"EXPLAIN (FORMAT JSON) SELECT vid FROM edge_versions "
                f"WHERE {pred} AND vt_e > 100 AND vt_s < 5000 ORDER BY vt_s, vid"
            ).fetchone()[0]
            used[label] = _index_used(json.dumps(plan))
            print(f"  window scan ({label}): {used[label]}")
        if used["current_equality"] != "ev_vt_current":
            print("  WARNING: current-belief scan missed the partial index")

        if args.json:
            args.json.write_text(json.dumps({
                "settings": settings, "counts": counts,
                "load_seconds": round(load_s, 2),
                "index_seconds": round(index_s, 2),
                "edge_bytes": size[0], "node_bytes": size[1],
                "edge_index_bytes": size[2],
                "window_scan_index": used,
                "index_note": "partial index needs the equality spelling",
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
