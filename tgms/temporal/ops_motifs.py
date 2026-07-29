"""δ-temporal motif operators (Paranjape et al., WSDM 2017): O6 count, O7 find.

Motif semantics (engine and oracle agree on exactly this):
- An *event* is an edge version, at event time t = vt_s, restricted to
  t in [window.t_a, window.t_b).
- Motif edges are strictly ordered by (t, eid) — a deterministic total order
  that breaks timestamp ties — and satisfy t_last - t_first <= delta.
- Motif node variables are pairwise distinct; rel_type is ignored.

Matching runs in the native engine kernel (`tgms._engine.motif_match`),
which indexes the window's events once and then resolves each motif edge by
lookup rather than by scanning. It replaced a DuckDB three-way self-join —
the last third-party engine in the runtime path.
"""

from __future__ import annotations

from typing import Any

import numpy as np

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

# The catalogue is the operator contract's fixed motif set; the matching
# rules themselves live in the engine kernel, keyed by these names.
MOTIFS: dict[str, dict[str, Any]] = {
    "M_triangle_cyclic": {"n": 3},        # u->v, v->w, w->u
    "M_triangle_acyclic_1": {"n": 3},     # u->v, u->w, v->w
    "M_2node_pingpong": {"n": 3},         # u->v, v->u, u->v
    "M_star_out_3": {"n": 3},             # u->a, u->b, u->c
    "M_path_3": {"n": 3},                 # u->v->w->x
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


def _events(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    """Window events as columns, filtered exactly as the operator specifies."""
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
    return {
        "src": e["src_id"][m], "dst": e["dst_id"][m], "t": e["vt_s"][m],
        "eid": e["eid"][m], "rel_type": e["rel_type"][m],
    }


def _match(ev: dict[str, Any], motif: str, delta: int, collect: bool):
    from tgms import _engine

    return _engine.motif_match(
        motif,
        [int(v) for v in ev["src"]],
        [int(v) for v in ev["dst"]],
        [int(v) for v in ev["t"]],
        [str(v) for v in ev["eid"]],
        int(delta),
        collect,
    )


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
    ev = _events(adapter, args)
    count, _ = _match(ev, args["motif"], args["delta"], collect=False)
    return {"count": int(count), "n_events_in_window": int(len(ev["t"])),
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
    ev = _events(adapter, args)
    _, instances = _match(ev, args["motif"], args["delta"], collect=True)
    # one dense-id lookup for the whole result rather than two per edge
    needed = sorted({int(v) for tri in instances for i in tri
                     for v in (ev["src"][i], ev["dst"][i])})
    uid = dict(zip(needed, adapter.uids_for(needed))) if needed else {}
    rows = [
        {"edges": [
            {"src": uid[int(ev["src"][i])], "dst": uid[int(ev["dst"][i])],
             "t": int(ev["t"][i]), "eid": str(ev["eid"][i]),
             "rel_type": str(ev["rel_type"][i])}
            for i in tri
        ]}
        for tri in instances
    ]
    return paginate(rows, args["limit"], args["cursor"])
