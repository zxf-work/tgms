"""Neo4j baseline: schema, tuning, and loader (evaluation plan §4.1, Phase 3).

Same contract as the PostgreSQL and ClickHouse baselines (D-030/D-035):
Neo4j is a *baseline*, never a backend — TGMS produces the canonical version
rows and this loads them, so transaction times and derived ids arrive
byte-for-byte (D-023). The server is Neo4j Community 5.26 under a user-space
JDK 21 on the measurement host (no root there), loopback-only, heap and page
cache pinned in neo4j.conf and echoed into the manifest.

Data model, chosen for the registry rather than for graph-modeling
aesthetics: `(:Entity {uid, dense_id})` nodes; node versions as
`(:NodeVersion)` nodes indexed by uid; edge versions as relationships
`(:Entity)-[:E {...}]->(:Entity)` under a single relationship type with
`rel_type` as a property — the registry filters relations by value, and a
type-per-relation scheme would turn that into query-text surgery. All
bi-temporal fields live as integer properties; `props` stays a canonical
JSON string for digest parity.

    uv run --extra eval python scripts/neo4j_baseline.py --scale 200000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import neo4j

import tgms
from tgms.core.model import canonical_json

sys.path.insert(0, str(Path(__file__).resolve().parent))

URI = "bolt://127.0.0.1:7687"


def connect():
    # auth is disabled server-side (loopback-only box), but the bolt
    # handshake still requires a well-formed token; any one is accepted
    return neo4j.GraphDatabase.driver(URI, auth=("neo4j", "neo4j"))


def create_schema(drv) -> None:
    with drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        for stmt in (
            "CREATE CONSTRAINT ent_uid IF NOT EXISTS "
            "FOR (e:Entity) REQUIRE e.uid IS UNIQUE",
            "CREATE INDEX nv_uid IF NOT EXISTS "
            "FOR (n:NodeVersion) ON (n.uid)",
            "CREATE INDEX e_vts IF NOT EXISTS "
            "FOR ()-[r:E]-() ON (r.vt_s)",
            "CREATE INDEX e_tte IF NOT EXISTS "
            "FOR ()-[r:E]-() ON (r.tt_e)",
        ):
            s.run(stmt).consume()


def load(drv, adapter) -> dict[str, int]:
    counts: dict[str, int] = {}
    uids = adapter.uids_for(list(range(adapter.num_entities())))
    with drv.session() as s:
        s.run("UNWIND $rows AS r CREATE (:Entity {uid: r.uid, dense_id: r.id})",
              rows=[{"uid": u, "id": i} for i, u in enumerate(uids)]).consume()
        counts["entities"] = len(uids)

        # `name` is promoted to a property when it is a JSON string —
        # D-031's rule, and the same move as the engine's typed column:
        # Cypher has no JSON parser without APOC, and resolve matches on it
        nv = [{"vid": v.vid, "uid": v.uid, "label": v.label, "vt_s": v.vt_s,
               "vt_e": v.vt_e, "tt_s": v.tt_s, "tt_e": v.tt_e,
               "props": canonical_json(v.props), "source": v.source,
               "prov": v.provenance_ref,
               "name": (v.props.get("name")
                        if isinstance(v.props.get("name"), str) else None)}
              for v in adapter.all_node_versions()]
        for i in range(0, len(nv), 10_000):
            s.run("UNWIND $rows AS r CREATE (:NodeVersion {vid: r.vid, "
                  "uid: r.uid, label: r.label, vt_s: r.vt_s, vt_e: r.vt_e, "
                  "tt_s: r.tt_s, tt_e: r.tt_e, props: r.props, "
                  "source: r.source, prov: r.prov, name: r.name})",
                  rows=nv[i:i + 10_000]).consume()
        counts["node_versions"] = len(nv)

        n = 0
        batch = []

        def flush():
            nonlocal n
            if batch:
                s.run("UNWIND $rows AS r "
                      "MATCH (a:Entity {uid: r.src}), (b:Entity {uid: r.dst}) "
                      "CREATE (a)-[:E {eid: r.eid, vid: r.vid, "
                      "rel_type: r.rel, disc: r.disc, vt_s: r.vt_s, "
                      "vt_e: r.vt_e, tt_s: r.tt_s, tt_e: r.tt_e, "
                      "props: r.props, source: r.source, prov: r.prov}]->(b)",
                      rows=batch).consume()
                n += len(batch)
                batch.clear()

        for v in adapter.all_edge_versions():
            batch.append({"eid": v.eid, "vid": v.vid, "src": v.src,
                          "dst": v.dst, "rel": v.rel_type, "disc": v.disc,
                          "vt_s": v.vt_s, "vt_e": v.vt_e, "tt_s": v.tt_s,
                          "tt_e": v.tt_e, "props": canonical_json(v.props),
                          "source": v.source, "prov": v.provenance_ref})
            if len(batch) >= 10_000:
                flush()
        flush()
        counts["edge_versions"] = n

        for label, key, expect in (("Entity", "entities", counts["entities"]),
                                   ("NodeVersion", "node_versions",
                                    counts["node_versions"])):
            got = s.run(f"MATCH (x:{label}) RETURN count(x)").single()[0]
            if got != expect:
                raise SystemExit(f"{key}: sent {expect}, {got} landed")
        got = s.run("MATCH ()-[r:E]->() RETURN count(r)").single()[0]
        if got != counts["edge_versions"]:
            raise SystemExit(f"edges: sent {counts['edge_versions']}, {got} landed")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=200_000)
    args = ap.parse_args()
    from eval_harness import build_dataset

    data = build_dataset(args.scale)
    adapter = tgms.open(Path(data.log).parent, backend="native").adapter
    drv = connect()
    t0 = time.perf_counter()
    create_schema(drv)
    counts = load(drv, adapter)
    print(f"neo4j baseline — loaded {counts} in {time.perf_counter()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
