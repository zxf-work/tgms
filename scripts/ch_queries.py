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


#: Registry id -> callable. Slice one; absent entries report as "no SQL
#: written yet", which is not a verdict (see eval_semantics).
QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
}
