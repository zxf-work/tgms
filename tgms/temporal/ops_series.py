"""Series-family operators: O8 graph_metric_timeseries, O9 burst_detection,
O11 co_active.

Bucket semantics: `window` [t_a, t_b) is split into buckets
[t_a + i*stride, min(t_a + (i+1)*stride, t_b)); instant metrics (node_count,
active_edge_count, mean_out_degree) are sampled at the bucket *start*; event
metrics (edge_event_count, new_node_rate, reciprocity) aggregate events with
vt_s inside the bucket. An "event" is an edge version; a node's first
appearance is the minimum vt_s over its believed versions.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import CostError, InvalidArgError, LimitError
from tgms.core.model import OPEN_END
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    UID,
    WINDOW,
    check_window,
    operator,
    paginate,
    required,
)
from tgms.temporal.guardrails import scan_estimate

MAX_BUCKETS = 2_000
METRICS = ["node_count", "edge_event_count", "active_edge_count",
           "mean_out_degree", "new_node_rate", "reciprocity"]


def _buckets(args: dict[str, Any]) -> np.ndarray:
    t_a, t_b, stride = args["window"]["t_a"], args["window"]["t_b"], args["stride"]
    n = -(-(t_b - t_a) // stride)
    if n > MAX_BUCKETS:
        raise LimitError(f"bucket count {n} exceeds cap {MAX_BUCKETS}; increase stride")
    return np.arange(t_a, t_b, stride, dtype=np.int64)


def _active_at(starts_sorted: np.ndarray, ends_sorted: np.ndarray,
               at: np.ndarray) -> np.ndarray:
    """#intervals with vt_s <= at < vt_e, vectorized over `at`."""
    return (np.searchsorted(starts_sorted, at, side="right")
            - np.searchsorted(ends_sorted, at, side="right"))


def _series_values(adapter: StorageAdapter, metric: str, args: dict[str, Any],
                   bucket_starts: np.ndarray) -> np.ndarray:
    t_a, t_b, stride = args["window"]["t_a"], args["window"]["t_b"], args["stride"]
    as_of = args["as_of_tt"]
    bucket_ends = np.minimum(bucket_starts + stride, t_b)

    if metric in ("node_count", "mean_out_degree", "new_node_rate"):
        nodes = adapter.nodes_columnar(as_of_tt=as_of, vt_max=t_b)
    if metric in ("edge_event_count", "active_edge_count", "mean_out_degree",
                  "reciprocity"):
        ints = ("src_id", "dst_id", "vt_s", "vt_e")
        edges = adapter.edges_columnar(as_of_tt=as_of, vt_min=t_a, vt_max=t_b,
                                       columns=ints) \
            if metric != "mean_out_degree" else \
            adapter.edges_columnar(as_of_tt=as_of, vt_max=t_b, columns=ints)

    if metric == "node_count":
        return _active_at(np.sort(nodes["vt_s"]), np.sort(nodes["vt_e"]),
                          bucket_starts).astype(np.int64)
    if metric == "edge_event_count":
        m = (edges["vt_s"] >= t_a) & (edges["vt_s"] < t_b)
        idx = (edges["vt_s"][m] - t_a) // stride
        return np.bincount(idx, minlength=len(bucket_starts)).astype(np.int64)
    if metric == "active_edge_count":
        return _active_at(np.sort(edges["vt_s"]), np.sort(edges["vt_e"]),
                          bucket_starts).astype(np.int64)
    if metric == "mean_out_degree":
        act = _active_at(np.sort(edges["vt_s"]), np.sort(edges["vt_e"]), bucket_starts)
        nod = _active_at(np.sort(nodes["vt_s"]), np.sort(nodes["vt_e"]), bucket_starts)
        return np.where(nod > 0, act / np.maximum(nod, 1), 0.0)
    if metric == "new_node_rate":
        # first appearance = min vt_s over an identity's believed versions
        ids, first = nodes["uid_id"], nodes["vt_s"]
        order = np.lexsort((first, ids))
        uniq_mask = np.ones(len(ids), dtype=bool)
        uniq_mask[1:] = ids[order][1:] != ids[order][:-1]
        births = first[order][uniq_mask]
        m = (births >= t_a) & (births < t_b)
        idx = (births[m] - t_a) // stride
        return np.bincount(idx, minlength=len(bucket_starts)).astype(np.int64)
    if metric == "reciprocity":
        m = (edges["vt_s"] >= t_a) & (edges["vt_s"] < t_b)
        src, dst, t = edges["src_id"][m], edges["dst_id"][m], edges["vt_s"][m]
        bidx = (t - t_a) // stride
        nv = adapter.num_entities() + 1
        out = np.zeros(len(bucket_starts), dtype=np.float64)
        for b in range(len(bucket_starts)):  # loop over <=2000 buckets, not edges
            bm = bidx == b
            pairs = np.unique(src[bm] * nv + dst[bm])
            if pairs.size == 0:
                continue
            swapped = np.unique(dst[bm] * nv + src[bm])
            out[b] = float(np.isin(pairs, swapped).sum() / pairs.size)
        return out
    raise InvalidArgError(f"unknown metric {metric}")


@operator(
    "graph_metric_timeseries",
    {
        "metric": required({"type": "string", "enum": METRICS}),
        "window": required(WINDOW),
        "stride": required({"type": "integer", "minimum": 1}),
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Per-bucket metric series over `window` (bucket cap 2000). Instant metrics "
    "sample the bucket start; event metrics aggregate vt_s within the bucket.",
    cost_fn=scan_estimate,
    validators=[check_window],
    output_fields=("rows", "rows_total", "truncated", "cursor", "n_buckets"),
)
def graph_metric_timeseries(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    bucket_starts = _buckets(args)
    t_b, stride = args["window"]["t_b"], args["stride"]
    vals = _series_values(adapter, args["metric"], args, bucket_starts)
    rows = [{"t_a": int(bs), "t_b": int(min(bs + stride, t_b)),
             "value": (float(v) if isinstance(v, (float, np.floating)) else int(v))}
            for bs, v in zip(bucket_starts, vals)]
    out = paginate(rows, args["limit"], args["cursor"])
    out["n_buckets"] = len(rows)
    return out


# --------------------------------------------------------------------------- #
# O9 burst_detection                                                           #
# --------------------------------------------------------------------------- #

TARGET = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["edge_event_rate", "node_activity"]},
        "rel_type": {"type": ["string", "null"], "default": None},
        "uid": {"type": ["string", "null"], "default": None},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
PARAMS = {
    "type": "object",
    "properties": {
        "w": {"type": "integer", "minimum": 2, "maximum": 500, "default": 10,
              "description": "trailing window length in buckets"},
        "z": {"type": "number", "minimum": 0.1, "default": 3.0},
        "r": {"type": "number", "minimum": 1.0, "default": 3.0},
    },
    "additionalProperties": False,
    "default": {},
}
BIG_SCORE = 1e9  # stands in for "infinite" score; keeps JSON finite


def _burst_validators(args: dict[str, Any]) -> None:
    check_window(args)
    if args["target"]["kind"] == "node_activity" and not args["target"].get("uid"):
        raise InvalidArgError("target.kind='node_activity' requires target.uid")


@operator(
    "burst_detection",
    {
        "target": required(TARGET),
        "window": required(WINDOW),
        "stride": required({"type": "integer", "minimum": 1}),
        "method": {"type": "string", "enum": ["zscore", "ratio"], "default": "zscore"},
        "params": PARAMS,
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Flag bursty buckets in an event-rate series. zscore: |x - trailing_mean| "
    ">= z * trailing_std (previous w buckets; std=0 -> flagged iff x != mean). "
    "ratio: x / trailing_median >= r (median=0 -> flagged iff x > 0).",
    cost_fn=scan_estimate,
    validators=[_burst_validators],
    output_fields=("rows", "rows_total", "truncated", "cursor", "n_buckets"),
)
def burst_detection(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    bucket_starts = _buckets(args)
    t_a, t_b, stride = args["window"]["t_a"], args["window"]["t_b"], args["stride"]
    tgt = args["target"]

    rel_types = [tgt["rel_type"]] if tgt.get("rel_type") else None
    e = adapter.edges_columnar(as_of_tt=args["as_of_tt"], vt_min=t_a, vt_max=t_b,
                               rel_types=rel_types,
                               columns=("src_id", "dst_id", "vt_s", "vt_e"))
    m = (e["vt_s"] >= t_a) & (e["vt_s"] < t_b)
    if tgt["kind"] == "node_activity":
        uid_id = int(adapter.dense_ids([tgt["uid"]])[0])
        m &= (e["src_id"] == uid_id) | (e["dst_id"] == uid_id)
    idx = (e["vt_s"][m] - t_a) // stride
    series = np.bincount(idx, minlength=len(bucket_starts)).astype(np.float64)

    w = args["params"].get("w", 10)
    flagged_rows = []
    for b in range(len(series)):  # loop over <=2000 buckets
        hist = series[max(0, b - w): b]
        x = float(series[b])
        if hist.size == 0:
            continue
        if args["method"] == "zscore":
            mean, std = float(hist.mean()), float(hist.std())
            if std > 0:
                score = abs(x - mean) / std
            else:
                score = 0.0 if x == mean else BIG_SCORE
            # quantize before thresholding so the flag decision is stable
            # across summation orders (engine vs oracle agree bit-for-bit)
            score = round(score, 9)
            flag = score >= args["params"].get("z", 3.0)
        else:
            med = float(np.median(hist))
            if med > 0:
                score = x / med
            else:
                score = BIG_SCORE if x > 0 else 0.0
            score = round(score, 9)
            flag = score >= args["params"].get("r", 3.0)
        if flag:
            flagged_rows.append({"t_a": int(bucket_starts[b]),
                                 "t_b": int(min(bucket_starts[b] + stride, t_b)),
                                 "value": x, "score": float(score)})
    out = paginate(flagged_rows, args["limit"], args["cursor"])
    out["n_buckets"] = len(series)
    return out


# --------------------------------------------------------------------------- #
# O11 co_active                                                                #
# --------------------------------------------------------------------------- #

EDGE_SPEC = {
    "type": "object",
    "properties": {
        "src": {"type": ["string", "null"], "default": None},
        "dst": {"type": ["string", "null"], "default": None},
        "rel_type": {"type": ["string", "null"], "default": None},
    },
    "additionalProperties": False,
    "description": "edge-version selector; null fields match anything",
}
RELATION = {
    "type": "object",
    "properties": {
        "relation": {"type": "string",
                     "enum": ["overlaps", "during", "before", "meets"]},
        "gap": {"type": ["integer", "null"], "minimum": 1, "default": None,
                "description": "required for 'before': 0 < b.vt_s - a.vt_e <= gap"},
    },
    "required": ["relation"],
    "additionalProperties": False,
}
PAIR_CAP = 50_000


def _co_active_validators(args: dict[str, Any]) -> None:
    rel = args["allen_relation"]
    if rel["relation"] == "before" and not rel.get("gap"):
        raise InvalidArgError("allen_relation 'before' requires gap")


def _select(adapter: StorageAdapter, spec: dict[str, Any], as_of: int):
    rel_types = [spec["rel_type"]] if spec.get("rel_type") else None
    e = adapter.edges_columnar(as_of_tt=as_of, rel_types=rel_types)
    m = np.ones(len(e["src_id"]), dtype=bool)
    if spec.get("src"):
        m &= e["src_id"] == int(adapter.dense_ids([spec["src"]])[0])
    if spec.get("dst"):
        m &= e["dst_id"] == int(adapter.dense_ids([spec["dst"]])[0])
    return {k: v[m] for k, v in e.items()}


@operator(
    "co_active",
    {
        "a_spec": required(EDGE_SPEC),
        "b_spec": required(EDGE_SPEC),
        "allen_relation": required(RELATION),
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Interval join between two edge-version sets under an Allen relation "
    "(strict Allen semantics on half-open valid intervals): "
    "overlaps: a.s < b.s < a.e < b.e; during: b.s < a.s AND a.e < b.e; "
    "meets: a.e = b.s; before: 0 < b.s - a.e <= gap. Pairs with a.vid = b.vid "
    "are excluded.",
    cost_fn=scan_estimate,
    validators=[_co_active_validators],
)
def co_active(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    as_of = args["as_of_tt"]
    a = _select(adapter, args["a_spec"], as_of)
    b = _select(adapter, args["b_spec"], as_of)
    rel = args["allen_relation"]["relation"]
    gap = args["allen_relation"].get("gap")

    # b sorted by (vt_s, vid) — columnar scans already return this order
    b_starts = b["vt_s"]
    a_s, a_e = a["vt_s"], a["vt_e"]
    if rel == "meets":
        lo = np.searchsorted(b_starts, a_e, side="left")
        hi = np.searchsorted(b_starts, a_e, side="right")
    elif rel == "before":
        lo = np.searchsorted(b_starts, a_e, side="right")
        hi = np.searchsorted(b_starts, a_e + gap, side="right")
    elif rel == "overlaps":
        lo = np.searchsorted(b_starts, a_s, side="right")
        hi = np.searchsorted(b_starts, a_e, side="left")
    else:  # during: b.s < a.s
        lo = np.zeros(len(a_s), dtype=np.int64)
        hi = np.searchsorted(b_starts, a_s, side="left")

    if int((hi - lo).clip(min=0).sum()) > PAIR_CAP:
        raise CostError("co_active candidate pairs exceed cap",
                        estimate={"expansions_est": int((hi - lo).clip(min=0).sum())},
                        suggestions=["narrow a_spec/b_spec (src, dst, rel_type)"])

    def edge_desc(cols, i: int) -> dict[str, Any]:
        s, d = adapter.uids_for([int(cols["src_id"][i]), int(cols["dst_id"][i])])
        return {"eid": cols["eid"][i], "vid": cols["vid"][i], "src": s, "dst": d,
                "rel_type": cols["rel_type"][i],
                "vt_s": int(cols["vt_s"][i]), "vt_e": int(cols["vt_e"][i])}

    pairs = []
    for i in range(len(a_s)):  # loop over selected a-rows (spec-narrowed)
        for j in range(int(lo[i]), int(hi[i])):
            if rel == "overlaps" and not (b["vt_e"][j] > a_e[i]):
                continue
            if rel == "during" and not (b["vt_e"][j] > a_e[i]):
                continue
            if a["vid"][i] == b["vid"][j]:
                continue
            pairs.append((i, j))
    rows = [{"a": edge_desc(a, i), "b": edge_desc(b, j)} for i, j in pairs]
    rows.sort(key=lambda r: (r["a"]["vt_s"], r["a"]["vid"],
                             r["b"]["vt_s"], r["b"]["vid"]))
    return paginate(rows, args["limit"], args["cursor"])
