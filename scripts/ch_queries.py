"""Registry queries in ClickHouse SQL — first slice (evaluation plan §4.1).

Same oracle discipline as `pg_queries`: each function returns the operator's
logical payload so `eval_harness.canonical_hash` decides equivalence, and a
query that cannot be written faithfully stays out of `QUERIES` rather than
being weakened. Queries are added slice by slice, verified before timed —
this file starts with the shapes the MergeTree layout directly serves:
point history, the event-count series, and the z-score burst on top of it.

One PostgreSQL trap that does not exist here: ClickHouse compares String
bytewise, which *is* the operators' code-point order, so no collation
pinning is needed. One trap that does: HTTP roundtrips are ~1 ms, so each
query keeps to as few statements as the contract's count-plus-page shape
allows (§11.3 still separates count from enumeration).
"""

from __future__ import annotations

import json
from typing import Any

DB = "tgms_eval"
OPEN_END = 2**62


def clamp(as_of_tt: int) -> int:
    return min(as_of_tt, OPEN_END - 1)


def belief(as_of_tt: int, alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    if as_of_tt >= OPEN_END:
        return f"{p}tt_e = {OPEN_END}"
    a = clamp(as_of_tt)
    return f"{p}tt_s <= {a} AND {a} < {p}tt_e"


def _page(total: int, returned: int) -> dict[str, Any]:
    return {"rows_total": total, "truncated": total > returned}


# --------------------------------------------------------------------------
# O1  entity_history
# --------------------------------------------------------------------------

def entity_history(client, *, uid: str, as_of_tt: int = OPEN_END,
                   limit: int = 100) -> dict[str, Any]:
    where = f"uid = %(uid)s AND {belief(as_of_tt)}"
    total = client.query(
        f"SELECT count() FROM {DB}.node_versions WHERE {where}",
        parameters={"uid": uid}).result_rows[0][0]
    rows = client.query(
        f"SELECT vid, uid, label, vt_s, vt_e, tt_s, props, source, provenance_ref "
        f"FROM {DB}.node_versions WHERE {where} "
        f"ORDER BY vt_s, vid LIMIT {int(limit)}",
        parameters={"uid": uid}).result_rows
    # tt_e is unconditionally OPEN_END in this operator's output (see the
    # PostgreSQL twin for the derivation); selecting it would be wrong as-of.
    out = [{"vid": r[0], "uid": r[1], "label": r[2], "vt_s": r[3], "vt_e": r[4],
            "tt_s": r[5], "tt_e": OPEN_END, "props": json.loads(r[6]),
            "source": r[7] or "ingest", "provenance_ref": r[8]} for r in rows]
    return {"rows": out, **_page(total, len(out))}


# --------------------------------------------------------------------------
# O4  graph_metric_timeseries (edge_event_count)
# --------------------------------------------------------------------------

def graph_metric_timeseries(client, *, metric: str, window: dict, stride: int,
                            as_of_tt: int = OPEN_END,
                            limit: int = 100) -> dict[str, Any]:
    if metric != "edge_event_count":
        raise NotImplementedError(metric)
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    got = dict(client.query(
        f"SELECT intDiv(vt_s - {t_a}, {stride}) AS i, count() AS c "
        f"FROM {DB}.edge_versions "
        f"WHERE {belief(as_of_tt)} AND vt_s >= {t_a} AND vt_s < {t_b} "
        f"GROUP BY i").result_rows)
    rows = [{"t_a": t_a + i * stride,
             "t_b": min(t_a + (i + 1) * stride, t_b),
             "value": int(got.get(i, 0))}
            for i in range(min(n, limit))]
    return {"rows": rows, "rows_total": n, "truncated": n > len(rows),
            "n_buckets": n}


# --------------------------------------------------------------------------
# O14 aggregate_events (rel_type x time_bucket; count + distinct dst)
# --------------------------------------------------------------------------

def aggregate_events(client, *, group_by: list, aggregates: list, window: dict,
                     stride: int | None = None, rel_types: list | None = None,
                     as_of_tt: int = OPEN_END, limit: int = 100,
                     cursor: str | None = None) -> dict[str, Any]:
    # Written for the registry's flagship shape; other combinations are "no
    # SQL written yet", which is not a verdict (see run_clickhouse).
    if [d["dim"] for d in group_by] != ["rel_type", "time_bucket"] \
            or aggregates != [{"agg": "count"},
                              {"agg": "count_distinct", "of": "dst"}] \
            or rel_types is not None or cursor is not None:
        raise NotImplementedError("only the agg.rel_bucket shape is written")
    t_a, t_b = window["t_a"], window["t_b"]
    # This is the shape the operator's kernel copies from ClickHouse — a
    # two-phase parallel GROUP BY on fixed-width keys — so this baseline is
    # the one the operator is honestly raced against. Only non-empty groups
    # exist, matching the operator's contract, and bytewise String ORDER BY
    # is the operators' code-point order. Distinct dst counts dense ids,
    # which biject with uids.
    rows = client.query(
        f"SELECT rel_type, intDiv(vt_s - {t_a}, {stride}) AS b, "
        f"count() AS c, uniqExact(dst_id) AS d "
        f"FROM {DB}.edge_versions "
        f"WHERE {belief(as_of_tt)} AND vt_s >= {t_a} AND vt_s < {t_b} "
        f"GROUP BY rel_type, b ORDER BY rel_type, b").result_rows
    out = [{"rel_type": r[0],
            "t_a": t_a + int(r[1]) * stride,
            "t_b": min(t_a + (int(r[1]) + 1) * stride, t_b),
            "count": int(r[2]), "distinct_dst": int(r[3])} for r in rows]
    page = out[:limit]
    return {"rows": page, **_page(len(out), len(page))}


# --------------------------------------------------------------------------
# O7  burst_detection (zscore)
# --------------------------------------------------------------------------

BIG_SCORE = 1e9


def burst_detection(client, *, target: dict, window: dict, stride: int,
                    method: str = "zscore", params: dict | None = None,
                    as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if method != "zscore" or target.get("kind") != "edge_event_rate":
        raise NotImplementedError(f"{method}/{target.get('kind')}")
    if target.get("rel_type") or target.get("uid"):
        raise NotImplementedError("filtered burst target")
    params = params or {}
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    got = dict(client.query(
        f"SELECT intDiv(vt_s - {t_a}, {stride}) AS i, count() AS c "
        f"FROM {DB}.edge_versions "
        f"WHERE {belief(as_of_tt)} AND vt_s >= {t_a} AND vt_s < {t_b} "
        f"GROUP BY i").result_rows)
    series = [float(got.get(i, 0)) for i in range(n)]
    # Scalar tail in Python for the same reason as the PostgreSQL twin: the
    # reference thresholds on the *rounded* score, and Python's half-to-even
    # on the binary double decides which rows exist. <= 2000 buckets.
    w = params.get("w", 10)
    z = params.get("z", 3.0)
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
# working tables
#
# ClickHouse has no session temp tables over the stateless HTTP client, so
# the iterative queries (BFS, relaxation, path search) drive rounds through
# Memory-engine tables in the eval database — the same technique as the
# PostgreSQL temp tables, with round control in Python and every set
# operation in ClickHouse. Recursion is the one shape this engine does not
# natively offer; bounded hops make iteration exact, not approximate.
# --------------------------------------------------------------------------

def _work(client, name: str, schema: str, as_select: str | None = None,
          params: dict | None = None) -> str:
    t = f"{DB}.{name}"
    client.command(f"DROP TABLE IF EXISTS {t}")
    client.command(f"CREATE TABLE {t} ({schema}) ENGINE = Memory")
    if as_select:
        client.command(f"INSERT INTO {t} {as_select}", parameters=params or {})
    return t


# --------------------------------------------------------------------------
# O2  snapshot_subgraph
# --------------------------------------------------------------------------

def snapshot_subgraph(client, *, seeds: list[str], hops: int, t_valid: int,
                      as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    bel = belief(as_of_tt)
    # Valid nodes and instant edges are built entirely server-side: at 10M
    # the valid-id set is ~100k ids, and inlining it in query text blew the
    # default max_query_size. Sets that must cross the boundary (frontier,
    # reached) travel through Memory tables via INSERT — the HTTP body has
    # no query-size ceiling — never through SQL text.
    _work(client, "_validn", "id UInt64",
          f"SELECT e.dense_id FROM {DB}.node_versions nv "
          f"JOIN {DB}.entities e ON e.uid = nv.uid "
          f"WHERE {bel} AND vt_s <= {t_valid} AND {t_valid} < vt_e "
          f"GROUP BY e.dense_id")
    _work(client, "_snape", "src_id UInt64, dst_id UInt64",
          f"SELECT src_id, dst_id FROM {DB}.edge_versions "
          f"WHERE {bel} AND vt_s <= {t_valid} AND {t_valid} < vt_e "
          f"AND src_id IN (SELECT id FROM {DB}._validn) "
          f"AND dst_id IN (SELECT id FROM {DB}._validn)")

    seed_rows = client.query(
        f"SELECT DISTINCT e.dense_id FROM {DB}.entities e "
        f"WHERE e.uid IN %(s)s AND e.dense_id IN (SELECT id FROM {DB}._validn)",
        parameters={"s": list(seeds)}).result_rows
    dist: dict[int, int] = {int(r[0]): 0 for r in seed_rows}
    frontier = sorted(dist)
    _work(client, "_front", "id UInt64")
    for h in range(1, hops + 1):
        if not frontier:
            break
        client.command(f"TRUNCATE TABLE {DB}._front")
        client.insert(f"{DB}._front", [(i,) for i in frontier],
                      column_names=["id"])
        touched = client.query(
            f"SELECT DISTINCT arrayJoin([src_id, dst_id]) FROM {DB}._snape "
            f"WHERE src_id IN (SELECT id FROM {DB}._front) "
            f"   OR dst_id IN (SELECT id FROM {DB}._front)").result_rows
        new = sorted(int(r[0]) for r in touched if int(r[0]) not in dist)
        for i in new:
            dist[i] = h
        frontier = new

    _work(client, "_distn", "id UInt64")
    if dist:
        client.insert(f"{DB}._distn", [(i,) for i in sorted(dist)],
                      column_names=["id"])
    ind = (f"FROM {DB}.edge_versions "
           f"WHERE {bel} AND vt_s <= {t_valid} AND {t_valid} < vt_e "
           f"AND src_id IN (SELECT id FROM {DB}._distn) "
           f"AND dst_id IN (SELECT id FROM {DB}._distn)")
    total = client.query(f"SELECT count() {ind}").result_rows[0][0]
    erows = client.query(
        f"SELECT eid, vid, src, dst, rel_type, vt_s, vt_e {ind} "
        f"ORDER BY vt_s, vid LIMIT {int(limit)}").result_rows
    rows = [{"eid": r[0], "vid": r[1], "src": r[2], "dst": r[3],
             "rel_type": r[4], "vt_s": r[5], "vt_e": r[6]} for r in erows]

    # labels and uids for reached nodes, canonical row per uid
    nrows = client.query(
        f"SELECT e.dense_id, nv.uid, nv.label FROM ("
        f"  SELECT uid, label FROM {DB}.node_versions "
        f"  WHERE {bel} AND vt_s <= {t_valid} AND {t_valid} < vt_e "
        f"  ORDER BY uid, vt_s DESC, vid DESC LIMIT 1 BY uid) nv "
        f"JOIN {DB}.entities e ON e.uid = nv.uid "
        f"WHERE e.dense_id IN (SELECT id FROM {DB}._distn)").result_rows
    meta = {int(r[0]): (r[1], r[2]) for r in nrows}
    nodes = sorted(({"uid": meta[i][0], "label": meta[i][1], "hop": hh}
                    for i, hh in dist.items()),
                   key=lambda r: (r["hop"], r["uid"]))
    nodes_truncated = len(nodes) > limit
    page = _page(total, len(rows))
    return {"rows": rows, "rows_total": page["rows_total"],
            "nodes": nodes[:limit], "nodes_total": len(nodes),
            "nodes_truncated": nodes_truncated,
            "truncated": page["truncated"] or nodes_truncated}


# --------------------------------------------------------------------------
# O3  diff_snapshots
# --------------------------------------------------------------------------

def _edge_state(client, bel: str, t: int) -> dict[str, tuple]:
    rows = client.query(
        f"SELECT eid, src, dst, rel_type, vid, props FROM {DB}.edge_versions "
        f"WHERE {bel} AND vt_s <= {t} AND {t} < vt_e "
        f"ORDER BY eid, vt_s DESC, vid DESC LIMIT 1 BY eid").result_rows
    return {r[0]: r[1:] for r in rows}


def diff_snapshots(client, *, t1: int, t2: int, as_of_tt: int = OPEN_END,
                   scope: dict | None = None, limit: int = 100) -> dict[str, Any]:
    if scope is not None:
        raise NotImplementedError("scoped diff")
    bel = belief(as_of_tt)
    nq = (f"SELECT uid, label, vid, props FROM {DB}.node_versions "
          f"WHERE {bel} AND vt_s <= %(t)s AND %(t)s < vt_e "
          f"ORDER BY uid, vt_s DESC, vid DESC LIMIT 1 BY uid")
    n1 = {r[0]: r[1:] for r in client.query(nq, parameters={"t": t1}).result_rows}
    n2 = {r[0]: r[1:] for r in client.query(nq, parameters={"t": t2}).result_rows}
    e1, e2 = _edge_state(client, bel, t1), _edge_state(client, bel, t2)

    nodes_added = sorted(u for u in n2 if u not in n1)
    nodes_removed = sorted(u for u in n1 if u not in n2)

    def edesc(eid, st):
        src, dst, rel, _v, _p = st[eid]
        return {"eid": eid, "src": src, "dst": dst, "rel_type": rel}

    edges_added = [edesc(e, e2) for e in sorted(e for e in e2 if e not in e1)]
    edges_removed = [edesc(e, e1) for e in sorted(e for e in e1 if e not in e2)]

    changed: list[dict[str, Any]] = []
    for u in sorted(u for u in n1 if u in n2 and n1[u][1] != n2[u][1]):
        (la, _va, pa), (lb, _vb, pb) = n1[u], n2[u]
        if json.loads(pa) != json.loads(pb) or la != lb:
            changed.append({"kind": "node", "id": u,
                            "from": {"label": la, "props": json.loads(pa)},
                            "to": {"label": lb, "props": json.loads(pb)}})
    for e in sorted(e for e in e1 if e in e2 and e1[e][3] != e2[e][3]):
        pa, pb = e1[e][4], e2[e][4]
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


# --------------------------------------------------------------------------
# O5  temporal_reachability
# --------------------------------------------------------------------------

def temporal_reachability(client, *, src: str, window: dict,
                          direction: str = "out", delta_max_wait: int | None = None,
                          as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if direction != "out" or delta_max_wait is not None:
        raise NotImplementedError(f"direction={direction} delta={delta_max_wait}")
    t_a, t_b = window["t_a"], window["t_b"]
    sid = client.query(f"SELECT dense_id FROM {DB}.entities WHERE uid = %(u)s",
                       parameters={"u": src}).result_rows[0][0]
    bel = belief(as_of_tt)
    _work(client, "_rev", "src_id UInt64, dst_id UInt64, vt_s Int64, vt_e Int64",
          f"SELECT src_id, dst_id, vt_s, vt_e FROM {DB}.edge_versions "
          f"WHERE {bel} AND vt_e > {t_a} AND vt_s < {t_b}")
    _work(client, "_reach", "id UInt64, arr Int64",
          f"SELECT {int(sid)}, {t_a}")
    # Bellman-Ford rounds: each rebuilds the label set as min over the old
    # set and one relaxation, terminating when a round changes nothing.
    prev = (1, t_a)
    while True:
        client.command(f"DROP TABLE IF EXISTS {DB}._reach2")
        client.command(
            f"CREATE TABLE {DB}._reach2 ENGINE = Memory AS "
            f"SELECT id, min(arr) AS arr FROM ("
            f"  SELECT id, arr FROM {DB}._reach"
            f"  UNION ALL"
            f"  SELECT e.dst_id AS id, greatest(r.arr, e.vt_s) AS arr"
            f"  FROM {DB}._reach r JOIN {DB}._rev e ON e.src_id = r.id"
            f"  WHERE greatest(r.arr, e.vt_s) < e.vt_e"
            f"    AND greatest(r.arr, e.vt_s) < {t_b}"
            f") GROUP BY id")
        cur = tuple(client.query(
            f"SELECT count(), sum(arr) FROM {DB}._reach2").result_rows[0])
        client.command(f"DROP TABLE IF EXISTS {DB}._reach")
        client.command(f"RENAME TABLE {DB}._reach2 TO {DB}._reach")
        if cur == prev:
            break
        prev = cur
    rows = client.query(
        f"SELECT e.uid, r.arr FROM {DB}._reach r "
        f"JOIN {DB}.entities e ON e.dense_id = r.id "
        f"WHERE r.id != {int(sid)} ORDER BY r.arr, e.uid").result_rows
    out = [{"uid": r[0], "earliest_arrival": int(r[1])} for r in rows[:limit]]
    return {"rows": out, **_page(len(rows), len(out))}


# --------------------------------------------------------------------------
# O6  temporal_paths
# --------------------------------------------------------------------------

def temporal_paths(client, *, src: str, dst: str, window: dict, k: int = 5,
                   max_hops: int = 4, as_of_tt: int = OPEN_END) -> dict[str, Any]:
    t_a, t_b = window["t_a"], window["t_b"]
    ids = dict(client.query(
        f"SELECT uid, dense_id FROM {DB}.entities WHERE uid IN %(u)s",
        parameters={"u": [src, dst]}).result_rows)
    sid, did = int(ids[src]), int(ids[dst])
    bel = belief(as_of_tt)
    _work(client, "_pev",
          "src_id UInt64, dst_id UInt64, eid String, rel_type String, "
          "src String, dst String, vt_s Int64, vt_e Int64",
          f"SELECT src_id, dst_id, eid, rel_type, src, dst, vt_s, vt_e "
          f"FROM {DB}.edge_versions WHERE {bel} AND vt_e > {t_a} AND vt_s < {t_b}")
    state = ("node UInt64, arr Int64, hops UInt8, seen Array(UInt64), "
             "sortkey String, eids Array(String), srcs Array(String), "
             "dsts Array(String), rels Array(String), ts Array(Int64)")
    _work(client, "_pf", state,
          f"SELECT {sid}, {t_a}, 0, [toUInt64({sid})], '', [], [], [], [], []")
    _work(client, "_paths", state)
    for _ in range(max_hops):
        # expand one hop: dst terminates, node-simple via has(seen, ...)
        client.command(
            f"DROP TABLE IF EXISTS {DB}._pf2")
        client.command(
            f"CREATE TABLE {DB}._pf2 ENGINE = Memory AS "
            f"SELECT e.dst_id AS node, greatest(p.arr, e.vt_s) AS arr, "
            f"  toUInt8(p.hops + 1) AS hops, "
            f"  arrayPushBack(p.seen, e.dst_id) AS seen, "
            f"  concat(p.sortkey, leftPad(toString(e.vt_s), 19, '0'), e.eid) AS sortkey, "
            f"  arrayPushBack(p.eids, e.eid) AS eids, "
            f"  arrayPushBack(p.srcs, e.src) AS srcs, "
            f"  arrayPushBack(p.dsts, e.dst) AS dsts, "
            f"  arrayPushBack(p.rels, e.rel_type) AS rels, "
            f"  arrayPushBack(p.ts, e.vt_s) AS ts "
            f"FROM {DB}._pf p JOIN {DB}._pev e ON e.src_id = p.node "
            f"WHERE p.node != {did} "
            f"  AND greatest(p.arr, e.vt_s) < e.vt_e "
            f"  AND greatest(p.arr, e.vt_s) < {t_b} "
            f"  AND NOT has(p.seen, e.dst_id)")
        client.command(f"INSERT INTO {DB}._paths "
                       f"SELECT * FROM {DB}._pf2 WHERE node = {did}")
        client.command(f"DROP TABLE IF EXISTS {DB}._pf")
        client.command(f"RENAME TABLE {DB}._pf2 TO {DB}._pf")
    rows = client.query(
        f"SELECT arr, hops, eids, srcs, dsts, rels, ts FROM {DB}._paths "
        f"ORDER BY arr, hops, sortkey").result_rows
    out = [{"arrival": int(r[0]), "hops": int(r[1]),
            "edges": [{"src": s2, "dst": d2, "rel_type": rt, "eid": e2, "t": int(t)}
                      for e2, s2, d2, rt, t in zip(r[2], r[3], r[4], r[5], r[6])]}
           for r in rows[:k]]
    return {"rows": out, "rows_total": len(rows), "truncated": len(rows) > k}


# --------------------------------------------------------------------------
# O10  neighborhood_evolution
# --------------------------------------------------------------------------

def neighborhood_evolution(client, *, uid: str, t1: int, t2: int,
                           stride: int | None = None, as_of_tt: int = OPEN_END,
                           limit: int = 100) -> dict[str, Any]:
    bel = belief(as_of_tt)
    stride = stride or max(1, (t2 - t1) // 20)
    dense = int(client.query(
        f"SELECT dense_id FROM {DB}.entities WHERE uid = %(u)s",
        parameters={"u": uid}).result_rows[0][0])

    def nbrs(t: int) -> set[str]:
        rows = client.query(
            f"SELECT DISTINCT e.uid FROM {DB}.edge_versions ev "
            f"JOIN {DB}.entities e ON e.dense_id = "
            f"  if(ev.src_id = {dense}, ev.dst_id, ev.src_id) "
            f"WHERE {bel} AND ev.vt_s <= {t} AND {t} < ev.vt_e "
            f"AND (ev.src_id = {dense} OR ev.dst_id = {dense}) "
            f"AND if(ev.src_id = {dense}, ev.dst_id, ev.src_id) != {dense}"
        ).result_rows
        return {r[0] for r in rows}

    m1, m2 = nbrs(t1), nbrs(t2)
    gained, lost = sorted(m2 - m1), sorted(m1 - m2)
    # cross join of ~20 bucket starts against the incident versions, not a
    # correlated scalar subquery: that construct silently yielded NULL at 1M
    # while passing at smaller scales. Buckets with no active version drop
    # out of the group-by and are refilled as zero.
    got = dict(client.query(
        f"SELECT bs, countIf(ev.vt_s <= bs AND ev.vt_e > bs) AS deg "
        f"FROM (SELECT arrayJoin(range({t1}, {t2}, {stride})) AS bs) b "
        f"CROSS JOIN (SELECT vt_s, vt_e FROM {DB}.edge_versions ev "
        f"  WHERE {bel} AND ev.vt_e > {t1} AND ev.vt_s < {t2} "
        f"  AND (ev.src_id = {dense} OR ev.dst_id = {dense})) ev "
        f"GROUP BY bs").result_rows)
    series = [(bs, int(got.get(bs, 0))) for bs in range(t1, t2, stride)]
    return {
        "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
        "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
        "degree_series": [{"t": int(r[0]), "degree": int(r[1])} for r in series],
        "stride": stride,
        "truncated": len(gained) > limit or len(lost) > limit,
    }


# --------------------------------------------------------------------------
# O11  co_active (Allen `overlaps`)
# --------------------------------------------------------------------------

def _spec_where(spec: dict, alias: str) -> str:
    parts = []
    for field in ("rel_type", "src", "dst"):
        if spec.get(field):
            v = spec[field].replace("'", "\\'")
            parts.append(f"{alias}.{field} = '{v}'")
    return (" AND " + " AND ".join(parts)) if parts else ""


def co_active(client, *, a_spec: dict, b_spec: dict, allen_relation: dict,
              as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if allen_relation.get("relation") != "overlaps":
        raise NotImplementedError(allen_relation.get("relation"))
    a_bel, b_bel = belief(as_of_tt, "a"), belief(as_of_tt, "b")
    # strict Allen overlaps; the time bounds live in WHERE because ClickHouse
    # joins want an equality core, and the narrow specs keep the cross small
    join = (f"FROM {DB}.edge_versions a CROSS JOIN {DB}.edge_versions b "
            f"WHERE a.vt_s < b.vt_s AND b.vt_s < a.vt_e AND a.vt_e < b.vt_e "
            f"AND a.vid != b.vid AND {a_bel}{_spec_where(a_spec, 'a')} "
            f"AND {b_bel}{_spec_where(b_spec, 'b')}")
    total = client.query(f"SELECT count() {join}").result_rows[0][0]
    rows = client.query(
        f"SELECT a.eid, a.vid, a.src, a.dst, a.rel_type, a.vt_s, a.vt_e, "
        f"b.eid, b.vid, b.src, b.dst, b.rel_type, b.vt_s, b.vt_e {join} "
        f"ORDER BY a.vt_s, a.vid, b.vt_s, b.vid LIMIT {int(limit)}").result_rows
    out = [{"a": {"eid": r[0], "vid": r[1], "src": r[2], "dst": r[3],
                  "rel_type": r[4], "vt_s": r[5], "vt_e": r[6]},
            "b": {"eid": r[7], "vid": r[8], "src": r[9], "dst": r[10],
                  "rel_type": r[11], "vt_s": r[12], "vt_e": r[13]}} for r in rows]
    return {"rows": out, **_page(total, len(out))}


# --------------------------------------------------------------------------
# O8  resolve_entities
# --------------------------------------------------------------------------

def resolve_entities(client, *, query: str, label: str | None = None,
                     as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    bel = belief(as_of_tt)
    q = query.replace("'", "\\'")
    ql = query.lower().replace("'", "\\'")
    # D-031: only JSON string names participate; JSONExtractString returns ''
    # for absent, null, and non-string values, which is exactly that rule
    label_f = ""
    if label:
        lv = label.replace("'", "\\'")
        label_f = f"AND l.label = '{lv}'"
    rows = client.query(f"""
        WITH v AS (
            SELECT uid, label, props, vt_s, vid,
                   lowerUTF8(uid) AS luid,
                   lowerUTF8(JSONExtractString(props, 'name')) AS lname
            FROM {DB}.node_versions WHERE {bel}
        ),
        scored AS (
            SELECT uid, min(multiIf(uid = '{q}', 0,
                                    position(luid, '{ql}') > 0, 1,
                                    lname != '' AND position(lname, '{ql}') > 0, 2,
                                    9)) AS m
            FROM v GROUP BY uid HAVING m < 9
        ),
        latest AS (
            SELECT uid, label, props FROM v
            ORDER BY uid, vt_s DESC, vid DESC LIMIT 1 BY uid
        )
        SELECT s.uid, l.label, l.props, s.m
        FROM scored s JOIN latest l ON l.uid = s.uid
        WHERE 1 {label_f}
        ORDER BY s.m, s.uid""").result_rows
    out = [{"uid": r[0], "label": r[1], "name": json.loads(r[2]).get("name"),
            "match": int(r[3])} for r in rows[:limit]]
    return {"rows": out, **_page(len(rows), len(out))}


# --------------------------------------------------------------------------
# O9  count_temporal_motifs (M_triangle_cyclic)
# --------------------------------------------------------------------------

def count_temporal_motifs(client, *, motif: str, delta: int, window: dict,
                          node_filter: list[str] | None = None,
                          as_of_tt: int = OPEN_END) -> dict[str, Any]:
    if motif != "M_triangle_cyclic":
        raise NotImplementedError(motif)
    t_a, t_b = window["t_a"], window["t_b"]
    nf = ""
    params: dict[str, Any] = {}
    if node_filter is not None:
        nf = "AND src IN %(nf)s AND dst IN %(nf)s"
        params["nf"] = sorted(set(node_filter))
    _work(client, "_mev", "eid String, src String, dst String, t Int64",
          f"SELECT eid, src, dst, vt_s FROM {DB}.edge_versions "
          f"WHERE {belief(as_of_tt)} AND vt_s >= {t_a} AND vt_s < {t_b} {nf}",
          params)
    n_events = client.query(f"SELECT count() FROM {DB}._mev").result_rows[0][0]
    cnt = client.query(f"""
        SELECT count() FROM {DB}._mev a
        JOIN {DB}._mev b ON b.src = a.dst
        JOIN {DB}._mev c ON c.src = b.dst AND c.dst = a.src
        WHERE (b.t, b.eid) > (a.t, a.eid)
          AND (c.t, c.eid) > (b.t, b.eid)
          AND b.t <= a.t + {int(delta)} AND c.t <= a.t + {int(delta)}
          AND a.src != a.dst AND b.dst != a.dst AND b.dst != a.src
        """).result_rows[0][0]
    return {"count": int(cnt), "n_events_in_window": int(n_events),
            "truncated": False}


#: Registry id -> callable. Full registry attempted; anything that cannot be
#: expressed faithfully would be removed and recorded, not weakened.
QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "diff.global": diff_snapshots,
    "reach.window": temporal_reachability,
    "paths.k": temporal_paths,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
    "agg.rel_bucket": aggregate_events,
    "nbr.evolution": neighborhood_evolution,
    "coactive.narrow": co_active,
    "resolve.substr": resolve_entities,
    "motif.filtered": count_temporal_motifs,
}
