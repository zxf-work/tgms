"""Memgraph baseline (evaluation plan §4.1, Phase 3, second graph system).

Memgraph speaks bolt and openCypher, so this deliberately *reuses* the Neo4j
baseline: the loader is imported unchanged (its UNWIND/CREATE statements are
standard Cypher), and only the connection, the index DDL, and one query
override differ (see memgraph_queries). Same fairness contract as every
baseline (D-030): canonical rows in byte-for-byte, slices hash-verified
before timed, tuning recorded.

The server is the official container on the measurement host, loopback-only
on port 7688, data and logs on /mnt/project, running under the invoking uid.

    uv run --extra eval python scripts/memgraph_baseline.py --scale 200000
"""

from __future__ import annotations

import sys
from pathlib import Path

import neo4j

sys.path.insert(0, str(Path(__file__).resolve().parent))

import neo4j_baseline

URI = "bolt://127.0.0.1:7688"


def connect():
    return neo4j.GraphDatabase.driver(URI, auth=None)


def create_schema(drv) -> None:
    with drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        # Memgraph's DDL differs from Neo4j 5's; each statement is applied
        # tolerantly so a syntax gap in one index degrades performance, not
        # correctness (results are hash-checked either way)
        for stmt in (
            "CREATE CONSTRAINT ON (e:Entity) ASSERT e.uid IS UNIQUE",
            "CREATE INDEX ON :Entity(uid)",
            "CREATE INDEX ON :Entity(dense_id)",
            "CREATE INDEX ON :NodeVersion(uid)",
            "CREATE EDGE INDEX ON :E(vt_s)",
            "CREATE EDGE INDEX ON :E(tt_e)",
        ):
            try:
                s.run(stmt).consume()
            except Exception as e:
                print(f"  (index skipped: {stmt!r}: {type(e).__name__})")


#: The loader is the Neo4j one, verbatim: standard Cypher, same data model.
load = neo4j_baseline.load
