"""Registry queries expressed as PostgreSQL SQL (evaluation plan §4.1).

Each function answers one registry query and returns the same *logical* payload
the TGMS operator returns, so `eval_harness.canonical_hash` can compare them
directly. That hash is the oracle: it covers row order, field set, and int vs
float, none of which survive a casual reimplementation. Anything that passes it
is `equivalent` in the sense `docs/eval_semantics.md` defines; anything that
cannot be written faithfully is left out of `QUERIES` and reported as
unsupported rather than answered with a weaker query.

Three details are load-bearing and easy to get silently wrong:

* **`COLLATE "C"` on every string ordering.** The operators sort uids, eids,
  and vids as Python strings, i.e. by code point. Under a locale collation
  PostgreSQL orders them differently and the hash diverges — while both
  answers look perfectly sorted.
* **`clamp_tt`.** The belief predicate is `tt_s <= a AND a < tt_e` with
  `a = LEAST(as_of_tt, OPEN_END - 1)`. Without the clamp the default
  `as_of_tt = OPEN_END` matches nothing at all, since `tt_e = OPEN_END`.
* **`rows_total` is counted before `LIMIT`.** So each query runs a count and a
  page. That is two round trips, and it is charged to PostgreSQL honestly —
  the plan (§11.3) separates count from enumeration for both systems because
  they optimize differently.

Current-belief queries must spell the belief filter `tt_e = OPEN_END` to reach
the partial indexes; the general range form is for as-of queries. See
`pg_baseline.INDEXES`.
"""

from __future__ import annotations

import json
from typing import Any

OPEN_END = 2**62


def clamp(as_of_tt: int) -> int:
    return min(as_of_tt, OPEN_END - 1)


def belief(as_of_tt: int, alias: str = "") -> str:
    """SQL for `believed at as_of_tt`, in whichever spelling reaches an index.

    The equality form is not merely an optimization of the range form: it is
    the only one the planner can match against the partial indexes, because
    `T < tt_e` implies `tt_e = OPEN_END` only given that tt_e never exceeds
    OPEN_END — true of the data, unstated in the schema.

    The instant is inlined rather than bound. It is an int under our control,
    and inlining lets the same helper serve queries that use named parameters
    and queries that use positional ones without threading an args list
    through every caller.
    """
    p = f"{alias}." if alias else ""
    if as_of_tt >= OPEN_END:
        return f"{p}tt_e = {OPEN_END}"
    a = clamp(as_of_tt)
    return f"{p}tt_s <= {a} AND {a} < {p}tt_e"


def _page(total: int, returned: int) -> dict[str, Any]:
    """`paginate` with no cursor: totals are pre-limit."""
    return {"rows_total": total, "truncated": total > returned}


def _props(raw: str) -> dict[str, Any]:
    return json.loads(raw)


# --------------------------------------------------------------------------
# O1  entity_history
# --------------------------------------------------------------------------

def entity_history(conn, *, uid: str, as_of_tt: int = OPEN_END,
                   limit: int = 100) -> dict[str, Any]:
    where = f"uid = %s AND {belief(as_of_tt)}"
    args = [uid]
    total = conn.execute(
        f"SELECT count(*) FROM node_versions WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT vid, uid, label, vt_s, vt_e, tt_s, props, source, provenance_ref "
        f"FROM node_versions WHERE {where} "
        f'ORDER BY vt_s, vid COLLATE "C" LIMIT %s', [*args, limit]).fetchall()
    # tt_e is unconditionally OPEN_END in this operator's output: the belief
    # filter guarantees tt_e > as_of, and the operator then censors it
    # (ops_snapshot.py:111). Selecting the column would be wrong for as-of.
    out = [{"vid": r[0], "uid": r[1], "label": r[2], "vt_s": r[3], "vt_e": r[4],
            "tt_s": r[5], "tt_e": OPEN_END, "props": _props(r[6]),
            "source": r[7] or "ingest", "provenance_ref": r[8]} for r in rows]
    return {"rows": out, **_page(total, len(out))}


# --------------------------------------------------------------------------
# O2  snapshot_subgraph
# --------------------------------------------------------------------------

_SNAP_BFS = """
WITH RECURSIVE
nodes_at AS (
    SELECT DISTINCT ON (uid) uid, label,
           (SELECT dense_id FROM entities e WHERE e.uid = nv.uid) AS id
    FROM node_versions nv
    WHERE {nbel} AND vt_s <= %(t)s AND %(t)s < vt_e
    ORDER BY uid, vt_s, vid COLLATE "C"
),
edges_at AS (
    SELECT src_id, dst_id FROM edge_versions
    WHERE {ebel} AND vt_s <= %(t)s AND %(t)s < vt_e
),
adj AS (
    SELECT e.src_id AS a, e.dst_id AS b FROM edges_at e
    UNION ALL
    SELECT e.dst_id, e.src_id FROM edges_at e
),
bfs(id, hop) AS (
    SELECT n.id, 0 FROM nodes_at n WHERE n.uid = ANY(%(seeds)s)
    UNION
    SELECT adj.b, bfs.hop + 1
    FROM bfs JOIN adj ON adj.a = bfs.id
    WHERE bfs.hop < %(hops)s
      AND EXISTS (SELECT 1 FROM nodes_at n WHERE n.id = adj.b)
)
SELECT id, min(hop) AS hop FROM bfs GROUP BY id
"""


def snapshot_subgraph(conn, *, seeds: list[str], hops: int, t_valid: int,
                      as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    nbel = ebel = belief(as_of_tt)
    params = {"t": t_valid, "seeds": list(seeds), "hops": hops}
    conn.execute("DROP TABLE IF EXISTS _bfs")
    conn.execute("CREATE TEMP TABLE _bfs AS "
                 + _SNAP_BFS.format(nbel=nbel, ebel=ebel), params)
    conn.execute("CREATE INDEX ON _bfs (id)")
    conn.execute("ANALYZE _bfs")

    # Induced edges: both endpoints inside the reached set, scan order kept.
    ind = (f"FROM edge_versions ev WHERE {ebel} "
           f"AND ev.vt_s <= %(t)s AND %(t)s < ev.vt_e "
           f"AND EXISTS (SELECT 1 FROM _bfs b WHERE b.id = ev.src_id) "
           f"AND EXISTS (SELECT 1 FROM _bfs b WHERE b.id = ev.dst_id)")
    total = conn.execute(f"SELECT count(*) {ind}", params).fetchone()[0]
    erows = conn.execute(
        f"SELECT ev.eid, ev.vid, ev.src, ev.dst, ev.rel_type, ev.vt_s, ev.vt_e {ind} "
        f'ORDER BY ev.vt_s, ev.vid COLLATE "C" LIMIT %(limit)s',
        {**params, "limit": limit}).fetchall()

    nrows = conn.execute(
        'SELECT e.uid, COALESCE(nv.label, \'\'), b.hop '
        "FROM _bfs b JOIN entities e ON e.dense_id = b.id "
        "LEFT JOIN LATERAL ("
        "  SELECT label FROM node_versions n"
        f"  WHERE n.uid = e.uid AND {nbel} AND n.vt_s <= %(t)s AND %(t)s < n.vt_e"
        '  ORDER BY n.vt_s DESC, n.vid COLLATE "C" DESC LIMIT 1) nv ON true '
        'ORDER BY b.hop, e.uid COLLATE "C"', params).fetchall()

    nodes = [{"uid": r[0], "label": r[1], "hop": r[2]} for r in nrows]
    rows = [{"eid": r[0], "vid": r[1], "src": r[2], "dst": r[3],
             "rel_type": r[4], "vt_s": r[5], "vt_e": r[6]} for r in erows]
    nodes_truncated = len(nodes) > limit
    page = _page(total, len(rows))
    return {"rows": rows, "rows_total": page["rows_total"],
            "nodes": nodes[:limit], "nodes_total": len(nodes),
            "nodes_truncated": nodes_truncated,
            "truncated": page["truncated"] or nodes_truncated}


# --------------------------------------------------------------------------
# O4  graph_metric_timeseries (edge_event_count)
# --------------------------------------------------------------------------

def graph_metric_timeseries(conn, *, metric: str, window: dict, stride: int,
                            as_of_tt: int = OPEN_END,
                            limit: int = 100) -> dict[str, Any]:
    if metric != "edge_event_count":
        raise NotImplementedError(metric)
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    bel = belief(as_of_tt)
    # One event is one edge *version*, attributed to the instant vt_s. Empty
    # buckets are emitted, so the bucket spine is generated and left-joined.
    rows = conn.execute(
        f"""
        WITH b AS (
            SELECT g AS i, %s + g * %s AS ba,
                   LEAST(%s + (g + 1) * %s, %s) AS bb
            FROM generate_series(0, %s - 1) g
        ),
        c AS (
            SELECT (vt_s - %s) / %s AS i, count(*) AS n
            FROM edge_versions
            WHERE {bel} AND vt_s >= %s AND vt_s < %s
            GROUP BY 1
        )
        SELECT b.ba, b.bb, COALESCE(c.n, 0) FROM b LEFT JOIN c ON c.i = b.i
        ORDER BY b.ba LIMIT %s
        """,
        [t_a, stride, t_a, stride, t_b, n, t_a, stride, t_a, t_b, limit],
    ).fetchall()
    out = [{"t_a": r[0], "t_b": r[1], "value": int(r[2])} for r in rows]
    return {"rows": out, "rows_total": n, "truncated": n > len(out), "n_buckets": n}


# --------------------------------------------------------------------------
# O10  neighborhood_evolution
# --------------------------------------------------------------------------

_NBRS = """
SELECT DISTINCT CASE WHEN ev.src_id = %(id)s THEN ev.dst_id ELSE ev.src_id END
FROM edge_versions ev
WHERE {bel} AND ev.vt_s <= %(t)s AND %(t)s < ev.vt_e
  AND (ev.src_id = %(id)s OR ev.dst_id = %(id)s)
"""


def neighborhood_evolution(conn, *, uid: str, t1: int, t2: int,
                           stride: int | None = None, as_of_tt: int = OPEN_END,
                           limit: int = 100) -> dict[str, Any]:
    bel = belief(as_of_tt, "ev")
    stride = stride or max(1, (t2 - t1) // 20)
    dense = conn.execute("SELECT dense_id FROM entities WHERE uid = %s",
                         (uid,)).fetchone()[0]

    def nbrs(t: int) -> set[str]:
        ids = conn.execute(_NBRS.format(bel=bel), {"id": dense, "t": t}).fetchall()
        out = {i[0] for i in ids} - {dense}  # self-loops excluded
        if not out:
            return set()
        return {r[0] for r in conn.execute(
            "SELECT uid FROM entities WHERE dense_id = ANY(%s)",
            (sorted(out),)).fetchall()}

    n1, n2 = nbrs(t1), nbrs(t2)
    gained, lost = sorted(n2 - n1), sorted(n1 - n2)

    # degree at each bucket start = versions active at that instant
    series = conn.execute(
        f"""
        WITH bs AS (SELECT generate_series(%(t1)s, %(t2)s - 1, %(stride)s) AS t),
        inc AS (
            SELECT ev.vt_s, ev.vt_e FROM edge_versions ev
            WHERE {bel} AND ev.vt_e > %(t1)s AND ev.vt_s < %(t2)s
              AND (ev.src_id = %(id)s OR ev.dst_id = %(id)s)
        )
        SELECT bs.t, (SELECT count(*) FROM inc
                      WHERE inc.vt_s <= bs.t AND inc.vt_e > bs.t)
        FROM bs ORDER BY bs.t
        """, {"id": dense, "t1": t1, "t2": t2, "stride": stride}).fetchall()

    return {
        "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
        "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
        "degree_series": [{"t": r[0], "degree": int(r[1])} for r in series],
        "stride": stride,
        "truncated": len(gained) > limit or len(lost) > limit,
    }


# --------------------------------------------------------------------------
# O11  co_active (Allen `overlaps`)
# --------------------------------------------------------------------------

def _spec_sql(spec: dict, alias: str, params: list) -> str:
    parts = []
    if spec.get("rel_type"):
        parts.append(f"{alias}.rel_type = %s")
        params.append(spec["rel_type"])
    for field, col in (("src", "src"), ("dst", "dst")):
        if spec.get(field):
            parts.append(f"{alias}.{col} = %s")
            params.append(spec[field])
    return (" AND " + " AND ".join(parts)) if parts else ""


def co_active(conn, *, a_spec: dict, b_spec: dict, allen_relation: dict,
              as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    rel = allen_relation.get("relation")
    if rel != "overlaps":
        raise NotImplementedError(rel)
    params: list[Any] = []
    a_where, b_where = belief(as_of_tt, "a"), belief(as_of_tt, "b")
    a_f = _spec_sql(a_spec, "a", params)
    b_f = _spec_sql(b_spec, "b", params)
    # Allen `overlaps` on half-open intervals, all three inequalities strict.
    join = (f"FROM edge_versions a JOIN edge_versions b ON "
            f"a.vt_s < b.vt_s AND b.vt_s < a.vt_e AND a.vt_e < b.vt_e "
            f"AND a.vid <> b.vid "
            f"WHERE {a_where}{a_f} AND {b_where}{b_f}")
    total = conn.execute(f"SELECT count(*) {join}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT a.eid, a.vid, a.src, a.dst, a.rel_type, a.vt_s, a.vt_e, "
        f"       b.eid, b.vid, b.src, b.dst, b.rel_type, b.vt_s, b.vt_e {join} "
        f'ORDER BY a.vt_s, a.vid COLLATE "C", b.vt_s, b.vid COLLATE "C" '
        f"LIMIT %s", [*params, limit]).fetchall()
    out = [{"a": {"eid": r[0], "vid": r[1], "src": r[2], "dst": r[3],
                  "rel_type": r[4], "vt_s": r[5], "vt_e": r[6]},
            "b": {"eid": r[7], "vid": r[8], "src": r[9], "dst": r[10],
                  "rel_type": r[11], "vt_s": r[12], "vt_e": r[13]}} for r in rows]
    return {"rows": out, **_page(total, len(out))}


#: Registry id -> (callable, verdict). Only queries written faithfully appear.
#: The rest are absent on purpose: the harness reports a missing entry as
#: unsupported, which is the honest answer, whereas a weakened query would be
#: reported as a fast one.



# --------------------------------------------------------------------------
# O3  diff_snapshots
# --------------------------------------------------------------------------

_STATE = """
SELECT DISTINCT ON (eid) eid, src, dst, rel_type, vid, props
FROM edge_versions WHERE {bel} AND vt_s <= %(t)s AND %(t)s < vt_e
ORDER BY eid, vt_s, vid COLLATE "C"
"""

_NSTATE = """
SELECT DISTINCT ON (uid) uid, label, vid, props
FROM node_versions WHERE {bel} AND vt_s <= %(t)s AND %(t)s < vt_e
ORDER BY uid, vt_s, vid COLLATE "C"
"""


def diff_snapshots(conn, *, t1: int, t2: int, as_of_tt: int = OPEN_END,
                   scope: dict | None = None, limit: int = 100) -> dict[str, Any]:
    if scope is not None:
        raise NotImplementedError("scoped diff")
    bel = belief(as_of_tt)
    # Identity maps take the *last* row in (vt_s, vid) order on duplicates,
    # which DISTINCT ON reverses — so the ORDER BY here is ascending and the
    # disjointness invariant makes the choice moot in practice.
    n1, n2 = (dict((r[0], r[1:]) for r in
                   conn.execute(_NSTATE.format(bel=bel), {"t": t}).fetchall())
              for t in (t1, t2))
    e1, e2 = (dict((r[0], r[1:]) for r in
                   conn.execute(_STATE.format(bel=bel), {"t": t}).fetchall())
              for t in (t1, t2))

    nodes_added = sorted(u for u in n2 if u not in n1)
    nodes_removed = sorted(u for u in n1 if u not in n2)

    def edesc(eid: str, st: dict) -> dict[str, Any]:
        src, dst, rel, _vid, _props = st[eid]
        return {"eid": eid, "src": src, "dst": dst, "rel_type": rel}

    edges_added = [edesc(e, e2) for e in sorted(e for e in e2 if e not in e1)]
    edges_removed = [edesc(e, e1) for e in sorted(e for e in e1 if e not in e2)]

    # props_changed: same identity at both instants, different vid, and then
    # a genuine content difference. Node changes first (by uid), then edge
    # changes (by eid) — two sorted runs appended, not one merged sort.
    changed: list[dict[str, Any]] = []
    for u in sorted(u for u in n1 if u in n2 and n1[u][1] != n2[u][1]):
        (la, _va, pa), (lb, _vb, pb) = n1[u], n2[u]
        if _props(pa) != _props(pb) or la != lb:
            changed.append({"kind": "node", "id": u,
                            "from": {"label": la, "props": _props(pa)},
                            "to": {"label": lb, "props": _props(pb)}})
    for e in sorted(e for e in e1 if e in e2 and e1[e][3] != e2[e][3]):
        pa, pb = e1[e][4], e2[e][4]
        if _props(pa) != _props(pb):
            changed.append({"kind": "edge", "id": e,
                            "from": {"props": _props(pa)},
                            "to": {"props": _props(pb)}})

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

#: Time-respecting reachability, as round-by-round relaxation.
#:
#: The traversal rule is *non-decreasing*, not strictly increasing: arriving at
#: v over an edge [vt_s, vt_e) from arrival `a` gives tau = max(a, vt_s),
#: admissible iff tau < vt_e and tau < t_b. Many edges may therefore be
#: traversed at the same instant.
#:
#: The obvious spelling is one `WITH RECURSIVE ... UNION` over (node, arrival)
#: states. It is correct and it is a trap: a recursive CTE may not aggregate
#: over its own working table, so it cannot discard a state that is dominated
#: by a better arrival at the same node, and it enumerates the whole reachable
#: state space instead. Measured on xzgpu at 200k events, that formulation took
#: **278 seconds** against the engine's 24 ms. Relaxing round by round against
#: a temp table keeps one row per node -- the pruning the recursive form cannot
#: express -- and terminates when a round changes nothing.
_REACH_EDGES = """
CREATE TEMP TABLE _ev ON COMMIT DROP AS
SELECT src_id, dst_id, vt_s, vt_e FROM edge_versions
WHERE {bel} AND vt_e > %(t_a)s AND vt_s < %(t_b)s
"""

_REACH_ROUND = """
WITH upd AS (
    SELECT e.dst_id AS id, min(GREATEST(r.arr, e.vt_s)) AS arr
    FROM _reach r JOIN _ev e ON e.src_id = r.id
    WHERE GREATEST(r.arr, e.vt_s) < e.vt_e
      AND GREATEST(r.arr, e.vt_s) < %(t_b)s
    GROUP BY e.dst_id
)
INSERT INTO _reach (id, arr) SELECT id, arr FROM upd
ON CONFLICT (id) DO UPDATE SET arr = EXCLUDED.arr
WHERE _reach.arr > EXCLUDED.arr
"""


def temporal_reachability(conn, *, src: str, window: dict,
                          direction: str = "out", delta_max_wait: int | None = None,
                          as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if direction != "out" or delta_max_wait is not None:
        raise NotImplementedError(f"direction={direction} delta={delta_max_wait}")
    sid = conn.execute("SELECT dense_id FROM entities WHERE uid = %s",
                       (src,)).fetchone()[0]
    p = {"src": sid, "t_a": window["t_a"], "t_b": window["t_b"]}
    conn.execute("DROP TABLE IF EXISTS _ev")
    conn.execute("DROP TABLE IF EXISTS _reach")
    conn.execute(_REACH_EDGES.format(bel=belief(as_of_tt)).replace(
        " ON COMMIT DROP", ""), p)
    conn.execute("CREATE INDEX ON _ev (src_id)")
    conn.execute("ANALYZE _ev")
    conn.execute("CREATE TEMP TABLE _reach (id bigint PRIMARY KEY, arr bigint)")
    conn.execute("INSERT INTO _reach VALUES (%(src)s, %(t_a)s)", p)
    # Bellman-Ford style: each round relaxes every frontier edge once. Rounds
    # are bounded by the longest shortest path in hops, not by the state space.
    while conn.execute(_REACH_ROUND, p).rowcount:
        pass
    rows = conn.execute(
        "SELECT ent.uid, r.arr FROM _reach r "
        "JOIN entities ent ON ent.dense_id = r.id "
        "WHERE r.id <> %(src)s "
        'ORDER BY r.arr, ent.uid COLLATE "C"', p).fetchall()
    out = [{"uid": r[0], "earliest_arrival": int(r[1])} for r in rows[:limit]]
    return {"rows": out, **_page(len(rows), len(out))}


# --------------------------------------------------------------------------
# O6  temporal_paths
# --------------------------------------------------------------------------

#: Node-simple k-shortest time-respecting paths.
#:
#: Ranking is (arrival, hops, then the path's sequence of (vt_s, eid) pairs
#: compared lexicographically). The sequence is accumulated as one text column
#: of fixed-width chunks — 19 zero-padded digits of vt_s followed by the
#: 24-char eid — because equal `hops` is compared first, so all keys being
#: compared have equal length and concatenation orders exactly as element-wise
#: comparison would. `COLLATE "C"` on it is what makes that match Python.
_PATHS = """
WITH RECURSIVE ev AS (
    SELECT src_id, dst_id, eid, rel_type, src, dst, vt_s, vt_e
    FROM edge_versions
    WHERE {bel} AND vt_e > %(t_a)s AND vt_s < %(t_b)s
),
p(node, arr, hops, seen, sortkey, eids, srcs, dsts, rels, ts) AS (
    SELECT %(src)s::bigint, %(t_a)s::bigint, 0, ARRAY[%(src)s::bigint],
           ''::text, ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[],
           ARRAY[]::text[], ARRAY[]::bigint[]
    UNION ALL
    SELECT e.dst_id, GREATEST(p.arr, e.vt_s), p.hops + 1,
           p.seen || e.dst_id,
           p.sortkey || lpad(e.vt_s::text, 19, '0') || e.eid,
           p.eids || e.eid, p.srcs || e.src, p.dsts || e.dst,
           p.rels || e.rel_type, p.ts || e.vt_s
    FROM p JOIN ev e ON e.src_id = p.node
    WHERE p.hops < %(max_hops)s
      AND p.node <> %(dst)s                      -- dst terminates the path
      AND GREATEST(p.arr, e.vt_s) < e.vt_e
      AND GREATEST(p.arr, e.vt_s) < %(t_b)s
      AND NOT (e.dst_id = ANY(p.seen))           -- node-simple
)
SELECT arr, hops, eids, srcs, dsts, rels, ts
FROM p WHERE node = %(dst)s AND hops > 0
ORDER BY arr, hops, sortkey COLLATE "C"
"""


def temporal_paths(conn, *, src: str, dst: str, window: dict, k: int = 5,
                   max_hops: int = 4, as_of_tt: int = OPEN_END) -> dict[str, Any]:
    ids = dict(conn.execute(
        "SELECT uid, dense_id FROM entities WHERE uid = ANY(%s)",
        ([src, dst],)).fetchall())
    rows = conn.execute(_PATHS.format(bel=belief(as_of_tt)),
                        {"src": ids[src], "dst": ids[dst], "t_a": window["t_a"],
                         "t_b": window["t_b"], "max_hops": max_hops}).fetchall()
    out = [{"arrival": int(r[0]), "hops": int(r[1]),
            "edges": [{"src": s, "dst": d, "rel_type": rt, "eid": e, "t": int(t)}
                      for e, s, d, rt, t in zip(r[2], r[3], r[4], r[5], r[6])]}
           for r in rows[:k]]
    # not `paginate`: truncated is `rows_total > k`, and cursor is always null
    return {"rows": out, "rows_total": len(rows), "truncated": len(rows) > k}


# --------------------------------------------------------------------------
# O7  burst_detection (zscore)
# --------------------------------------------------------------------------

#: Bucket counts plus the trailing-window mean and population stddev.
#:
#: `stddev_pop`, not `stddev_samp`: the reference divides by len(hist).
#: The frame is `w PRECEDING AND 1 PRECEDING` — strictly earlier buckets,
#: excluding the current one, so bucket 0 has no history and is skipped.
_BURST = """
WITH b AS (
    SELECT g AS i, %(t_a)s + g * %(stride)s AS ba,
           LEAST(%(t_a)s + (g + 1) * %(stride)s, %(t_b)s) AS bb
    FROM generate_series(0, %(n)s - 1) g
),
c AS (
    SELECT (vt_s - %(t_a)s) / %(stride)s AS i, count(*) AS n
    FROM edge_versions
    WHERE {bel} AND vt_s >= %(t_a)s AND vt_s < %(t_b)s
    GROUP BY 1
),
s AS (SELECT b.i, b.ba, b.bb, COALESCE(c.n, 0)::float8 AS v
      FROM b LEFT JOIN c ON c.i = b.i)
SELECT i, ba, bb, v,
       avg(v)        OVER (ORDER BY i ROWS BETWEEN %(w)s PRECEDING AND 1 PRECEDING),
       stddev_pop(v) OVER (ORDER BY i ROWS BETWEEN %(w)s PRECEDING AND 1 PRECEDING),
       count(*)      OVER (ORDER BY i ROWS BETWEEN %(w)s PRECEDING AND 1 PRECEDING)
FROM s ORDER BY i
"""

BIG_SCORE = 1e9


def burst_detection(conn, *, target: dict, window: dict, stride: int,
                    method: str = "zscore", params: dict | None = None,
                    as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    if method != "zscore" or target.get("kind") != "edge_event_rate":
        raise NotImplementedError(f"{method}/{target.get('kind')}")
    if target.get("rel_type") or target.get("uid"):
        raise NotImplementedError("filtered burst target")
    params = params or {}
    t_a, t_b = window["t_a"], window["t_b"]
    n = -(-(t_b - t_a) // stride)
    rows = conn.execute(
        _BURST.format(bel=belief(as_of_tt)),
        {"t_a": t_a, "t_b": t_b, "stride": stride, "n": n,
         "w": params.get("w", 10)}).fetchall()

    # The scalar arithmetic stays in Python deliberately. Python rounds
    # half-to-even on the binary double; PostgreSQL's round() is half-away-
    # from-zero on a decimal expansion, and the reference thresholds on the
    # *rounded* score — so rounding server-side would change which rows exist,
    # not merely how they print. At most 2000 buckets reach here; the scan,
    # the bucketing and the windowed aggregation all stayed in SQL.
    z = params.get("z", 3.0)
    flagged = []
    for _i, ba, bb, v, mean, std, hist_n in rows:
        if not hist_n:
            continue
        std = float(std or 0.0)
        score = abs(v - float(mean)) / std if std > 0 else (
            0.0 if v == float(mean) else BIG_SCORE)
        score = round(score, 9)
        if score >= z:
            flagged.append({"t_a": int(ba), "t_b": int(bb),
                            "value": float(v), "score": float(score)})
    page = flagged[:limit]
    return {"rows": page, **_page(len(flagged), len(page)), "n_buckets": n}


# --------------------------------------------------------------------------
# O8  resolve_entities
# --------------------------------------------------------------------------

#: Match every believed node version, score it, and keep the best per uid.
#:
#: There is no valid-time filter at all — belief only. The "canonical"
#: label/name come from the latest version by vt_s over *all* believed
#: versions of that uid, not merely the matching ones, and ties on vt_s keep
#: the earliest in (vt_s, vid) order.
_RESOLVE = """
WITH v AS (
    SELECT uid, label, props, vt_s, vid,
           lower(uid) AS luid, lower(COALESCE(props::jsonb ->> 'name', '')) AS lname
    FROM node_versions WHERE {bel}
),
scored AS (
    SELECT uid, min(CASE WHEN uid = %(q)s THEN 0
                         WHEN strpos(luid,  %(ql)s) > 0 THEN 1
                         WHEN lname <> '' AND strpos(lname, %(ql)s) > 0 THEN 2
                    END) AS match
    FROM v GROUP BY uid
),
latest AS (
    SELECT DISTINCT ON (uid) uid, label, props
    FROM v ORDER BY uid, vt_s DESC, vid COLLATE "C"
)
SELECT s.uid, l.label, l.props, s.match
FROM scored s JOIN latest l ON l.uid = s.uid
WHERE s.match IS NOT NULL {label_f}
ORDER BY s.match, s.uid COLLATE "C"
"""


def resolve_entities(conn, *, query: str, label: str | None = None,
                     as_of_tt: int = OPEN_END, limit: int = 100) -> dict[str, Any]:
    sql = _RESOLVE.format(bel=belief(as_of_tt),
                          label_f="AND l.label = %(label)s" if label else "")
    rows = conn.execute(sql, {"q": query, "ql": query.lower(),
                              "label": label}).fetchall()
    # `name` keeps its JSON type — a number stays a number, absent is null —
    # so it is read from the parsed props, never via ->> which stringifies.
    out = [{"uid": r[0], "label": r[1], "name": _props(r[2]).get("name"),
            "match": int(r[3])} for r in rows[:limit]]
    return {"rows": out, **_page(len(rows), len(out))}


# --------------------------------------------------------------------------
# O9  count_temporal_motifs (M_triangle_cyclic)
# --------------------------------------------------------------------------

#: Three-way self-join over events ordered by (vt_s, eid).
#:
#: The ordering is strict in the *composite* key, not in time alone, so three
#: events sharing a vt_s can still form a motif with eid deciding their roles.
#: The span bound is inclusive: t3 - t1 <= delta.
_MOTIF = """
WITH ev AS (
    SELECT eid, src, dst, vt_s AS t FROM edge_versions
    WHERE {bel} AND vt_s >= %(t_a)s AND vt_s < %(t_b)s {nf}
)
SELECT (SELECT count(*) FROM ev) AS n_events,
       (SELECT count(*)
        FROM ev a JOIN ev b
          ON (b.t, b.eid COLLATE "C") > (a.t, a.eid COLLATE "C")
         AND b.src = a.dst
        JOIN ev c
          ON (c.t, c.eid COLLATE "C") > (b.t, b.eid COLLATE "C")
         AND c.src = b.dst AND c.dst = a.src
        WHERE c.t - a.t <= %(delta)s
          AND a.src <> a.dst AND b.dst <> a.dst AND b.dst <> a.src) AS cnt
"""


def count_temporal_motifs(conn, *, motif: str, delta: int, window: dict,
                          node_filter: list[str] | None = None,
                          as_of_tt: int = OPEN_END) -> dict[str, Any]:
    if motif != "M_triangle_cyclic":
        raise NotImplementedError(motif)
    p = {"t_a": window["t_a"], "t_b": window["t_b"], "delta": delta}
    nf = ""
    if node_filter is not None:
        # both endpoints must be inside the filter, and it is a pre-filter on
        # events, so it shrinks n_events_in_window too
        nf = "AND src = ANY(%(nf)s) AND dst = ANY(%(nf)s)"
        p["nf"] = sorted(set(node_filter))
    r = conn.execute(_MOTIF.format(bel=belief(as_of_tt), nf=nf), p).fetchone()
    return {"count": int(r[1]), "n_events_in_window": int(r[0]),
            "truncated": False}


QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "diff.global": diff_snapshots,
    "reach.window": temporal_reachability,
    "paths.k": temporal_paths,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
    "nbr.evolution": neighborhood_evolution,
    "coactive.narrow": co_active,
    "resolve.substr": resolve_entities,
    "motif.filtered": count_temporal_motifs,
}
