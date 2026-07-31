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


QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "reach.window": temporal_reachability,
    "paths.k": temporal_paths,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
    "nbr.evolution": neighborhood_evolution,
}
