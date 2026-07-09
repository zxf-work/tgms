"""Snapshot-family operators: O1 entity_history, O2 snapshot_subgraph,
O3 diff_snapshots, O10 neighborhood_evolution, O12 resolve_entities.

Kernels operate on columnar buffers (struct-of-arrays) — no per-edge Python
loops (exception documented on O12).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import InvalidArgError, LimitError
from tgms.core.model import OPEN_END
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    TIMESTAMP,
    UID,
    UID_LIST,
    operator,
    paginate,
    required,
)
from tgms.temporal.guardrails import scan_estimate

MAX_HOPS = 3


# --------------------------------------------------------------------------- #
# shared snapshot helpers                                                      #
# --------------------------------------------------------------------------- #

def edges_at(adapter: StorageAdapter, t: int, as_of_tt: int,
             rel_types: list[str] | None = None) -> dict[str, np.ndarray]:
    """Believed edge versions valid at instant t (vt_s <= t < vt_e)."""
    return adapter.edges_columnar(as_of_tt=as_of_tt, vt_min=t, vt_max=t + 1,
                                  rel_types=rel_types)


def nodes_at(adapter: StorageAdapter, t: int, as_of_tt: int) -> dict[str, np.ndarray]:
    return adapter.nodes_columnar(as_of_tt=as_of_tt, vt_min=t, vt_max=t + 1)


def bfs_node_set(edges: dict[str, np.ndarray], seed_ids: np.ndarray,
                 valid_ids: np.ndarray, hops: int) -> dict[int, int]:
    """Undirected BFS over a snapshot edge set, restricted to valid nodes.
    Returns {dense_id: hop_distance} for nodes within `hops` of the seeds."""
    src, dst = edges["src_id"], edges["dst_id"]
    valid = set(valid_ids.tolist())
    dist: dict[int, int] = {int(i): 0 for i in seed_ids if int(i) in valid}
    frontier = np.asarray(sorted(dist), dtype=np.int64)
    for h in range(1, hops + 1):
        if frontier.size == 0:
            break
        m = np.isin(src, frontier) | np.isin(dst, frontier)
        touched = np.unique(np.concatenate([src[m], dst[m]]))
        new = [int(i) for i in touched if int(i) not in dist and int(i) in valid]
        for i in new:
            dist[i] = h
        frontier = np.asarray(new, dtype=np.int64)
    return dist


def _edge_rows(adapter: StorageAdapter, edges: dict[str, np.ndarray],
               idx: np.ndarray) -> list[dict[str, Any]]:
    src_uids = adapter.uids_for(edges["src_id"][idx])
    dst_uids = adapter.uids_for(edges["dst_id"][idx])
    return [
        {"eid": edges["eid"][i], "vid": edges["vid"][i], "src": s, "dst": d,
         "rel_type": edges["rel_type"][i],
         "vt_s": int(edges["vt_s"][i]), "vt_e": int(edges["vt_e"][i])}
        for i, s, d in zip(idx.tolist(), src_uids, dst_uids)
    ]


# --------------------------------------------------------------------------- #
# O1 entity_history                                                            #
# --------------------------------------------------------------------------- #

@operator(
    "entity_history",
    {
        "uid": required(UID),
        "as_of_tt": AS_OF_TT,
        "include_edges": {"type": "boolean", "default": False},
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Ordered version list of a node (optionally with incident edge versions): "
    "all versions of `uid` believed at `as_of_tt`, ordered by vt_s.",
    cost_fn=scan_estimate,
)
def entity_history(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    uid = args["uid"]
    as_of = args["as_of_tt"]
    adapter.dense_ids([uid])  # raises E_NOT_FOUND for unknown uid
    versions = adapter.believed_node_versions(uid, as_of_tt=as_of)
    rows = [v.to_json() for v in sorted(versions, key=lambda v: (v.vt_s, v.vid))]
    for r in rows:
        # bi-temporal immutability: a version believed at as_of_tt has its
        # belief end in the future of that tt — report it as open, never
        # leak post-as_of knowledge into a pinned result
        if r["tt_e"] > as_of:
            r["tt_e"] = OPEN_END
    out = paginate(rows, args["limit"], args["cursor"])
    if args["include_edges"]:
        uid_id = int(adapter.dense_ids([uid])[0])
        edges = adapter.edges_columnar(as_of_tt=as_of)
        m = (edges["src_id"] == uid_id) | (edges["dst_id"] == uid_id)
        idx = np.flatnonzero(m)[: args["limit"]]
        out["edges"] = _edge_rows(adapter, edges, idx)
        out["edges_truncated"] = bool(int(m.sum()) > args["limit"])
    return out


# --------------------------------------------------------------------------- #
# O2 snapshot_subgraph                                                         #
# --------------------------------------------------------------------------- #

def _check_hops(args: dict[str, Any]) -> None:
    if args.get("hops", 1) > MAX_HOPS:
        raise LimitError(f"hops capped at {MAX_HOPS}")


@operator(
    "snapshot_subgraph",
    {
        "seeds": required(UID_LIST),
        "hops": {"type": "integer", "minimum": 0, "maximum": MAX_HOPS, "default": 1},
        "t_valid": required(TIMESTAMP),
        "as_of_tt": AS_OF_TT,
        "rel_types": {"type": ["array", "null"], "items": {"type": "string"}, "default": None},
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "k-hop subgraph (undirected expansion, induced edges) of the snapshot graph "
    "G(t_valid) under belief state as_of_tt. `rows` are the induced edges, "
    "paginated; `nodes` carry hop distance.",
    cost_fn=scan_estimate,
    validators=[_check_hops],
)
def snapshot_subgraph(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t, as_of = args["t_valid"], args["as_of_tt"]
    seed_ids = adapter.dense_ids(args["seeds"])
    edges = edges_at(adapter, t, as_of, args["rel_types"])
    nodes = nodes_at(adapter, t, as_of)
    dist = bfs_node_set(edges, seed_ids, nodes["uid_id"], args["hops"])
    in_set = np.isin(edges["src_id"], list(dist)) & np.isin(edges["dst_id"], list(dist))
    idx = np.flatnonzero(in_set)  # already ordered by (vt_s, vid)
    node_pos = {int(i): k for k, i in enumerate(nodes["uid_id"].tolist())}
    node_rows = sorted(
        ({"uid": nodes["uid"][node_pos[i]], "label": nodes["label"][node_pos[i]],
          "hop": h} for i, h in dist.items()),
        key=lambda r: (r["hop"], r["uid"]))
    out = paginate(_edge_rows(adapter, edges, idx), args["limit"], args["cursor"])
    out["nodes"] = node_rows[: args["limit"]]
    out["nodes_total"] = len(node_rows)
    out["nodes_truncated"] = len(node_rows) > args["limit"]
    out["truncated"] = out["truncated"] or out["nodes_truncated"]
    return out


# --------------------------------------------------------------------------- #
# O3 diff_snapshots                                                            #
# --------------------------------------------------------------------------- #

SCOPE = {
    "type": ["object", "null"],
    "default": None,
    "properties": {"seeds": UID_LIST,
                   "hops": {"type": "integer", "minimum": 0, "maximum": MAX_HOPS}},
    "required": ["seeds", "hops"],
    "additionalProperties": False,
    "description": "restrict the diff to nodes within `hops` of `seeds` in "
                   "either snapshot (edges need both endpoints in scope)",
}


def _point_state(adapter: StorageAdapter, t: int, as_of: int):
    """(node columnar, edge columnar) valid at t, plus identity->index maps."""
    nodes, edges = nodes_at(adapter, t, as_of), edges_at(adapter, t, as_of)
    node_map = {u: i for i, u in enumerate(nodes["uid"].tolist())}
    edge_map = {e: i for i, e in enumerate(edges["eid"].tolist())}
    return nodes, edges, node_map, edge_map


@operator(
    "diff_snapshots",
    {
        "t1": required(TIMESTAMP),
        "t2": required(TIMESTAMP),
        "as_of_tt": AS_OF_TT,
        "scope": SCOPE,
        "limit": LIMIT,
    },
    "Delta between snapshots G(t1) and G(t2) under one belief state: "
    "nodes/edges added & removed (by logical identity) and props_changed. "
    "Each list is independently capped at `limit` with *_total counts.",
    cost_fn=scan_estimate,
)
def diff_snapshots(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t1, t2, as_of, limit = args["t1"], args["t2"], args["as_of_tt"], args["limit"]
    n1, e1, nmap1, emap1 = _point_state(adapter, t1, as_of)
    n2, e2, nmap2, emap2 = _point_state(adapter, t2, as_of)

    allowed: set[str] | None = None
    if args["scope"] is not None:
        seed_ids = adapter.dense_ids(args["scope"]["seeds"])
        hops = args["scope"]["hops"]
        d1 = bfs_node_set(e1, seed_ids, n1["uid_id"], hops)
        d2 = bfs_node_set(e2, seed_ids, n2["uid_id"], hops)
        allowed = set(adapter.uids_for(sorted(set(d1) | set(d2))))

    def node_ok(uid: str) -> bool:
        return allowed is None or uid in allowed

    def edge_ok(edges, i) -> bool:
        if allowed is None:
            return True
        s, d = adapter.uids_for([int(edges["src_id"][i]), int(edges["dst_id"][i])])
        return s in allowed and d in allowed

    nodes_added = sorted(u for u in nmap2 if u not in nmap1 and node_ok(u))
    nodes_removed = sorted(u for u in nmap1 if u not in nmap2 and node_ok(u))
    edges_added = sorted(e for e in emap2 if e not in emap1 and edge_ok(e2, emap2[e]))
    edges_removed = sorted(e for e in emap1 if e not in emap2 and edge_ok(e1, emap1[e]))

    # props_changed: same identity present at both instants via different
    # version rows with different content
    changed: list[dict[str, Any]] = []
    common_nodes = [u for u in nmap1 if u in nmap2 and node_ok(u)]
    cand = [(u, n1["vid"][nmap1[u]], n2["vid"][nmap2[u]]) for u in common_nodes
            if n1["vid"][nmap1[u]] != n2["vid"][nmap2[u]]]
    props = adapter.props_for_vids(
        "node", sorted({v for _, a, b in cand for v in (a, b)}))
    for u, va, vb in sorted(cand):
        la, lb = n1["label"][nmap1[u]], n2["label"][nmap2[u]]
        if props.get(va) != props.get(vb) or la != lb:
            changed.append({"kind": "node", "id": u,
                            "from": {"label": la, "props": props.get(va)},
                            "to": {"label": lb, "props": props.get(vb)}})
    common_edges = [e for e in emap1 if e in emap2 and edge_ok(e1, emap1[e])]
    cand_e = [(e, e1["vid"][emap1[e]], e2["vid"][emap2[e]]) for e in common_edges
              if e1["vid"][emap1[e]] != e2["vid"][emap2[e]]]
    props_e = adapter.props_for_vids(
        "edge", sorted({v for _, a, b in cand_e for v in (a, b)}))
    for e, va, vb in sorted(cand_e):
        if props_e.get(va) != props_e.get(vb):
            changed.append({"kind": "edge", "id": e,
                            "from": {"props": props_e.get(va)},
                            "to": {"props": props_e.get(vb)}})

    def edge_desc(edges, emap, eid):
        i = emap[eid]
        s, d = adapter.uids_for([int(edges["src_id"][i]), int(edges["dst_id"][i])])
        return {"eid": eid, "src": s, "dst": d, "rel_type": edges["rel_type"][i]}

    out = {
        "nodes_added": nodes_added[:limit], "nodes_added_total": len(nodes_added),
        "nodes_removed": nodes_removed[:limit], "nodes_removed_total": len(nodes_removed),
        "edges_added": [edge_desc(e2, emap2, e) for e in edges_added[:limit]],
        "edges_added_total": len(edges_added),
        "edges_removed": [edge_desc(e1, emap1, e) for e in edges_removed[:limit]],
        "edges_removed_total": len(edges_removed),
        "props_changed": changed[:limit], "props_changed_total": len(changed),
    }
    out["truncated"] = any(
        out[f"{k}_total"] > limit
        for k in ("nodes_added", "nodes_removed", "edges_added", "edges_removed",
                  "props_changed"))
    return out


# --------------------------------------------------------------------------- #
# O10 neighborhood_evolution                                                   #
# --------------------------------------------------------------------------- #

MAX_BUCKETS = 2_000


def _check_t1_t2(args: dict[str, Any]) -> None:
    if not (args["t1"] < args["t2"]):
        raise InvalidArgError("neighborhood_evolution requires t1 < t2")


@operator(
    "neighborhood_evolution",
    {
        "uid": required(UID),
        "t1": required(TIMESTAMP),
        "t2": required(TIMESTAMP),
        "as_of_tt": AS_OF_TT,
        "stride": {"type": ["integer", "null"], "minimum": 1, "default": None,
                   "description": "degree-series bucket width; default (t2-t1)/20"},
        "limit": LIMIT,
    },
    "Neighbors of `uid` gained/lost between G(t1) and G(t2) (undirected), plus "
    "an incident-active-edge-count series sampled at bucket starts in [t1, t2).",
    cost_fn=scan_estimate,
    validators=[_check_t1_t2],
)
def neighborhood_evolution(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    uid, t1, t2, as_of = args["uid"], args["t1"], args["t2"], args["as_of_tt"]
    uid_id = int(adapter.dense_ids([uid])[0])
    stride = args["stride"] or max(1, (t2 - t1) // 20)
    n_buckets = -(-(t2 - t1) // stride)
    if n_buckets > MAX_BUCKETS:
        raise LimitError(f"bucket count {n_buckets} exceeds cap {MAX_BUCKETS}; "
                         "increase stride")

    def neighbors(t: int) -> set[str]:
        e = edges_at(adapter, t, as_of)
        m_out = e["src_id"] == uid_id
        m_in = e["dst_id"] == uid_id
        ids = np.unique(np.concatenate([e["dst_id"][m_out], e["src_id"][m_in]]))
        return set(adapter.uids_for([i for i in ids.tolist() if i != uid_id]))

    n_t1, n_t2 = neighbors(t1), neighbors(t2)
    gained, lost = sorted(n_t2 - n_t1), sorted(n_t1 - n_t2)

    # incident active-edge count at each bucket start, one interval scan
    e = adapter.edges_columnar(as_of_tt=as_of, vt_min=t1, vt_max=t2)
    m = (e["src_id"] == uid_id) | (e["dst_id"] == uid_id)
    starts = np.sort(e["vt_s"][m])
    ends = np.sort(e["vt_e"][m])
    bucket_starts = np.arange(t1, t2, stride, dtype=np.int64)
    deg = (np.searchsorted(starts, bucket_starts, side="right")
           - np.searchsorted(ends, bucket_starts, side="right"))
    series = [{"t": int(t), "degree": int(d)} for t, d in zip(bucket_starts, deg)]

    limit = args["limit"]
    return {
        "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
        "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
        "degree_series": series, "stride": stride,
        "truncated": len(gained) > limit or len(lost) > limit,
    }


# --------------------------------------------------------------------------- #
# O12 resolve_entities                                                         #
# --------------------------------------------------------------------------- #

@operator(
    "resolve_entities",
    {
        "query": required({"type": "string", "minLength": 1}),
        "label": {"type": ["string", "null"], "default": None},
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Name/uid lookup returning uids — exact uid match first, then "
    "case-insensitive substring on uid and on the `name` prop of any believed "
    "version. Planners must obtain uids from this operator (grounding rule).",
    cost_fn=scan_estimate,
)
def resolve_entities(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    # NOTE: Python row loop tolerated here — resolve is a small-table lookup,
    # not a hot-path scan (TODO M3: move behind a name index).
    q = args["query"]
    ql = q.lower()
    as_of = args["as_of_tt"]
    latest: dict[str, Any] = {}
    matched: dict[str, int] = {}
    for v in adapter.all_node_versions():
        if not v.believed_at(as_of):
            continue
        cur = latest.get(v.uid)
        if cur is None or v.vt_s > cur.vt_s:
            latest[v.uid] = v
        name = str(v.props.get("name", ""))
        if v.uid == q:
            score = 0
        elif ql in v.uid.lower():
            score = 1
        elif name and ql in name.lower():
            score = 2
        else:
            continue
        matched[v.uid] = min(matched.get(v.uid, 9), score)
    rows = []
    for uid, score in matched.items():
        v = latest[uid]
        if args["label"] is not None and v.label != args["label"]:
            continue
        rows.append({"uid": uid, "label": v.label,
                     "name": v.props.get("name"), "match": score})
    rows.sort(key=lambda r: (r["match"], r["uid"]))
    return paginate(rows, args["limit"], args["cursor"])
