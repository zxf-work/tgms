"""Registry queries in Cypher — first slice (evaluation plan §4.1, Phase 3).

Same oracle discipline as the SQL baselines: each function returns the
operator's logical payload for `canonical_hash` to judge, verified before
timed, and anything that cannot be written faithfully stays out of
`QUERIES`. This slice covers the point and aggregation shapes; the
traversal family — Cypher's home turf — follows, and is the comparison
this baseline exists for.

Cypher note: string comparison is codepoint-ordered, matching the
operators; ORDER BY on two keys carries the (vt_s, vid) contract.
"""

from __future__ import annotations

import json
from typing import Any

OPEN_END = 2**62


def clamp(as_of_tt: int) -> int:
    return min(as_of_tt, OPEN_END - 1)


def belief(as_of_tt: int, var: str) -> str:
    if as_of_tt >= OPEN_END:
        return f"{var}.tt_e = {OPEN_END}"
    a = clamp(as_of_tt)
    return f"{var}.tt_s <= {a} AND {a} < {var}.tt_e"


def _page(total: int, returned: int) -> dict[str, Any]:
    return {"rows_total": total, "truncated": total > returned}


def entity_history(drv, *, uid: str, as_of_tt: int = OPEN_END,
                   limit: int = 100) -> dict[str, Any]:
    with drv.session() as s:
        total = s.run(
            f"MATCH (n:NodeVersion {{uid: $uid}}) WHERE {belief(as_of_tt, 'n')} "
            f"RETURN count(n)", uid=uid).single()[0]
        recs = s.run(
            f"MATCH (n:NodeVersion {{uid: $uid}}) WHERE {belief(as_of_tt, 'n')} "
            f"RETURN n ORDER BY n.vt_s, n.vid LIMIT {int(limit)}",
            uid=uid).data()
    out = []
    for rec in recs:
        n = rec["n"]
        out.append({"vid": n["vid"], "uid": n["uid"], "label": n["label"],
                    "vt_s": n["vt_s"], "vt_e": n["vt_e"], "tt_s": n["tt_s"],
                    "tt_e": OPEN_END, "props": json.loads(n["props"]),
                    "source": n["source"] or "ingest",
                    # Neo4j stores no property for a null value, so absent
                    # and null are the same fact here
                    "provenance_ref": n.get("prov")})
    return {"rows": out, **_page(total, len(out))}


def graph_metric_timeseries(drv, *, metric: str, window: dict, stride: int,
                            as_of_tt: int = OPEN_END,
                            limit: int = 100) -> dict[str, Any]:
    if metric != "edge_event_count":
        raise NotImplementedError(metric)
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    with drv.session() as s:
        got = {r["i"]: r["c"] for r in s.run(
            f"MATCH ()-[r:E]->() WHERE {belief(as_of_tt, 'r')} "
            f"AND r.vt_s >= $ta AND r.vt_s < $tb "
            f"RETURN (r.vt_s - $ta) / $stride AS i, count(r) AS c",
            ta=t_a, tb=t_b, stride=stride).data()}
    rows = [{"t_a": t_a + i * stride,
             "t_b": min(t_a + (i + 1) * stride, t_b),
             "value": int(got.get(i, 0))}
            for i in range(min(n, limit))]
    return {"rows": rows, "rows_total": n, "truncated": n > len(rows),
            "n_buckets": n}


def aggregate_events(drv, *, group_by: list, aggregates: list, window: dict,
                     stride: int | None = None, rel_types: list | None = None,
                     as_of_tt: int = OPEN_END, limit: int = 100,
                     cursor: str | None = None) -> dict[str, Any]:
    """O14, the registry's flagship grouped-aggregation shape only.

    Grouped aggregation is the one query family Cypher expresses as
    directly as SQL does, so this twin is a fair reading of the graph
    engines on it — no Python-driven rounds, one statement. Only non-empty
    groups exist, which is the operator's contract; ORDER BY on a string is
    codepoint-ordered, which is its canonical order; distinct dst counts
    `dense_id`, which bijects with uid.
    """
    if [d["dim"] for d in group_by] != ["rel_type", "time_bucket"] \
            or aggregates != [{"agg": "count"},
                              {"agg": "count_distinct", "of": "dst"}] \
            or rel_types is not None or cursor is not None:
        raise NotImplementedError("only the agg.rel_bucket shape is written")
    t_a, t_b = window["t_a"], window["t_b"]
    with drv.session() as s:
        recs = s.run(
            f"MATCH ()-[r:E]->(b:Entity) WHERE {belief(as_of_tt, 'r')} "
            f"AND r.vt_s >= $ta AND r.vt_s < $tb "
            f"RETURN r.rel_type AS rel, (r.vt_s - $ta) / $stride AS i, "
            f"count(r) AS c, count(DISTINCT b.dense_id) AS d "
            f"ORDER BY rel, i",
            ta=t_a, tb=t_b, stride=stride).data()
    out = [{"rel_type": r["rel"],
            "t_a": t_a + int(r["i"]) * stride,
            "t_b": min(t_a + (int(r["i"]) + 1) * stride, t_b),
            "count": int(r["c"]), "distinct_dst": int(r["d"])}
           for r in recs]
    page = out[:limit]
    return {"rows": page, **_page(len(out), len(page))}


BIG_SCORE = 1e9


def burst_detection(drv, *, target: dict, window: dict, stride: int,
                    method: str = "zscore", params: dict | None = None,
                    as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if method != "zscore" or target.get("kind") != "edge_event_rate":
        raise NotImplementedError(f"{method}/{target.get('kind')}")
    if target.get("rel_type") or target.get("uid"):
        raise NotImplementedError("filtered burst target")
    params = params or {}
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    with drv.session() as s:
        got = {r["i"]: r["c"] for r in s.run(
            f"MATCH ()-[r:E]->() WHERE {belief(as_of_tt, 'r')} "
            f"AND r.vt_s >= $ta AND r.vt_s < $tb "
            f"RETURN (r.vt_s - $ta) / $stride AS i, count(r) AS c",
            ta=t_a, tb=t_b, stride=stride).data()}
    series = [float(got.get(i, 0)) for i in range(n)]
    w, z = params.get("w", 10), params.get("z", 3.0)
    flagged = []
    for b in range(n):
        hist = series[max(0, b - w):b]
        if not hist:
            continue
        x = series[b]
        mean = sum(hist) / len(hist)
        std = (sum((h - mean) ** 2 for h in hist) / len(hist)) ** 0.5
        score = abs(x - mean) / std if std > 0 else (
            0.0 if x == mean else BIG_SCORE)
        score = round(score, 9)
        if score >= z:
            flagged.append({"t_a": t_a + b * stride,
                            "t_b": min(t_a + (b + 1) * stride, t_b),
                            "value": float(x), "score": float(score)})
    page = flagged[:limit]
    return {"rows": page, **_page(len(flagged), len(page)), "n_buckets": n}


# --------------------------------------------------------------------------
# traversal slice — Cypher's home turf, and the reason this baseline exists.
# Round control lives in Python (Neo4j Community has no iterate-to-fixpoint
# primitive without APOC); every per-round set operation is one Cypher
# query, and state crosses as parameters in the bolt body, which has no
# query-text size ceiling.
# --------------------------------------------------------------------------

def snapshot_subgraph(drv, *, seeds: list[str], hops: int, t_valid: int,
                      as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    with drv.session() as s:
        nrows = s.run(
            f"MATCH (n:NodeVersion) WHERE {belief(as_of_tt, 'n')} "
            f"AND n.vt_s <= $t AND $t < n.vt_e "
            f"MATCH (e:Entity {{uid: n.uid}}) "
            f"WITH e.uid AS uid, e.dense_id AS id, n "
            f"ORDER BY n.vt_s DESC, n.vid DESC "
            f"RETURN uid, id, collect(n.label)[0] AS label",
            t=t_valid).data()
        valid = {int(r["id"]): (r["uid"], r["label"]) for r in nrows}
        dist: dict[int, int] = {i: 0 for i, (u, _) in valid.items()
                                if u in set(seeds)}
        frontier = sorted(dist)
        rbel = belief(as_of_tt, "r")
        for h in range(1, hops + 1):
            if not frontier:
                break
            touched = s.run(
                f"MATCH (a:Entity)-[r:E]-(b:Entity) "
                f"WHERE a.dense_id IN $front AND {rbel} "
                f"AND r.vt_s <= $t AND $t < r.vt_e "
                f"RETURN DISTINCT b.dense_id AS id",
                front=frontier, t=t_valid).data()
            new = sorted(int(r["id"]) for r in touched
                         if int(r["id"]) not in dist and int(r["id"]) in valid)
            for i in new:
                dist[i] = h
            frontier = new
        reached = sorted(dist)
        total = s.run(
            f"MATCH (a:Entity)-[r:E]->(b:Entity) "
            f"WHERE a.dense_id IN $d AND b.dense_id IN $d AND {rbel} "
            f"AND r.vt_s <= $t AND $t < r.vt_e RETURN count(r)",
            d=reached, t=t_valid).single()[0]
        erows = s.run(
            f"MATCH (a:Entity)-[r:E]->(b:Entity) "
            f"WHERE a.dense_id IN $d AND b.dense_id IN $d AND {rbel} "
            f"AND r.vt_s <= $t AND $t < r.vt_e "
            f"RETURN r.eid AS eid, r.vid AS vid, a.uid AS src, b.uid AS dst, "
            f"r.rel_type AS rel, r.vt_s AS vs, r.vt_e AS ve "
            f"ORDER BY r.vt_s, r.vid LIMIT {int(limit)}",
            d=reached, t=t_valid).data()
    rows = [{"eid": r["eid"], "vid": r["vid"], "src": r["src"], "dst": r["dst"],
             "rel_type": r["rel"], "vt_s": r["vs"], "vt_e": r["ve"]}
            for r in erows]
    nodes = sorted(({"uid": valid[i][0], "label": valid[i][1], "hop": hh}
                    for i, hh in dist.items()),
                   key=lambda r: (r["hop"], r["uid"]))
    nodes_truncated = len(nodes) > limit
    page = _page(total, len(rows))
    return {"rows": rows, "rows_total": page["rows_total"],
            "nodes": nodes[:limit], "nodes_total": len(nodes),
            "nodes_truncated": nodes_truncated,
            "truncated": page["truncated"] or nodes_truncated}


def neighborhood_evolution(drv, *, uid: str, t1: int, t2: int,
                           stride: int | None = None, as_of_tt: int = OPEN_END,
                           limit: int = 100) -> dict[str, Any]:
    stride = stride or max(1, (t2 - t1) // 20)
    rbel = belief(as_of_tt, "r")
    with drv.session() as s:
        def nbrs(t: int) -> set[str]:
            rows = s.run(
                f"MATCH (e:Entity {{uid: $uid}})-[r:E]-(m:Entity) "
                f"WHERE {rbel} AND r.vt_s <= $t AND $t < r.vt_e "
                f"AND m.uid <> $uid RETURN DISTINCT m.uid AS u",
                uid=uid, t=t).data()
            return {r["u"] for r in rows}

        m1, m2 = nbrs(t1), nbrs(t2)
        series = s.run(
            f"UNWIND range($t1, $t2 - 1, $stride) AS bs "
            f"CALL (bs) {{ "
            f"  MATCH (e:Entity {{uid: $uid}})-[r:E]-() "
            f"  WHERE {rbel} AND r.vt_s <= bs AND r.vt_e > bs "
            f"  RETURN count(r) AS deg }} "
            f"RETURN bs, deg ORDER BY bs",
            uid=uid, t1=t1, t2=t2, stride=stride).data()
    gained, lost = sorted(m2 - m1), sorted(m1 - m2)
    return {
        "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
        "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
        "degree_series": [{"t": int(r["bs"]), "degree": int(r["deg"])}
                          for r in series],
        "stride": stride,
        "truncated": len(gained) > limit or len(lost) > limit,
    }


def temporal_reachability(drv, *, src: str, window: dict,
                          direction: str = "out", delta_max_wait: int | None = None,
                          as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if direction != "out" or delta_max_wait is not None:
        raise NotImplementedError(f"direction={direction} delta={delta_max_wait}")
    t_a, t_b = window["t_a"], window["t_b"]
    rbel = belief(as_of_tt, "r")
    with drv.session() as s:
        sid = int(s.run("MATCH (e:Entity {uid: $u}) RETURN e.dense_id",
                        u=src).single()[0])
        arr: dict[int, int] = {sid: t_a}
        frontier = [{"id": sid, "arr": t_a}]
        while frontier:
            relaxed = s.run(
                f"UNWIND $front AS f "
                f"MATCH (a:Entity {{dense_id: f.id}})-[r:E]->(b:Entity) "
                f"WHERE {rbel} AND r.vt_e > $ta AND r.vt_s < $tb "
                f"WITH b, CASE WHEN f.arr > r.vt_s THEN f.arr ELSE r.vt_s END AS tau, r "
                f"WHERE tau < r.vt_e AND tau < $tb "
                f"RETURN b.dense_id AS id, min(tau) AS arr",
                front=frontier, ta=t_a, tb=t_b).data()
            frontier = []
            for r in relaxed:
                i, a = int(r["id"]), int(r["arr"])
                if i not in arr or a < arr[i]:
                    arr[i] = a
                    frontier.append({"id": i, "arr": a})
        ids = sorted(i for i in arr if i != sid)
        uids = dict(s.run(
            "MATCH (e:Entity) WHERE e.dense_id IN $ids "
            "RETURN e.dense_id AS id, e.uid AS uid", ids=ids).values())
    rows = sorted(({"uid": uids[i], "earliest_arrival": arr[i]} for i in ids),
                  key=lambda r: (r["earliest_arrival"], r["uid"]))
    out = rows[:limit]
    return {"rows": out, **_page(len(rows), len(out))}


def temporal_paths(drv, *, src: str, dst: str, window: dict, k: int = 5,
                   max_hops: int = 4, as_of_tt: int = OPEN_END) -> dict[str, Any]:
    t_a, t_b = window["t_a"], window["t_b"]
    rbel = belief(as_of_tt, "r")
    with drv.session() as s:
        ids = {r["uid"]: int(r["id"]) for r in s.run(
            "MATCH (e:Entity) WHERE e.uid IN $u "
            "RETURN e.uid AS uid, e.dense_id AS id", u=[src, dst]).data()}
        sid, did = ids[src], ids[dst]
        states = [{"node": sid, "arr": t_a, "seen": [sid], "key": "",
                   "edges": []}]
        done = []
        for _ in range(max_hops):
            live = [st for st in states if st["node"] != did]
            if not live:
                break
            expanded = s.run(
                f"UNWIND $states AS st "
                f"MATCH (a:Entity {{dense_id: st.node}})-[r:E]->(b:Entity) "
                f"WHERE {rbel} AND r.vt_e > $ta AND r.vt_s < $tb "
                f"AND NOT b.dense_id IN st.seen "
                f"WITH st, r, a, b, "
                f"CASE WHEN st.arr > r.vt_s THEN st.arr ELSE r.vt_s END AS tau "
                f"WHERE tau < r.vt_e AND tau < $tb "
                f"RETURN st, b.dense_id AS nid, tau, r.eid AS eid, "
                f"a.uid AS asrc, b.uid AS bdst, r.rel_type AS rel, "
                f"r.vt_s AS t", states=live, ta=t_a, tb=t_b).data()
            states = []
            for row in expanded:
                st = row["st"]
                nst = {"node": int(row["nid"]), "arr": int(row["tau"]),
                       "seen": st["seen"] + [int(row["nid"])],
                       "key": st["key"] + f"{int(row['t']):019d}" + row["eid"],
                       "edges": st["edges"] + [{
                           "src": row["asrc"], "dst": row["bdst"],
                           "rel_type": row["rel"], "eid": row["eid"],
                           "t": int(row["t"])}]}
                if nst["node"] == did:
                    done.append(nst)
                else:
                    states.append(nst)
    done.sort(key=lambda p: (p["arr"], len(p["edges"]), p["key"]))
    out = [{"arrival": p["arr"], "hops": len(p["edges"]), "edges": p["edges"]}
           for p in done[:k]]
    return {"rows": out, "rows_total": len(done), "truncated": len(done) > k}


# --------------------------------------------------------------------------
# shared four-query slice — written once in openCypher, reused by Memgraph
# --------------------------------------------------------------------------

def diff_snapshots(drv, *, t1: int, t2: int, as_of_tt: int = OPEN_END,
                   scope: dict | None = None, limit: int = 100) -> dict[str, Any]:
    if scope is not None:
        raise NotImplementedError("scoped diff")
    rbel, nbel = belief(as_of_tt, "r"), belief(as_of_tt, "n")

    with drv.session() as s:
        def edge_state(t: int) -> dict[str, tuple]:
            rows = s.run(
                f"MATCH (a:Entity)-[r:E]->(b:Entity) "
                f"WHERE {rbel} AND r.vt_s <= $t AND $t < r.vt_e "
                f"RETURN r.eid AS eid, a.uid AS src, b.uid AS dst, "
                f"r.rel_type AS rel, r.vid AS vid, r.vt_s AS vs, "
                f"r.props AS props", t=t).data()
            st: dict[str, tuple] = {}
            for r in rows:  # canonical row per eid: max (vt_s, vid)
                cur = st.get(r["eid"])
                if cur is None or (r["vs"], r["vid"]) > (cur[4], cur[3]):
                    st[r["eid"]] = (r["src"], r["dst"], r["rel"], r["vid"],
                                    r["vs"], r["props"])
            return st

        def node_state(t: int) -> dict[str, tuple]:
            rows = s.run(
                f"MATCH (n:NodeVersion) "
                f"WHERE {nbel} AND n.vt_s <= $t AND $t < n.vt_e "
                f"RETURN n.uid AS uid, n.label AS label, n.vid AS vid, "
                f"n.vt_s AS vs, n.props AS props", t=t).data()
            st: dict[str, tuple] = {}
            for r in rows:
                cur = st.get(r["uid"])
                if cur is None or (r["vs"], r["vid"]) > (cur[3], cur[1]):
                    st[r["uid"]] = (r["label"], r["vid"], r["props"], r["vs"])
            return st

        n1, n2 = node_state(t1), node_state(t2)
        e1, e2 = edge_state(t1), edge_state(t2)

    nodes_added = sorted(u for u in n2 if u not in n1)
    nodes_removed = sorted(u for u in n1 if u not in n2)

    def edesc(eid, st):
        src, dst, rel = st[eid][0], st[eid][1], st[eid][2]
        return {"eid": eid, "src": src, "dst": dst, "rel_type": rel}

    edges_added = [edesc(e, e2) for e in sorted(e for e in e2 if e not in e1)]
    edges_removed = [edesc(e, e1) for e in sorted(e for e in e1 if e not in e2)]

    changed: list[dict[str, Any]] = []
    for u in sorted(u for u in n1 if u in n2 and n1[u][1] != n2[u][1]):
        (la, _va, pa, _), (lb, _vb, pb, _) = n1[u], n2[u]
        if json.loads(pa) != json.loads(pb) or la != lb:
            changed.append({"kind": "node", "id": u,
                            "from": {"label": la, "props": json.loads(pa)},
                            "to": {"label": lb, "props": json.loads(pb)}})
    for e in sorted(e for e in e1 if e in e2 and e1[e][3] != e2[e][3]):
        pa, pb = e1[e][5], e2[e][5]
        if json.loads(pa) != json.loads(pb):
            changed.append({"kind": "edge", "id": e,
                            "from": {"props": json.loads(pa)},
                            "to": {"props": json.loads(pb)}})

    lists = {"nodes_added": nodes_added, "nodes_removed": nodes_removed,
             "edges_added": edges_added, "edges_removed": edges_removed,
             "props_changed": changed}
    out: dict[str, Any] = {}
    for k, v in lists.items():
        out[k], out[f"{k}_total"] = v[:limit], len(v)
    out["truncated"] = any(len(v) > limit for v in lists.values())
    return out


def _spec_where(spec: dict, rel: str, src: str, dst: str) -> str:
    parts = []
    if spec.get("rel_type"):
        parts.append(f"{rel}.rel_type = '" + spec["rel_type"].replace("'", "\'") + "'")
    if spec.get("src"):
        parts.append(f"{src}.uid = '" + spec["src"].replace("'", "\'") + "'")
    if spec.get("dst"):
        parts.append(f"{dst}.uid = '" + spec["dst"].replace("'", "\'") + "'")
    return (" AND " + " AND ".join(parts)) if parts else ""


def co_active(drv, *, a_spec: dict, b_spec: dict, allen_relation: dict,
              as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if allen_relation.get("relation") != "overlaps":
        raise NotImplementedError(allen_relation.get("relation"))
    where = (f"{belief(as_of_tt, 'ra')} AND {belief(as_of_tt, 'rb')} "
             f"AND ra.vt_s < rb.vt_s AND rb.vt_s < ra.vt_e "
             f"AND ra.vt_e < rb.vt_e AND ra.vid <> rb.vid"
             f"{_spec_where(a_spec, 'ra', 'sa', 'da')}"
             f"{_spec_where(b_spec, 'rb', 'sb', 'db')}")
    pat = ("MATCH (sa:Entity)-[ra:E]->(da:Entity) "
           "MATCH (sb:Entity)-[rb:E]->(db:Entity) ")
    with drv.session() as s:
        total = s.run(f"{pat} WHERE {where} RETURN count(*)").single()[0]
        rows = s.run(
            f"{pat} WHERE {where} "
            f"RETURN ra.eid AS ae, ra.vid AS av, sa.uid AS asrc, da.uid AS adst, "
            f"ra.rel_type AS ar, ra.vt_s AS avs, ra.vt_e AS ave, "
            f"rb.eid AS be, rb.vid AS bv, sb.uid AS bsrc, db.uid AS bdst, "
            f"rb.rel_type AS br, rb.vt_s AS bvs, rb.vt_e AS bve "
            f"ORDER BY ra.vt_s, ra.vid, rb.vt_s, rb.vid LIMIT {int(limit)}").data()
    out = [{"a": {"eid": r["ae"], "vid": r["av"], "src": r["asrc"],
                  "dst": r["adst"], "rel_type": r["ar"], "vt_s": r["avs"],
                  "vt_e": r["ave"]},
            "b": {"eid": r["be"], "vid": r["bv"], "src": r["bsrc"],
                  "dst": r["bdst"], "rel_type": r["br"], "vt_s": r["bvs"],
                  "vt_e": r["bve"]}} for r in rows]
    return {"rows": out, **_page(total, len(out))}


def resolve_entities(drv, *, query: str, label: str | None = None,
                     as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    nbel = belief(as_of_tt, "n")
    ql = query.lower()
    with drv.session() as s:
        scored = {r["uid"]: int(r["m"]) for r in s.run(
            f"MATCH (n:NodeVersion) WHERE {nbel} "
            f"WITH n, CASE WHEN n.uid = $q THEN 0 "
            f"WHEN toLower(n.uid) CONTAINS $ql THEN 1 "
            f"WHEN n.name IS NOT NULL AND n.name <> '' "
            f"AND toLower(n.name) CONTAINS $ql THEN 2 ELSE 9 END AS sc "
            f"WITH n.uid AS uid, min(sc) AS m WHERE m < 9 "
            f"RETURN uid, m", q=query, ql=ql).data()}
        canon = {}
        if scored:
            for r in s.run(
                f"MATCH (n:NodeVersion) WHERE {nbel} AND n.uid IN $uids "
                f"WITH n ORDER BY n.vt_s DESC, n.vid DESC "
                f"WITH n.uid AS uid, collect(n)[0] AS c "
                f"RETURN uid, c.label AS label, c.props AS props",
                    uids=sorted(scored)).data():
                canon[r["uid"]] = (r["label"], r["props"])
    rows = [{"uid": u, "label": canon[u][0],
             "name": json.loads(canon[u][1]).get("name"), "match": m}
            for u, m in scored.items()
            if label is None or canon[u][0] == label]
    rows.sort(key=lambda r: (r["match"], r["uid"]))
    out = rows[:limit]
    return {"rows": out, **_page(len(rows), len(out))}


def count_temporal_motifs(drv, *, motif: str, delta: int, window: dict,
                          node_filter: list[str] | None = None,
                          as_of_tt: int = OPEN_END) -> dict[str, Any]:
    if motif != "M_triangle_cyclic":
        raise NotImplementedError(motif)
    t_a, t_b = window["t_a"], window["t_b"]
    nf_ev = nf_tri = ""
    params: dict[str, Any] = {"ta": t_a, "tb": t_b, "d": delta}
    if node_filter is not None:
        params["nf"] = sorted(set(node_filter))
        nf_ev = " AND x.uid IN $nf AND y.uid IN $nf"
        nf_tri = " AND x.uid IN $nf AND y.uid IN $nf AND z.uid IN $nf"
    rbel = belief(as_of_tt, "r")
    with drv.session() as s:
        n_events = s.run(
            f"MATCH (x:Entity)-[r:E]->(y:Entity) "
            f"WHERE {rbel} AND r.vt_s >= $ta AND r.vt_s < $tb{nf_ev} "
            f"RETURN count(r)", **params).single()[0]
        # closed triangle pattern; strictness lives in the composite
        # (vt_s, eid) comparisons, span bound inclusive (t3 - t1 <= delta)
        cnt = s.run(
            f"MATCH (x:Entity)-[a:E]->(y:Entity)-[b:E]->(z:Entity)-[c:E]->(x) "
            f"WHERE {belief(as_of_tt, 'a')} AND {belief(as_of_tt, 'b')} "
            f"AND {belief(as_of_tt, 'c')} "
            f"AND a.vt_s >= $ta AND a.vt_s < $tb "
            f"AND b.vt_s >= $ta AND b.vt_s < $tb "
            f"AND c.vt_s >= $ta AND c.vt_s < $tb{nf_tri} "
            f"AND (b.vt_s > a.vt_s OR (b.vt_s = a.vt_s AND b.eid > a.eid)) "
            f"AND (c.vt_s > b.vt_s OR (c.vt_s = b.vt_s AND c.eid > b.eid)) "
            f"AND c.vt_s - a.vt_s <= $d AND b.vt_s - a.vt_s <= $d "
            f"AND x <> y AND y <> z AND z <> x "
            f"RETURN count(*)", **params).single()[0]
    return {"count": int(cnt), "n_events_in_window": int(n_events),
            "truncated": False}


QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "reach.window": temporal_reachability,
    "paths.k": temporal_paths,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
    "agg.rel_bucket": aggregate_events,
    "nbr.evolution": neighborhood_evolution,
    "diff.global": diff_snapshots,
    "coactive.narrow": co_active,
    "resolve.substr": resolve_entities,
    "motif.filtered": count_temporal_motifs,
}
