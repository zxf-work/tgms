"""δ-temporal motif operators (Paranjape et al., WSDM 2017): O6 count, O7 find.

Motif semantics (engine and oracle agree on exactly this):
- An *event* is an edge version, at event time t = vt_s, restricted to
  t in [window.t_a, window.t_b).
- Motif edges are strictly ordered by (t, eid) — a deterministic total order
  that breaks timestamp ties — and satisfy t_last - t_first <= delta.
- Motif node variables are pairwise distinct; rel_type is ignored.

Matching runs as DuckDB non-equi self-joins over an in-memory Arrow event
table — vectorized, engine-parallel, no per-edge Python loops.
"""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

from tgms.core.errors import InvalidArgError
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    WINDOW,
    check_window,
    operator,
    paginate,
    required,
)
from tgms.temporal.guardrails import window_fraction

# each motif: number of edges + join/filter SQL over aliases e1..eN
# (s = src dense id, d = dst dense id, t = event time, x = eid tiebreaker)
_ORD = ("(({b}.t > {a}.t) OR ({b}.t = {a}.t AND {b}.x > {a}.x))")


def _seq(*aliases: str) -> str:
    return " AND ".join(_ORD.format(a=a, b=b) for a, b in zip(aliases, aliases[1:]))


MOTIFS: dict[str, dict[str, Any]] = {
    "M_triangle_cyclic": {  # u->v, v->w, w->u
        "n": 3,
        "join": ("e2.s = e1.d AND e3.s = e2.d AND e3.d = e1.s "
                 "AND e1.s <> e1.d AND e2.s <> e2.d AND e1.s <> e2.d"),
    },
    "M_triangle_acyclic_1": {  # u->v, u->w, v->w
        "n": 3,
        "join": ("e2.s = e1.s AND e3.s = e1.d AND e3.d = e2.d "
                 "AND e1.d <> e2.d AND e1.s <> e1.d AND e1.s <> e2.d"),
    },
    "M_2node_pingpong": {  # u->v, v->u, u->v
        "n": 3,
        "join": ("e2.s = e1.d AND e2.d = e1.s AND e3.s = e1.s AND e3.d = e1.d "
                 "AND e1.s <> e1.d"),
    },
    "M_star_out_3": {  # u->a, u->b, u->c
        "n": 3,
        "join": ("e2.s = e1.s AND e3.s = e1.s "
                 "AND e1.d <> e2.d AND e1.d <> e3.d AND e2.d <> e3.d "
                 "AND e1.d <> e1.s AND e2.d <> e1.s AND e3.d <> e1.s"),
    },
    "M_path_3": {  # u->v->w->x
        "n": 3,
        "join": ("e2.s = e1.d AND e3.s = e2.d "
                 "AND e1.s <> e1.d AND e1.s <> e2.d AND e1.s <> e3.d "
                 "AND e1.d <> e2.d AND e1.d <> e3.d AND e2.d <> e3.d"),
    },
}

MOTIF_ARGS = {
    "motif": required({"type": "string", "enum": sorted(MOTIFS)}),
    "delta": required({"type": "integer", "minimum": 1,
                       "description": "max span t_last - t_first, microseconds"}),
    "window": required(WINDOW),
    "node_filter": {"type": ["array", "null"], "items": {"type": "string"},
                    "maxItems": 10_000, "default": None,
                    "description": "restrict all motif nodes to this uid set"},
    "as_of_tt": AS_OF_TT,
    "mode": {"type": "string", "enum": ["exact", "sample"], "default": "exact"},
    "seed": {"type": ["integer", "null"], "default": None},
}

EXACT_EDGE_CAP = 5_000_000


def _motif_cost(args: dict[str, Any], stats: dict[str, Any]) -> dict[str, int]:
    e_w = int(stats.get("n_edge_versions", 0) * window_fraction(args, stats))
    deg = max(1, int(stats.get("max_out_degree", 1)))
    if args.get("node_filter"):
        e_w = min(e_w, len(args["node_filter"]) * deg)
    return {"rows_scanned_est": stats.get("n_edge_versions", 0),
            "expansions_est": min(e_w * deg, 2**40)}


def _event_table(adapter: StorageAdapter, args: dict[str, Any]) -> pa.Table:
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    e = adapter.edges_columnar(as_of_tt=args["as_of_tt"], vt_min=t_a, vt_max=t_b)
    m = (e["vt_s"] >= t_a) & (e["vt_s"] < t_b)
    if args["node_filter"] is not None:
        ids = adapter.dense_ids(sorted(set(args["node_filter"])))
        m &= np.isin(e["src_id"], ids) & np.isin(e["dst_id"], ids)
    if args["mode"] == "exact" and args["node_filter"] is None \
            and int(m.sum()) > EXACT_EDGE_CAP:
        raise InvalidArgError(
            f"exact motif matching needs node_filter or <= {EXACT_EDGE_CAP} "
            "window events; add a node_filter or use mode='sample'")
    return pa.table({
        "s": pa.array(e["src_id"][m], pa.int64()),
        "d": pa.array(e["dst_id"][m], pa.int64()),
        "t": pa.array(e["vt_s"][m], pa.int64()),
        "x": pa.array(e["eid"][m].tolist(), pa.string()),
        "r": pa.array(e["rel_type"][m].tolist(), pa.string()),
    })


def _motif_query(motif: str, select: str, order: str = "") -> str:
    spec = MOTIFS[motif]
    aliases = [f"e{i + 1}" for i in range(spec["n"])]
    froms = ", ".join(f"ev {a}" for a in aliases)
    where = (f"{spec['join']} AND {_seq(*aliases)} "
             f"AND {aliases[-1]}.t - {aliases[0]}.t <= $delta")
    return f"SELECT {select} FROM {froms} WHERE {where} {order}"


def _run(events: pa.Table, sql: str, delta: int) -> duckdb.DuckDBPyRelation:
    conn = duckdb.connect(":memory:")
    conn.register("ev", events)
    return conn.execute(sql, {"delta": delta})


@operator(
    "count_temporal_motifs",
    MOTIF_ARGS,
    "Exact count of delta-temporal motif instances among edge events in "
    "`window` (events strictly ordered by (t, eid); span <= delta; motif "
    "nodes pairwise distinct; rel_type ignored).",
    cost_fn=_motif_cost,
    validators=[check_window],
    output_fields=("count", "n_events_in_window", "truncated"),
)
def count_temporal_motifs(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    if args["mode"] == "sample":
        raise InvalidArgError("mode='sample' lands with the M3 guardrail work; "
                              "use node_filter for now")
    events = _event_table(adapter, args)
    sql = _motif_query(args["motif"], "count(*)")
    count = _run(events, sql, args["delta"]).fetchone()[0]
    return {"count": int(count), "n_events_in_window": events.num_rows,
            "truncated": False}


@operator(
    "find_temporal_motif_instances",
    {**MOTIF_ARGS, "limit": LIMIT, "cursor": CURSOR},
    "Enumerate delta-temporal motif instances as ordered edge-event tuples, "
    "deterministically ordered by the (t, eid) sequence of their edges.",
    cost_fn=_motif_cost,
    validators=[check_window],
)
def find_temporal_motif_instances(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    if args["mode"] == "sample":
        raise InvalidArgError("mode='sample' lands with the M3 guardrail work; "
                              "use node_filter for now")
    events = _event_table(adapter, args)
    n = MOTIFS[args["motif"]]["n"]
    select = ", ".join(f"e{i}.s, e{i}.d, e{i}.t, e{i}.x, e{i}.r"
                       for i in range(1, n + 1))
    order = "ORDER BY " + ", ".join(f"e{i}.t, e{i}.x" for i in range(1, n + 1))
    res = _run(events, _motif_query(args["motif"], select, order), args["delta"])
    raw = res.fetchall()
    rows = []
    for tup in raw:
        edges = []
        for i in range(n):
            s, d, t, x, r = tup[i * 5: i * 5 + 5]
            su, du = adapter.uids_for([int(s), int(d)])
            edges.append({"src": su, "dst": du, "t": int(t), "eid": x, "rel_type": r})
        rows.append({"edges": edges})
    return paginate(rows, args["limit"], args["cursor"])
