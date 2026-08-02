"""O14 `aggregate_events`: grouped aggregation over edge events (D-044).

An *event* is a believed edge version with `t_a <= vt_s < t_b`. Events group
by up to two dimensions from a closed set — time bucket (same bucket
conventions as `graph_metric_timeseries`), rel_type, endpoint (src or dst, as
uids), endpoint label (the believed node version valid at the event's vt_s;
null when none) — and 1..4 aggregates from a closed set: count, distinct
endpoints, min/max/mean over a closed numeric source (`vt_s`, or
`duration = vt_e - vt_s` with open-ended rows excluded).

Contract highlights (full rationale in DECISIONS.md D-044):
- grouped calls emit only non-empty groups; `group_by: []` emits exactly one
  row (SQL scalar-aggregate semantics);
- canonical ordering: dimension values in `group_by` order, numeric dims in
  numeric order, string dims in code-point order, null labels first;
- `mean` = exact integer sum, then `q, r = divmod(s, n)`;
  `float(q) + r / n` — bit-identical across kernel, fallback, oracle, and
  SQL twins, because float accumulation order never participates;
- numeric-*prop* aggregates are deliberately absent: props are untyped JSON
  and an aggregate over them needs a typing story first (D-044).

This module holds the operator plus the portable vectorized fallback over
adapter columnar scans. The native engine answers through one PyO3 crossing
(`NativeAdapter.aggregate_events_columnar` -> `aggregate.rs`, two-phase
parallel aggregation on fixed-width codes).
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from tgms.core.errors import InvalidArgError, LimitError
from tgms.core.model import OPEN_END
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    WINDOW,
    check_window,
    operator,
    paginate,
    required,
)
from tgms.temporal.guardrails import scan_estimate
from tgms.temporal.ops_series import MAX_BUCKETS

#: Runtime cap on emitted groups. Pagination bounds the *page*, not the
#: aggregation state; this bounds the state, with the narrowing levers named
#: in the payload the repair loop consumes.
MAX_GROUPS = 100_000

DIM_SPEC = {
    "type": "object",
    "properties": {
        "dim": {"type": "string",
                "enum": ["time_bucket", "rel_type", "endpoint", "label"]},
        "role": {"type": ["string", "null"], "enum": ["src", "dst", None],
                 "default": None,
                 "description": "which endpoint, for endpoint/label dims"},
    },
    "required": ["dim"],
    "additionalProperties": False,
}
AGG_SPEC = {
    "type": "object",
    "properties": {
        "agg": {"type": "string",
                "enum": ["count", "count_distinct", "min", "max", "mean"]},
        "of": {"type": ["string", "null"],
               "enum": ["src", "dst", "vt_s", "duration", None],
               "default": None,
               "description": "src|dst for count_distinct; "
                              "vt_s|duration for min/max/mean"},
    },
    "required": ["agg"],
    "additionalProperties": False,
}


def _dim_key(d: dict[str, Any]) -> tuple[str, str | None]:
    return (d["dim"], d.get("role"))


def _agg_field(a: dict[str, Any]) -> str:
    """Deterministic output field name for one aggregate spec."""
    if a["agg"] == "count":
        return "count"
    if a["agg"] == "count_distinct":
        return f"distinct_{a['of']}"
    return f"{a['agg']}_{a['of']}"


def _aggregate_validators(args: dict[str, Any]) -> None:
    check_window(args)
    dims, aggs = args["group_by"], args["aggregates"]

    seen: set[tuple[str, str | None]] = set()
    for d in dims:
        if d["dim"] in ("endpoint", "label"):
            if d.get("role") not in ("src", "dst"):
                raise InvalidArgError(
                    f"dimension {d['dim']!r} requires role 'src' or 'dst'")
        elif d.get("role") is not None:
            raise InvalidArgError(
                f"dimension {d['dim']!r} takes no role")
        if _dim_key(d) in seen:
            raise InvalidArgError(f"duplicate dimension {d}")
        seen.add(_dim_key(d))

    fields: set[str] = set()
    for a in aggs:
        if a["agg"] == "count":
            if a.get("of") is not None:
                raise InvalidArgError("aggregate 'count' takes no 'of'")
        elif a["agg"] == "count_distinct":
            if a.get("of") not in ("src", "dst"):
                raise InvalidArgError(
                    "aggregate 'count_distinct' requires of 'src' or 'dst'")
        else:
            if a.get("of") not in ("vt_s", "duration"):
                raise InvalidArgError(
                    f"aggregate {a['agg']!r} requires of 'vt_s' or 'duration'")
        f = _agg_field(a)
        if f in fields:
            raise InvalidArgError(f"duplicate aggregate {f!r}")
        fields.add(f)

    has_bucket = any(d["dim"] == "time_bucket" for d in dims)
    stride = args.get("stride")
    if has_bucket:
        if stride is None:
            raise InvalidArgError(
                "a time_bucket dimension requires 'stride'")
        t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
        n = -(-(t_b - t_a) // stride)
        if n > MAX_BUCKETS:
            # actionable repair payload, same shape as graph_metric_timeseries
            min_stride = -(-(t_b - t_a) // MAX_BUCKETS)
            raise LimitError(
                f"bucket count {n} exceeds cap {MAX_BUCKETS}; "
                f"use stride >= {min_stride} for this window")
    elif stride is not None:
        raise InvalidArgError("'stride' requires a time_bucket dimension")


def _mean(s: int, n: int) -> float:
    """The one blessed mean: exact integer sum, one float rounding per term.

    `float(q) + r / n` is the same IEEE sequence in the Rust kernel, the SQL
    twins' Python assembly, and the oracle — so a mean over epoch-microsecond
    sums hashes identically everywhere, which plain float accumulation does
    not survive.
    """
    q, r = divmod(int(s), int(n))
    return float(q) + r / n


def _too_many_groups(n: int) -> LimitError:
    return LimitError(
        f"group count {n} exceeds cap {MAX_GROUPS}; narrow the window, add a "
        f"rel_types filter, or use coarser dimensions")


# --------------------------------------------------------------------------- #
# portable fallback: vectorized NumPy over the adapter columnar scans          #
# --------------------------------------------------------------------------- #


def _labels_at(adapter: StorageAdapter, uid_ids: np.ndarray, ts: np.ndarray,
               as_of: int) -> np.ndarray:
    """Label of each endpoint's believed node version valid at `ts[i]`.

    Believed valid intervals of one identity are disjoint (the store
    invariant D-001 relies on), so at most one version matches; `None` where
    none does. Vectorized: sort node intervals by (uid, vt_s), locate the
    last interval starting at or before t with a bounded backward sweep —
    the bound is the max believed-version count of any single node.
    """
    nodes = adapter.nodes_columnar(as_of_tt=as_of)
    nuid, ns, ne = nodes["uid_id"], nodes["vt_s"], nodes["vt_e"]
    nlabel = nodes["label"]
    order = np.lexsort((ns, nuid))
    nuid, ns, ne, nlabel = nuid[order], ns[order], ne[order], nlabel[order]

    out = np.full(len(uid_ids), None, dtype=object)
    if len(nuid) == 0 or len(uid_ids) == 0:
        return out
    start = np.searchsorted(nuid, uid_ids, side="left")
    end = np.searchsorted(nuid, uid_ids, side="right")
    idx = end - 1
    max_versions = int((end - start).max()) if len(uid_ids) else 0
    for _ in range(max_versions):
        safe = np.maximum(idx, 0)
        bad = (idx >= start) & (ns[safe] > ts)
        if not bad.any():
            break
        idx = idx - bad.astype(np.int64)
    safe = np.maximum(idx, 0)
    valid = (idx >= start) & (ns[safe] <= ts) & (ts < ne[safe])
    out[valid] = nlabel[safe][valid]
    return out


def _portable_dim_codes(adapter: StorageAdapter, d: dict[str, Any],
                        args: dict[str, Any], vt_s: np.ndarray,
                        src: np.ndarray, dst: np.ndarray,
                        rel: np.ndarray | None,
                        ) -> tuple[np.ndarray, Callable, Callable]:
    """Per-event integer codes for one dimension, plus `values(codes)` (the
    output columns for unique codes) and `ranks(codes)` (integers whose order
    is the canonical order). Codes are never strings (D-044); -1 means null.
    """
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    if d["dim"] == "time_bucket":
        stride = args["stride"]
        codes = (vt_s - t_a) // stride

        def values(c: np.ndarray) -> dict[str, list]:
            ba = t_a + c * stride
            bb = np.minimum(ba + stride, t_b)
            return {"t_a": [int(v) for v in ba], "t_b": [int(v) for v in bb]}

        return codes, values, lambda c: c
    if d["dim"] == "rel_type":
        uniq, inv = np.unique(np.asarray(rel, dtype=object), return_inverse=True)

        def values(c: np.ndarray) -> dict[str, list]:
            return {"rel_type": [str(uniq[i]) for i in c]}

        # np.unique sorts, and Python string order is code-point order
        return inv.astype(np.int64), values, lambda c: c
    if d["dim"] == "endpoint":
        ids = src if d["role"] == "src" else dst

        def values(c: np.ndarray) -> dict[str, list]:
            return {d["role"]: adapter.uids_for([int(v) for v in c])}

        def ranks(c: np.ndarray) -> np.ndarray:
            # canonical order is uid string order, not dense-id order
            present = np.unique(c)
            uids = adapter.uids_for([int(v) for v in present])
            r = np.empty(len(present), dtype=np.int64)
            r[np.argsort(np.asarray(uids, dtype=object), kind="stable")] = \
                np.arange(len(present))
            return r[np.searchsorted(present, c)]

        return ids.astype(np.int64), values, ranks
    # label
    ids = src if d["role"] == "src" else dst
    labels = _labels_at(adapter, ids, vt_s, args["as_of_tt"])
    non_null = np.fromiter((v is not None for v in labels), dtype=bool,
                           count=len(labels))
    uniq = np.unique(labels[non_null].astype(object)) if non_null.any() \
        else np.empty(0, dtype=object)
    codes = np.full(len(labels), -1, dtype=np.int64)
    if non_null.any():
        codes[non_null] = np.searchsorted(uniq, labels[non_null].astype(object))

    def values(c: np.ndarray) -> dict[str, list]:
        return {f"{d['role']}_label":
                [None if i < 0 else str(uniq[i]) for i in c]}

    # -1 (null) sorts first in integer order — the contract's null-first
    return codes, values, lambda c: c


def _portable(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    dims, aggs = args["group_by"], args["aggregates"]
    need_rel = any(d["dim"] == "rel_type" for d in dims)
    cols = ["src_id", "dst_id", "vt_s", "vt_e"] + (["rel_type"] if need_rel else [])
    e = adapter.edges_columnar(
        as_of_tt=args["as_of_tt"], vt_min=t_a, vt_max=t_b,
        rel_types=args["rel_types"], columns=tuple(cols))
    # the scan filter is interval *overlap*; an event is containment of vt_s
    m = e["vt_s"] >= t_a
    vt_s, vt_e = e["vt_s"][m], e["vt_e"][m]
    src, dst = e["src_id"][m], e["dst_id"][m]
    rel = e["rel_type"][m] if need_rel else None
    n = len(vt_s)

    dim_cols = [_portable_dim_codes(adapter, d, args, vt_s, src, dst, rel)
                for d in dims]

    # group identification on integer codes
    if not dims:
        g = 1
        inv = np.zeros(n, dtype=np.int64)
        uniq_codes: list[np.ndarray] = []
    elif len(dims) == 1:
        u, inv = np.unique(dim_cols[0][0], return_inverse=True)
        g, uniq_codes = len(u), [u]
    else:
        c0, c1 = dim_cols[0][0], dim_cols[1][0]
        stacked = np.stack([c0, c1], axis=1)
        u2, inv = np.unique(stacked, axis=0, return_inverse=True)
        g, uniq_codes = len(u2), [u2[:, 0], u2[:, 1]]
        inv = inv.ravel()
    if g > MAX_GROUPS:
        raise _too_many_groups(g)

    # canonical order over groups, computed on integer ranks
    if g and dims:
        rank_arrays = [dim_cols[i][2](uniq_codes[i]) for i in range(len(dims))]
        order = np.lexsort(tuple(reversed(rank_arrays)))
        uniq_codes = [u[order] for u in uniq_codes]
    else:
        order = np.arange(g)

    inv_pos = np.empty(g, dtype=np.int64)
    inv_pos[order] = np.arange(g)
    if n:
        inv = inv_pos[inv]

    # aggregates
    agg_cols: dict[str, list] = {}
    counts = np.bincount(inv, minlength=g) if n else np.zeros(g, dtype=np.int64)
    for a in aggs:
        field = _agg_field(a)
        if a["agg"] == "count":
            agg_cols[field] = [int(v) for v in counts]
        elif a["agg"] == "count_distinct":
            ids = src if a["of"] == "src" else dst
            if n:
                base = int(adapter.num_entities()) + 1
                uk = np.unique(inv * base + ids)
                dist = np.bincount(uk // base, minlength=g)
            else:
                dist = np.zeros(g, dtype=np.int64)
            agg_cols[field] = [int(v) for v in dist]
        else:
            if a["of"] == "vt_s":
                vals, sub = vt_s, np.ones(n, dtype=bool)
            else:
                vals, sub = vt_e - vt_s, vt_e < OPEN_END
            grp, v = inv[sub], vals[sub]
            nc = np.bincount(grp, minlength=g) if len(grp) else \
                np.zeros(g, dtype=np.int64)
            if a["agg"] == "min":
                out = np.full(g, np.iinfo(np.int64).max, dtype=np.int64)
                if len(grp):
                    np.minimum.at(out, grp, v)
            elif a["agg"] == "max":
                out = np.full(g, np.iinfo(np.int64).min, dtype=np.int64)
                if len(grp):
                    np.maximum.at(out, grp, v)
            else:  # mean: exact integer sums (object dtype = Python ints)
                sums = np.zeros(g, dtype=object)
                if len(grp):
                    np.add.at(sums, grp, v.astype(object))
                agg_cols[field] = [
                    _mean(sums[i], int(nc[i])) if nc[i] else None
                    for i in range(g)]
                continue
            agg_cols[field] = [int(out[i]) if nc[i] else None for i in range(g)]

    # rehydrate dimension values only now, for surviving groups only
    dim_value_cols: dict[str, list] = {}
    for i in range(len(dims)):
        dim_value_cols.update(dim_cols[i][1](uniq_codes[i]))

    rows = [{**{k: col[i] for k, col in dim_value_cols.items()},
             **{k: col[i] for k, col in agg_cols.items()}}
            for i in range(g)]
    return paginate(rows, args["limit"], args["cursor"])


# --------------------------------------------------------------------------- #
# native path: one PyO3 crossing, rows built for the requested page only       #
# --------------------------------------------------------------------------- #


def _native(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    got = adapter.aggregate_events_columnar(
        as_of_tt=args["as_of_tt"],
        t_a=args["window"]["t_a"], t_b=args["window"]["t_b"],
        rel_types=args["rel_types"],
        stride=args.get("stride"),
        group_by=[(d["dim"], d.get("role")) for d in args["group_by"]],
        aggregates=[(a["agg"], a.get("of")) for a in args["aggregates"]],
        max_groups=MAX_GROUPS,
    )
    g = got["rows_total"]
    try:
        offset = int(args["cursor"]) if args["cursor"] else 0
    except ValueError:
        raise InvalidArgError(f"bad cursor: {args['cursor']!r}") from None
    limit = args["limit"]
    lo, hi = offset, min(offset + limit, g)
    hi = max(hi, lo)

    dim_value_cols: dict[str, list] = {}
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    for i, d in enumerate(args["group_by"]):
        codes = got["keys"][i]
        page = codes[lo:hi]
        if d["dim"] == "time_bucket":
            stride = args["stride"]
            dim_value_cols["t_a"] = [int(t_a + c * stride) for c in page]
            dim_value_cols["t_b"] = [int(min(t_a + (c + 1) * stride, t_b))
                                     for c in page]
        elif d["dim"] == "rel_type":
            names = got["rel_names"]
            dim_value_cols["rel_type"] = [names[int(c)] for c in page]
        elif d["dim"] == "endpoint":
            dim_value_cols[d["role"]] = adapter.uids_for([int(c) for c in page])
        else:
            names = got["label_names"]
            dim_value_cols[f"{d['role']}_label"] = [
                None if c < 0 else names[int(c)] for c in page]

    agg_value_cols: dict[str, list] = {}
    for a, col in zip(args["aggregates"], got["aggs"]):
        field = _agg_field(a)
        page = col[lo:hi]
        if a["agg"] == "mean":
            agg_value_cols[field] = [None if v is None else float(v)
                                     for v in page]
        else:
            agg_value_cols[field] = [None if v is None else int(v)
                                     for v in page]

    rows = [{**{k: c[i] for k, c in dim_value_cols.items()},
             **{k: c[i] for k, c in agg_value_cols.items()}}
            for i in range(hi - lo)]
    truncated = lo + len(rows) < g
    return {"rows": rows, "rows_total": g, "truncated": truncated,
            "cursor": str(lo + len(rows)) if truncated else None}


@operator(
    "aggregate_events",
    {
        "group_by": required({
            "type": "array", "items": DIM_SPEC, "maxItems": 2,
            "description": "0-2 dimensions; [] = one global row"}),
        "aggregates": required({
            "type": "array", "items": AGG_SPEC, "minItems": 1, "maxItems": 4}),
        "window": required(WINDOW),
        "stride": {"type": ["integer", "null"], "minimum": 1, "default": None,
                   "description": "bucket width; required with a "
                                  "time_bucket dimension (cap 2000 buckets)"},
        "rel_types": {"type": ["array", "null"], "items": {"type": "string"},
                      "default": None},
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "Grouped aggregation over edge events (believed edge versions with "
    "t_a <= vt_s < t_b). Dimensions: time_bucket (with stride), rel_type, "
    "endpoint (src|dst as uids), label (endpoint's node label valid at the "
    "event; null when none). Aggregates: count, count_distinct(src|dst), "
    "min/max/mean over vt_s or duration (= vt_e - vt_s; open-ended rows "
    "excluded). Non-empty groups only; group_by [] returns one global row. "
    "Rows ordered by dimension values (numeric order / code-point order, "
    "null labels first).",
    cost_fn=scan_estimate,
    validators=[_aggregate_validators],
)
def aggregate_events(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    if hasattr(adapter, "aggregate_events_columnar"):
        return _native(adapter, args)
    return _portable(adapter, args)
