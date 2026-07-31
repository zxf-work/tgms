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


QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
}
