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
QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "series.count": graph_metric_timeseries,
    "nbr.evolution": neighborhood_evolution,
    "coactive.narrow": co_active,
}
