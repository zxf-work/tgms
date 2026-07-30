"""ClickHouse baseline: schema, tuning, and loader (evaluation plan §4.1, Phase 2).

Same contract as the PostgreSQL baseline (D-030): ClickHouse is a *baseline*,
not a backend — it never implements the write semantics. TGMS produces the
canonical version rows and this loads them, so transaction times arrive
exactly as recorded (D-023) and every derived id survives byte-for-byte.

The server is a user-space static binary on the measurement host (no root
there, same as the PostgreSQL source build): localhost-only on ports
19000/18123, data under /mnt/project/xzhang/tgms/ch. Settings a competent
operator would change are in the config written at install and echoed into
the run manifest.

    uv run --extra eval python scripts/ch_baseline.py --scale 200000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import clickhouse_connect

import tgms
from tgms.core.model import canonical_json

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = "tgms_eval"

#: MergeTree, ordered to serve the registry's two dominant access shapes.
#:
#: `edge_versions` orders by (vt_s, vid): the scan contract's total order, so
#: window scans read in output order with no sort. `node_versions` orders by
#: (uid, vt_s, vid): entity history is a prefix read. ClickHouse compares
#: String bytewise, which matches the operators' code-point ordering for free
#: — the COLLATE "C" trap that PostgreSQL required does not exist here.
#:
#: `props` is a String holding canonical JSON, for the same digest-parity
#: reason as the PostgreSQL TEXT column: JSON types normalize, and the store
#: digest is computed over exactly these bytes.
SCHEMA = """
CREATE DATABASE IF NOT EXISTS {db};

DROP TABLE IF EXISTS {db}.edge_versions;
DROP TABLE IF EXISTS {db}.node_versions;
DROP TABLE IF EXISTS {db}.entities;

CREATE TABLE {db}.entities (
    dense_id UInt64,
    uid      String,
    label    String
) ENGINE = MergeTree ORDER BY dense_id;

CREATE TABLE {db}.node_versions (
    vid            String,
    uid            String,
    label          String,
    vt_s           Int64,
    vt_e           Int64,
    tt_s           Int64,
    tt_e           Int64,
    props          String,
    source         String,
    provenance_ref Nullable(String)
) ENGINE = MergeTree ORDER BY (uid, vt_s, vid);

CREATE TABLE {db}.edge_versions (
    vid            String,
    eid            String,
    src            String,
    dst            String,
    src_id         UInt64,
    dst_id         UInt64,
    rel_type       String,
    disc           String,
    vt_s           Int64,
    vt_e           Int64,
    tt_s           Int64,
    tt_e           Int64,
    props          String,
    source         String,
    provenance_ref Nullable(String)
) ENGINE = MergeTree ORDER BY (vt_s, vid);
"""

#: Session settings, echoed into the manifest (D-030: tuning is part of what
#: is measured). ClickHouse parallelizes by default; nothing here caps it.
SESSION = {"max_threads": 16}


def connect():
    return clickhouse_connect.get_client(
        host="127.0.0.1", port=18123, settings=SESSION)


def settings_report(client) -> dict[str, str]:
    out = {"version": client.server_version}
    for k in ("max_threads", "max_memory_usage"):
        r = client.query(
            f"SELECT value FROM system.settings WHERE name = '{k}'").result_rows
        out[k] = r[0][0] if r else "?"
    return out


def create_schema(client) -> None:
    for stmt in SCHEMA.format(db=DB).split(";"):
        if stmt.strip():
            client.command(stmt)


def load(client, adapter) -> dict[str, int]:
    """Insert the canonical version rows, blocked for the columnar client."""
    counts: dict[str, int] = {}
    uids = adapter.uids_for(list(range(adapter.num_entities())))
    rows = [(i, u, "") for i, u in enumerate(uids)]
    client.insert(f"{DB}.entities", rows,
                  column_names=["dense_id", "uid", "label"])
    counts["entities"] = len(rows)

    nv = [(v.vid, v.uid, v.label, v.vt_s, v.vt_e, v.tt_s, v.tt_e,
           canonical_json(v.props), v.source, v.provenance_ref)
          for v in adapter.all_node_versions()]
    client.insert(f"{DB}.node_versions", nv,
                  column_names=["vid", "uid", "label", "vt_s", "vt_e",
                                "tt_s", "tt_e", "props", "source",
                                "provenance_ref"])
    counts["node_versions"] = len(nv)

    cols = ["vid", "eid", "src", "dst", "src_id", "dst_id", "rel_type",
            "disc", "vt_s", "vt_e", "tt_s", "tt_e", "props", "source",
            "provenance_ref"]
    batch, n = [], 0
    for v in adapter.all_edge_versions():
        batch.append((v.vid, v.eid, v.src, v.dst,
                      int(adapter.dense_ids([v.src])[0]),
                      int(adapter.dense_ids([v.dst])[0]),
                      v.rel_type, v.disc, v.vt_s, v.vt_e, v.tt_s, v.tt_e,
                      canonical_json(v.props), v.source, v.provenance_ref))
        if len(batch) >= 200_000:
            client.insert(f"{DB}.edge_versions", batch, column_names=cols)
            n += len(batch)
            batch = []
    if batch:
        client.insert(f"{DB}.edge_versions", batch, column_names=cols)
        n += len(batch)
    counts["edge_versions"] = n

    for table, expect in counts.items():
        got = client.query(f"SELECT count() FROM {DB}.{table}").result_rows[0][0]
        if got != expect:
            raise SystemExit(f"{table}: inserted {expect}, {got} landed")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=200_000)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    from eval_harness import build_dataset

    data = build_dataset(args.scale)
    adapter = tgms.open(Path(data.log).parent, backend="native").adapter
    client = connect()
    st = settings_report(client)
    print(f"clickhouse baseline — {st['version']} | loading {args.scale:,} events")
    t0 = time.perf_counter()
    create_schema(client)
    counts = load(client, adapter)
    secs = time.perf_counter() - t0
    client.command(f"OPTIMIZE TABLE {DB}.edge_versions FINAL")
    bytes_on_disk = client.query(
        f"SELECT sum(bytes_on_disk) FROM system.parts "
        f"WHERE database = '{DB}' AND active").result_rows[0][0]
    for t, n in counts.items():
        print(f"  {t:<16}{n:>12,} rows")
    print(f"  load {secs:6.1f}s | on disk {bytes_on_disk/1e6:.1f} MB "
          f"({bytes_on_disk/max(1, counts['edge_versions']):.1f} B/edge-row)")
    if args.json:
        args.json.write_text(json.dumps(
            {"settings": st, "counts": counts, "load_seconds": round(secs, 2),
             "bytes_on_disk": bytes_on_disk}, indent=2, sort_keys=True) + "\n")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
