"""Neo4j record -> TGMS-shaped payload, per family (design memo §2.3's own
field choices, checked against the operators that produced them:
`tgms/temporal/ops_snapshot.py::_edge_rows`, `NodeVersion.to_json`,
`StorageAdapter.VERSION_COLS["node"]`).

`scripts/ext_neo4j_run.py` in the memo "only *formats* the payload
(`truncated`, `cursor`, JSON parsing of `props`, float rounding) — no
computation outside Neo4j except F11's fixpoint loop" (§2.3). This module is
that formatter: every family's Cypher text already projects the right
field *names* (canonical JSON sorts keys, so field order never matters);
what is left is (a) parsing the `props` string back into an object, (b)
deriving the top-level `truncated`/`cursor` TGMS's own `paginate()` would
have set, since not every family's query computes them itself, and (c)
re-rounding every float with `canon.canonicalize_floats` so a Neo4j-side
`round()` (half-up) can never be what makes two sides' digests agree —
only Python's (half-even) rounding, applied identically on both sides, can.
"""
from __future__ import annotations

import json
from typing import Any

from .canon import canonicalize_floats


def _props(obj: Any) -> Any:
    """Parse a `props` field (a canonical-JSON string column) into a value;
    leave anything already parsed (e.g. a test-built fixture) untouched."""
    if isinstance(obj, str):
        return json.loads(obj)
    return obj


def _parse_version_row(row: dict) -> dict:
    row = dict(row)
    if "props" in row:
        row["props"] = _props(row["props"])
    return row


def _paginate_fields(rows_total: int, limit: int) -> tuple[bool, str | None]:
    """TGMS's own `paginate()` semantics, for a call registered with
    `cursor=None` (offset 0) — true of every storm-registered artifact."""
    truncated = rows_total > limit
    cursor = str(min(rows_total, limit)) if truncated else None
    return truncated, cursor


def format_entity_history(record: dict, limit: int) -> dict:
    rows = [_parse_version_row(r) for r in record["rows"]]
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {
        "rows": rows, "rows_total": rows_total, "truncated": truncated,
        "cursor": cursor, "edges": record["edges"],
        "edges_truncated": record["edges_truncated"],
    }
    return canonicalize_floats(payload)


def format_version_history(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": record["rows"], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor}
    return canonicalize_floats(payload)


def format_snapshot_subgraph(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    nodes_total = record["nodes_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)  # cursor: edges-only page
    nodes_truncated = nodes_total > limit
    payload = {
        "rows": record["rows"], "rows_total": rows_total,
        "truncated": truncated or nodes_truncated, "cursor": cursor,
        "nodes": record["nodes"][:limit], "nodes_total": nodes_total,
        "nodes_truncated": nodes_truncated,
    }
    return canonicalize_floats(payload)


def format_diff_snapshots(record: dict, limit: int) -> dict:
    # nodes_added/removed are plain uid-string lists and edges_added/removed
    # carry no `props` (ops_snapshot.py's own `diff_snapshots`); only
    # props_changed rows carry {"from": {"props": "<json>", ...}, "to": {...}}.
    def fix_changed(rows: list) -> list:
        out = []
        for r in rows:
            r = dict(r)
            for side in ("from", "to"):
                if side in r and isinstance(r[side], dict) and "props" in r[side]:
                    s = dict(r[side])
                    s["props"] = _props(s["props"])
                    r[side] = s
            out.append(r)
        return out

    totals = {
        "nodes_added_total": record["nodes_added_total"],
        "nodes_removed_total": record["nodes_removed_total"],
        "edges_added_total": record["edges_added_total"],
        "edges_removed_total": record["edges_removed_total"],
        "props_changed_total": record["props_changed_total"],
    }
    truncated = any(v > limit for v in totals.values())
    payload = {
        "nodes_added": record["nodes_added"], "nodes_removed": record["nodes_removed"],
        "edges_added": record["edges_added"], "edges_removed": record["edges_removed"],
        "props_changed": fix_changed(record["props_changed"]),
        "truncated": truncated, **totals,
    }
    return canonicalize_floats(payload)


def format_neighborhood_evolution(record: dict, limit: int) -> dict:
    gained_total = record["neighbors_gained_total"]
    lost_total = record["neighbors_lost_total"]
    payload = {
        "neighbors_gained": record["neighbors_gained"],
        "neighbors_gained_total": gained_total,
        "neighbors_lost": record["neighbors_lost"],
        "neighbors_lost_total": lost_total,
        "degree_series": record["degree_series"],
        "stride": record["stride"],
        "truncated": gained_total > limit or lost_total > limit,
    }
    return canonicalize_floats(payload)


def format_aggregate_events(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": record["rows"], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor}
    return canonicalize_floats(payload)


def format_graph_metric_timeseries(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": record["rows"], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor,
               "n_buckets": record["n_buckets"]}
    return canonicalize_floats(payload)


def format_burst_detection(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": record["rows"], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor,
               "n_buckets": record["n_buckets"]}
    return canonicalize_floats(payload)


def format_count_temporal_motifs(record: dict, limit: int) -> dict:
    payload = {"count": record["count"], "n_events_in_window": record["n_events_in_window"],
               "truncated": False}
    return canonicalize_floats(payload)


def format_find_temporal_motif_instances(record: dict, limit: int) -> dict:
    rows_total = record["rows_total"]
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": record["rows"], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor}
    return canonicalize_floats(payload)


def format_temporal_reachability(rows: list[dict], limit: int) -> dict:
    """`rows` is already the sorted (arrival, uid) list the client-driven
    fixpoint produced (`reachability.py`), minus `src` — exactly the
    Python operator's own pre-`paginate` list."""
    rows_total = len(rows)
    truncated, cursor = _paginate_fields(rows_total, limit)
    payload = {"rows": rows[:limit], "rows_total": rows_total,
               "truncated": truncated, "cursor": cursor}
    return canonicalize_floats(payload)


def format_temporal_paths(record: dict, k: int) -> dict:
    payload = {"rows": record["rows"], "rows_total": record["rows_total"],
               "truncated": record["truncated"], "cursor": None}
    return canonicalize_floats(payload)


def format_compute(record: dict) -> dict:
    payload = {"value": record["value"], "truncated": False}
    return canonicalize_floats(payload)


FORMATTERS = {
    "entity_history": format_entity_history,
    "version_history": format_version_history,
    "snapshot_subgraph": format_snapshot_subgraph,
    "diff_snapshots": format_diff_snapshots,
    "neighborhood_evolution": format_neighborhood_evolution,
    "aggregate_events": format_aggregate_events,
    "graph_metric_timeseries": format_graph_metric_timeseries,
    "burst_detection": format_burst_detection,
    "count_temporal_motifs": format_count_temporal_motifs,
    "find_temporal_motif_instances": format_find_temporal_motif_instances,
    "temporal_reachability": format_temporal_reachability,
    "temporal_paths": format_temporal_paths,
    "compute": format_compute,
}
