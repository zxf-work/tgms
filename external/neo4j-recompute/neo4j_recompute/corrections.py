"""Apply one burst's corrections as property/edge updates with a new
transaction time (design memo §2.2), from X1's exported `deltas.jsonl` row
shape: `{tt, correction_class, generator, placement, closed: [{kind, vid,
tt_e}], inserted: [version rows]}`.

One write transaction per burst, four statements (closing is split by kind
since `NV`/`EV` are different graph element types in this schema; inserting
is split by kind because a new node version needs no relationship while a
new edge version does, and because `MERGE` on `(:E {uid})` must run before
the edge `CREATE` that references it).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

CLOSE_NODE_VERSIONS = """
UNWIND $closed_nodes AS c MATCH (v:NV {vid:c.vid}) SET v.tt_e = c.tt_e
""".strip()

CLOSE_EDGE_VERSIONS = """
UNWIND $closed_edges AS c MATCH ()-[e:EV {vid:c.vid}]->() SET e.tt_e = c.tt_e
""".strip()

INSERT_NODE_VERSIONS = """
UNWIND $new_nodes AS n MERGE (:E {uid:n.uid}) CREATE (v:NV) SET v = n
""".strip()

INSERT_EDGE_VERSIONS = """
UNWIND $new_edges AS r MERGE (a:E {uid:r.src}) MERGE (b:E {uid:r.dst})
CREATE (a)-[x:EV]->(b) SET x = r.p
""".strip()


def _props_to_text(row: dict) -> dict:
    """Bolt has no map-property restriction on *parameters* (only on stored
    node/relationship properties), but `props` is stored as a JSON string
    (schema §2.1) — this is where a freshly-inserted version's `props` dict
    gets serialized before `SET x = r.p`/`SET v = n`, mirroring what the CSV
    importer already did for epoch 0 (`csv_export.py::_row_values`)."""
    import json
    row = dict(row)
    if isinstance(row.get("props"), (dict, list)):
        row["props"] = json.dumps(row["props"], sort_keys=True, separators=(",", ":"))
    return row


def apply_burst(run_write: Callable[[str, dict[str, Any]], None], delta: dict) -> None:
    """Run the burst's four statements inside one write transaction.

    `run_write(cypher, params)` is the caller's single-transaction wrapper
    (`loader.py`): all four calls must commit together or not at all, since
    a burst that closes a version and inserts its replacement is one
    correction, not two independent writes.
    """
    closed = delta.get("closed") or []
    inserted = delta.get("inserted") or []
    closed_nodes = [c for c in closed if c["kind"] == "node"]
    closed_edges = [c for c in closed if c["kind"] == "edge"]
    new_nodes = [_props_to_text(r) for r in inserted if r.get("kind") == "node"]
    new_edges = [{"src": r["src"], "dst": r["dst"], "p": _props_to_text(
        {k: v for k, v in r.items() if k not in ("src", "dst")})}
        for r in inserted if r.get("kind") == "edge"]

    if closed_nodes:
        run_write(CLOSE_NODE_VERSIONS, {"closed_nodes": closed_nodes})
    if closed_edges:
        run_write(CLOSE_EDGE_VERSIONS, {"closed_edges": closed_edges})
    if new_nodes:
        run_write(INSERT_NODE_VERSIONS, {"new_nodes": new_nodes})
    if new_edges:
        run_write(INSERT_EDGE_VERSIONS, {"new_edges": new_edges})
