"""The 13 Cypher texts, one per registered artifact family (design memo
§2.3), and the parameter builders that turn a registered artifact's own
`args` (the identical dict TGMS's operator registry validates) into the
query's Bolt parameters.

F11 (`temporal_reachability`) has no single-statement form in 5.26 (memo:
quantified path patterns cannot carry arrival time across iterations, and
Cypher 25's `allReduce` is not in 5.26) — it is a client-driven iterated
query, in `reachability.py`. F13 (`compute` on the ∅-scope control) needs no
database access at all: the registered instance is the literal-count
control (`fn="count", input=[{"x":1},{"x":2}]`), always 2, never read from
the graph (memo: "constant").

Every other family is one parameterized statement, assembled here exactly
as the memo's §2.3 text (F12's four hop-pruned branches are generated
programmatically — `f12_query` — rather than hand-duplicated, since they
differ only in how many hops they unroll).

A family whose registered args fall outside what its query below assumes
(e.g. `as_of_tt` other than belief-at-current, multiple `snapshot_subgraph`
seeds, `version_history` over edges) is out of the frozen scope: callers get
`NotExpressibleError`, not a silently different query.
"""
from __future__ import annotations

from .canon import OPEN_END

OPEN = OPEN_END


class NotExpressibleError(ValueError):
    """Registered args fall outside what the frozen Cypher text covers."""


# --------------------------------------------------------------------------- #
# F1 entity_history                                                            #
# --------------------------------------------------------------------------- #

F1_ENTITY_HISTORY = """
CALL { MATCH (v:NV {uid:$uid}) WHERE v.tt_e=$O WITH v ORDER BY v.vt_s, v.vid
       RETURN collect(v{.vid,.uid,.label,.vt_s,.vt_e,.tt_s,.tt_e,.props,.source,.provenance_ref}) AS rs }
CALL { MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH DISTINCT e ORDER BY e.vt_s, e.vid
       RETURN collect(e{.eid,.vid,src:startNode(e).uid,dst:endNode(e).uid,.rel_type,.vt_s,.vt_e}) AS es }
RETURN rs[0..$lim] AS rows, size(rs) AS rows_total, es[0..$lim] AS edges, size(es) > $lim AS edges_truncated
""".strip()


def f1_params(args: dict, limit: int = 100) -> dict:
    if args.get("as_of_tt", OPEN_END) != OPEN_END:
        raise NotExpressibleError("entity_history: only as_of_tt=OPEN_END is frozen-scope")
    if not args.get("include_edges", False):
        raise NotExpressibleError("entity_history: registered scope always has include_edges=True")
    return {"uid": args["uid"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F2 version_history (node, belief current)                                   #
# --------------------------------------------------------------------------- #

F2_VERSION_HISTORY = """
MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s < $t_b AND $t_a < v.vt_e WITH v ORDER BY v.tt_s, v.vid
WITH collect(v{.vid,.uid,.label,.vt_s,.vt_e,.tt_s,.tt_e}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total
""".strip()


def f2_params(args: dict, limit: int = 100) -> dict:
    if args.get("kind") != "node" or args.get("belief", "current") != "current":
        raise NotExpressibleError("version_history: only kind=node, belief=current is frozen-scope")
    if args.get("rel_types") is not None:
        raise NotExpressibleError("version_history: rel_types filter is out of frozen scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F3 snapshot_subgraph (one seed, hops 1)                                      #
# --------------------------------------------------------------------------- #

F3_SNAPSHOT_SUBGRAPH = """
CALL { MATCH (s:NV {uid:$seed}) WHERE s.tt_e=$O AND s.vt_s<=$t<s.vt_e RETURN collect({uid:s.uid,label:s.label,hop:0}) AS h0 }
CALL { WITH h0 MATCH (:E {uid:$seed})-[e:EV]-(y:E) WHERE size(h0)>0 AND y.uid<>$seed AND e.tt_e=$O AND e.vt_s<=$t<e.vt_e
       MATCH (w:NV {uid:y.uid}) WHERE w.tt_e=$O AND w.vt_s<=$t<w.vt_e WITH DISTINCT w ORDER BY w.uid
       RETURN collect({uid:w.uid,label:w.label,hop:1}) AS h1 }
WITH h0+h1 AS ns WITH ns, [n IN ns | n.uid] AS us
CALL { WITH us MATCH (a:E)-[e:EV]->(b:E) WHERE a.uid IN us AND b.uid IN us AND e.tt_e=$O AND e.vt_s<=$t<e.vt_e
       WITH a,b,e ORDER BY e.vt_s, e.vid RETURN collect(e{.eid,.vid,src:a.uid,dst:b.uid,.rel_type,.vt_s,.vt_e}) AS es }
RETURN es[0..$lim] AS rows, size(es) AS rows_total, ns[0..$lim] AS nodes, size(ns) AS nodes_total
""".strip()


def f3_params(args: dict, limit: int = 100) -> dict:
    if args.get("hops", 1) != 1 or len(args["seeds"]) != 1:
        raise NotExpressibleError("snapshot_subgraph: only a single seed at hops=1 is frozen-scope")
    if args.get("rel_types") is not None:
        raise NotExpressibleError("snapshot_subgraph: rel_types filter is out of frozen scope")
    return {"seed": args["seeds"][0], "t": args["t_valid"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F4 diff_snapshots (whole graph, scope=null)                                  #
# --------------------------------------------------------------------------- #

F4_DIFF_SNAPSHOTS = """
CALL { MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s<=$t1<v.vt_e RETURN collect(v.uid) AS u1 }
CALL { MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s<=$t2<v.vt_e RETURN collect(v.uid) AS u2 }
WITH u1, u2, apoc.coll.sort(apoc.coll.subtract(u2,u1)) AS na, apoc.coll.sort(apoc.coll.subtract(u1,u2)) AS nr
CALL { MATCH (a:E)-[e:EV]->(b:E) WHERE e.tt_e=$O AND e.vt_s<=$t2<e.vt_e
         AND NOT EXISTS { MATCH ()-[f:EV {eid:e.eid}]->() WHERE f.tt_e=$O AND f.vt_s<=$t1<f.vt_e }
       WITH a,b,e ORDER BY e.eid RETURN collect({eid:e.eid,src:a.uid,dst:b.uid,rel_type:e.rel_type}) AS ea }
CALL { MATCH (a:E)-[e:EV]->(b:E) WHERE e.tt_e=$O AND e.vt_s<=$t1<e.vt_e
         AND NOT EXISTS { MATCH ()-[f:EV {eid:e.eid}]->() WHERE f.tt_e=$O AND f.vt_s<=$t2<f.vt_e }
       WITH a,b,e ORDER BY e.eid RETURN collect({eid:e.eid,src:a.uid,dst:b.uid,rel_type:e.rel_type}) AS er }
CALL { MATCH (v1:NV) WHERE v1.tt_e=$O AND v1.vt_s<=$t1<v1.vt_e
       MATCH (v2:NV) WHERE v2.tt_e=$O AND v2.vt_s<=$t2<v2.vt_e AND v2.uid=v1.uid AND v2.vid<>v1.vid
       WITH v1,v2 WHERE v1.props<>v2.props OR v1.label<>v2.label
       WITH v1,v2 ORDER BY v1.uid
       RETURN collect({kind:'node',id:v1.uid,from:{label:v1.label,props:v1.props},
                       to:{label:v2.label,props:v2.props}}) AS pn }
CALL { MATCH (:E)-[e1:EV]->() WHERE e1.tt_e=$O AND e1.vt_s<=$t1<e1.vt_e
       MATCH (:E)-[e2:EV]->() WHERE e2.tt_e=$O AND e2.vt_s<=$t2<e2.vt_e AND e2.eid=e1.eid AND e2.vid<>e1.vid
       WITH e1,e2 WHERE e1.props<>e2.props
       WITH e1,e2 ORDER BY e1.eid
       RETURN collect({kind:'edge',id:e1.eid,from:{props:e1.props},to:{props:e2.props}}) AS pe }
WITH na,nr,ea,er,pn,pe, pn+pe AS pc
RETURN na[0..$lim] AS nodes_added, size(na) AS nodes_added_total,
       nr[0..$lim] AS nodes_removed, size(nr) AS nodes_removed_total,
       ea[0..$lim] AS edges_added, size(ea) AS edges_added_total,
       er[0..$lim] AS edges_removed, size(er) AS edges_removed_total,
       pc[0..$lim] AS props_changed, size(pc) AS props_changed_total
""".strip()


def f4_params(args: dict, limit: int = 100) -> dict:
    if args.get("scope") is not None:
        raise NotExpressibleError("diff_snapshots: scoped (hop-restricted) diff is out of frozen scope")
    return {"t1": args["t1"], "t2": args["t2"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F5 neighborhood_evolution                                                    #
# --------------------------------------------------------------------------- #

F5_NEIGHBORHOOD_EVOLUTION = """
MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH DISTINCT e
WITH collect({vs:e.vt_s, ve:e.vt_e, nb:CASE WHEN startNode(e).uid=$uid THEN endNode(e).uid ELSE startNode(e).uid END}) AS inc
WITH inc, apoc.coll.sort(apoc.coll.toSet([p IN inc WHERE p.nb<>$uid AND p.vs<=$t1<p.ve | p.nb])) AS n1,
          apoc.coll.sort(apoc.coll.toSet([p IN inc WHERE p.nb<>$uid AND p.vs<=$t2<p.ve | p.nb])) AS n2
WITH inc, apoc.coll.sort(apoc.coll.subtract(n2,n1)) AS g, apoc.coll.sort(apoc.coll.subtract(n1,n2)) AS l
RETURN g[0..$lim] AS neighbors_gained, size(g) AS neighbors_gained_total, l[0..$lim] AS neighbors_lost, size(l) AS neighbors_lost_total,
       [bs IN range($t1,$t2-1,$stride) | {t:bs, degree:size([p IN inc WHERE p.vs<=bs<p.ve])}] AS degree_series,
       $stride AS stride
""".strip()


def f5_params(args: dict, limit: int = 100) -> dict:
    return {"uid": args["uid"], "t1": args["t1"], "t2": args["t2"],
            "stride": args["stride"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F6 aggregate_events (group_by endpoint src, count)                          #
# --------------------------------------------------------------------------- #

F6_AGGREGATE_EVENTS = """
MATCH (a:E)-[e:EV]->() WHERE e.tt_e=$O AND $t_a<=e.vt_s<$t_b WITH a.uid AS src, count(*) AS c ORDER BY src
WITH collect({src:src, count:c}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total
""".strip()


def f6_params(args: dict, limit: int = 100) -> dict:
    gb = args["group_by"]
    aggs = args["aggregates"]
    if gb != [{"dim": "endpoint", "role": "src"}] or aggs != [{"agg": "count"}]:
        raise NotExpressibleError("aggregate_events: only group_by=[endpoint/src], "
                                  "aggregates=[count] is frozen-scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F7 graph_metric_timeseries (edge_event_count)                               #
# --------------------------------------------------------------------------- #

F7_GRAPH_METRIC_TIMESERIES = """
UNWIND range($t_a,$t_b-1,$stride) AS bs WITH bs, CASE WHEN bs+$stride<$t_b THEN bs+$stride ELSE $t_b END AS be
CALL { WITH bs,be MATCH ()-[e:EV]->() WHERE e.tt_e=$O AND bs<=e.vt_s<be RETURN count(e) AS v }
WITH bs,be,v ORDER BY bs WITH collect({t_a:bs,t_b:be,value:v}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total, size(rs) AS n_buckets
""".strip()


def f7_params(args: dict, limit: int = 100) -> dict:
    if args.get("metric") != "edge_event_count":
        raise NotExpressibleError("graph_metric_timeseries: only metric=edge_event_count is frozen-scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "stride": args["stride"], "O": OPEN,
            "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F8 burst_detection (node_activity, zscore, w=10, z=3.0)                      #
# --------------------------------------------------------------------------- #

F8_BURST_DETECTION = """
MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH collect(DISTINCT e) AS es
WITH [bs IN range($t_a,$t_b-1,$stride) | {bs:bs, be:CASE WHEN bs+$stride<$t_b THEN bs+$stride ELSE $t_b END}] AS bk, es
WITH bk, [b IN bk | toFloat(size([e IN es WHERE b.bs<=e.vt_s<b.be]))] AS x
WITH bk, x, [i IN range(1,size(x)-1) | {i:i, h:x[CASE WHEN i<$w THEN 0 ELSE i-$w END..i]}] AS hs
WITH bk, x, [p IN hs | {i:p.i, h:p.h, m:reduce(s=0.0, y IN p.h | s+y)/size(p.h)}] AS ms
WITH bk, x, [p IN ms | {i:p.i, m:p.m, sd:sqrt(reduce(s=0.0, y IN p.h | s+(y-p.m)^2)/size(p.h))}] AS ss
WITH bk, x, [p IN ss | {i:p.i, sc:round(CASE WHEN p.sd>0 THEN abs(x[p.i]-p.m)/p.sd WHEN x[p.i]=p.m THEN 0.0 ELSE 1e9 END, 9)}] AS sc
WITH x, [p IN sc WHERE p.sc >= $z | {t_a:bk[p.i].bs, t_b:bk[p.i].be, value:x[p.i], score:p.sc}] AS fl
RETURN fl[0..$lim] AS rows, size(fl) AS rows_total, size(x) AS n_buckets
""".strip()


def f8_params(args: dict, limit: int = 100) -> dict:
    if args["target"]["kind"] != "node_activity" or args.get("method", "zscore") != "zscore":
        raise NotExpressibleError("burst_detection: only target.kind=node_activity, "
                                  "method=zscore is frozen-scope")
    w = args["window"]
    p = args.get("params") or {}
    return {"uid": args["target"]["uid"], "t_a": w["t_a"], "t_b": w["t_b"],
            "stride": args["stride"], "w": p.get("w", 10), "z": p.get("z", 3.0),
            "O": OPEN, "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F9 count_temporal_motifs (M_2node_pingpong)                                  #
# --------------------------------------------------------------------------- #

F9_COUNT_TEMPORAL_MOTIFS = """
MATCH (u:E)-[a:EV]->(v:E) WHERE u<>v AND a.tt_e=$O AND $t_a<=a.vt_s<$t_b
MATCH (v)-[b:EV]->(u) WHERE b.tt_e=$O AND b.vt_s<$t_b AND b.vt_s-a.vt_s<=$d AND (b.vt_s>a.vt_s OR (b.vt_s=a.vt_s AND b.eid>a.eid))
MATCH (u)-[c:EV]->(v) WHERE c.tt_e=$O AND c.vt_s<$t_b AND c.vt_s-a.vt_s<=$d AND (c.vt_s>b.vt_s OR (c.vt_s=b.vt_s AND c.eid>b.eid))
WITH count(*) AS n CALL { MATCH ()-[e:EV]->() WHERE e.tt_e=$O AND $t_a<=e.vt_s<$t_b RETURN count(e) AS nw }
RETURN n AS count, nw AS n_events_in_window
""".strip()


def f9_params(args: dict, limit: int = 100) -> dict:
    if args.get("motif") != "M_2node_pingpong" or args.get("node_filter") is not None:
        raise NotExpressibleError("count_temporal_motifs: only motif=M_2node_pingpong, "
                                  "node_filter=null is frozen-scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "d": args["delta"], "O": OPEN}


# --------------------------------------------------------------------------- #
# F10 find_temporal_motif_instances                                            #
# --------------------------------------------------------------------------- #

#: F9's three MATCH clauses, then two sub-queries: count(*) and the ordered,
#: limited enumeration of the three (u,v) edges per instance (memo §2.3).
F10_FIND_TEMPORAL_MOTIF_INSTANCES = """
CALL {
  MATCH (u:E)-[a:EV]->(v:E) WHERE u<>v AND a.tt_e=$O AND $t_a<=a.vt_s<$t_b
  MATCH (v)-[b:EV]->(u) WHERE b.tt_e=$O AND b.vt_s<$t_b AND b.vt_s-a.vt_s<=$d AND (b.vt_s>a.vt_s OR (b.vt_s=a.vt_s AND b.eid>a.eid))
  MATCH (u)-[c:EV]->(v) WHERE c.tt_e=$O AND c.vt_s<$t_b AND c.vt_s-a.vt_s<=$d AND (c.vt_s>b.vt_s OR (c.vt_s=b.vt_s AND c.eid>b.eid))
  RETURN count(*) AS total
}
CALL {
  MATCH (u:E)-[a:EV]->(v:E) WHERE u<>v AND a.tt_e=$O AND $t_a<=a.vt_s<$t_b
  MATCH (v)-[b:EV]->(u) WHERE b.tt_e=$O AND b.vt_s<$t_b AND b.vt_s-a.vt_s<=$d AND (b.vt_s>a.vt_s OR (b.vt_s=a.vt_s AND b.eid>a.eid))
  MATCH (u)-[c:EV]->(v) WHERE c.tt_e=$O AND c.vt_s<$t_b AND c.vt_s-a.vt_s<=$d AND (c.vt_s>b.vt_s OR (c.vt_s=b.vt_s AND c.eid>b.eid))
  WITH u,v,a,b,c ORDER BY a.vt_s,a.eid,b.vt_s,b.eid,c.vt_s,c.eid LIMIT $lim
  RETURN collect({edges:[{src:u.uid,dst:v.uid,t:a.vt_s,eid:a.eid,rel_type:a.rel_type},
                        {src:v.uid,dst:u.uid,t:b.vt_s,eid:b.eid,rel_type:b.rel_type},
                        {src:u.uid,dst:v.uid,t:c.vt_s,eid:c.eid,rel_type:c.rel_type}]}) AS instances
}
RETURN instances AS rows, total AS rows_total, total > $lim AS truncated
""".strip()


def f10_params(args: dict, limit: int = 25) -> dict:
    if args.get("motif") != "M_2node_pingpong" or args.get("node_filter") is not None:
        raise NotExpressibleError("find_temporal_motif_instances: only motif=M_2node_pingpong, "
                                  "node_filter=null is frozen-scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "d": args["delta"], "O": OPEN,
            "lim": args.get("limit", limit)}


# --------------------------------------------------------------------------- #
# F11 temporal_reachability — iterated, see reachability.py                   #
# --------------------------------------------------------------------------- #

#: One round, client-driven (memo §2.3): `$front` = [{uid, arr}], the
#: frontier whose label just improved (or the seed on round 0).
F11_REACHABILITY_ROUND = """
UNWIND $front AS f MATCH (:E {uid:f.uid})-[e:EV]->(y:E) WHERE e.tt_e=$O AND e.vt_e>$t_a AND e.vt_s<$t_b
WITH y, e, CASE WHEN f.arr>e.vt_s THEN f.arr ELSE e.vt_s END AS tau WHERE tau<e.vt_e AND tau<$t_b
RETURN y.uid AS uid, min(tau) AS arr
""".strip()


def f11_window_params(args: dict) -> dict:
    if args.get("delta_max_wait") is not None:
        raise NotExpressibleError("temporal_reachability: delta_max_wait is out of frozen scope "
                                  "(no single-round relaxation form)")
    if args.get("direction", "out") != "out":
        raise NotExpressibleError("temporal_reachability: only direction=out is frozen-scope")
    w = args["window"]
    return {"t_a": w["t_a"], "t_b": w["t_b"], "O": OPEN}


# --------------------------------------------------------------------------- #
# F12 temporal_paths — k 2, max_hops 4: hop-pruned branches, UNION ALL         #
# --------------------------------------------------------------------------- #

def f12_branch(hops: int) -> str:
    """One fixed-length (`hops`-edge) node-simple time-respecting branch,
    pruned at every hop (memo §2.3): `tau_i = max(tau_{i-1}, r_i.vt_s) <
    min(r_i.vt_e, $t_b)`, interior nodes distinct from src/dst and from each
    other, only the last node is `$dst`.
    """
    if hops < 1:
        raise ValueError("hops must be >= 1")
    lines: list[str] = ["MATCH (n0:E {uid:$src})"]
    prev_t = "$t_a"
    for i in range(1, hops + 1):
        is_last = i == hops
        target = "(nL:E {uid:$dst})" if is_last else f"(n{i}:E)"
        distinct_clause = "" if is_last else (
            f" AND n{i}.uid<>$dst AND n{i}.uid<>$src"
            + "".join(f" AND n{i}.uid<>n{j}.uid" for j in range(1, i)))
        lines.append(f"MATCH (n{i-1})-[r{i}:EV]->{target} "
                     f"WHERE r{i}.tt_e=$O AND r{i}.vt_e>$t_a AND r{i}.vt_s<$t_b"
                     + distinct_clause)
        tvar = f"t{i}"
        lines.append(
            f"WITH *, CASE WHEN {prev_t}>r{i}.vt_s THEN {prev_t} ELSE r{i}.vt_s END AS {tvar} "
            f"WHERE {tvar}<r{i}.vt_e AND {tvar}<$t_b")
        prev_t = tvar
    key_items = ",".join(f"[r{i}.vt_s,r{i}.eid]" for i in range(1, hops + 1))
    edge_items = []
    prev_name = "n0"
    for i in range(1, hops + 1):
        this_name = "nL" if i == hops else f"n{i}"
        edge_items.append(f"{{src:{prev_name}.uid,dst:{this_name}.uid,rel_type:r{i}.rel_type,"
                          f"eid:r{i}.eid,t:r{i}.vt_s}}")
        prev_name = this_name
    lines.append(f"RETURN {prev_t} AS arrival, {hops} AS hops, [{key_items}] AS key, "
                 f"[{','.join(edge_items)}] AS edges")
    return "\n".join(lines)


def f12_query(max_hops: int = 4) -> str:
    # Each branch is its own bare statement; UNION ALL them directly (no
    # nested CALL needed, since none declares outer variables).
    branches = [f12_branch(h) for h in range(1, max_hops + 1)]
    unioned = "\nUNION ALL\n".join(branches)
    return (
        "CALL {\n" + unioned + "\n}\n"
        "WITH arrival, hops, key, edges ORDER BY arrival, hops, key\n"
        "WITH collect({arrival:arrival, hops:hops, edges:edges}) AS all_paths\n"
        "RETURN all_paths[0..$k] AS rows, size(all_paths) AS rows_total, "
        "size(all_paths) > $k AS truncated"
    )


def f12_params(args: dict) -> dict:
    if args.get("as_of_tt", OPEN_END) != OPEN_END:
        raise NotExpressibleError("temporal_paths: only as_of_tt=OPEN_END is frozen-scope")
    w = args["window"]
    return {"src": args["src"], "dst": args["dst"], "t_a": w["t_a"], "t_b": w["t_b"],
            "k": args.get("k", 5), "O": OPEN}, args.get("max_hops", 4)


# --------------------------------------------------------------------------- #
# F13 compute (∅-scope control) — no database access                          #
# --------------------------------------------------------------------------- #

F13_COMPUTE_CONTROL = "RETURN 2 AS value"


def f13_params(args: dict) -> dict:
    if args.get("fn") != "count" or args.get("input") != [{"x": 1}, {"x": 2}]:
        raise NotExpressibleError("compute: only the literal-count ∅-scope control is frozen-scope")
    return {}


#: `benchmarks/external-v1/neo4j-recompute-<date>.json`'s `config.cypher`
#: keys every text by family name, for the README and the sha-gate.
FAMILY_TEXT: dict[str, str] = {
    "entity_history": F1_ENTITY_HISTORY,
    "version_history": F2_VERSION_HISTORY,
    "snapshot_subgraph": F3_SNAPSHOT_SUBGRAPH,
    "diff_snapshots": F4_DIFF_SNAPSHOTS,
    "neighborhood_evolution": F5_NEIGHBORHOOD_EVOLUTION,
    "aggregate_events": F6_AGGREGATE_EVENTS,
    "graph_metric_timeseries": F7_GRAPH_METRIC_TIMESERIES,
    "burst_detection": F8_BURST_DETECTION,
    "count_temporal_motifs": F9_COUNT_TEMPORAL_MOTIFS,
    "find_temporal_motif_instances": F10_FIND_TEMPORAL_MOTIF_INSTANCES,
    "temporal_reachability": F11_REACHABILITY_ROUND,  # per-round text; see reachability.py
    "temporal_paths": f12_query(4),
    "compute": F13_COMPUTE_CONTROL,
}
