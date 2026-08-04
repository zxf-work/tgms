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
- numeric-*prop* aggregates arrived with D-052 and are typed per call, not
  per store: `of: "prop"` with a `prop` key, plus `prop_filter`. Their
  arithmetic is D-051's rather than the `_mean` above — `mean_duration` is
  always a float because its source is a typed column, while a property
  holds whatever JSON held, so an integer property gives an exact integer.
  The two live in one row on purpose and the difference is the source, not
  an inconsistency;
- *sequence* aggregates arrived with D-056 and are the first ones that need
  a group's events in order rather than folded into an accumulator:
  `max_gap`, `max_in_window` (with a `span`) and `max_session_span` (with a
  `gap`). Their absent value follows one rule — a count is 0 over an empty
  population, a duration between events that do not exist is null.

This module holds the operator plus the portable vectorized fallback over
adapter columnar scans. The native engine answers through one PyO3 crossing
(`NativeAdapter.aggregate_events_columnar` -> `aggregate.rs`, two-phase
parallel aggregation on fixed-width codes).
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from tgms.core.errors import InvalidArgError, LimitError, NotFoundError
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
from tgms.temporal.ops_compute import _mean as _blessed_mean
from tgms.temporal.ops_series import MAX_BUCKETS
from tgms.temporal.props import (
    PROP_CMPS,
    SKIP,
    matches,
    numeric_value,
    parse_props,
    prop_keys,
)

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
#: D-056. Three walks over a group's event times, in valid-time order. They
#: take no `of`: the only ordering this operator has is `vt_s`, so naming it
#: would be a single legal value and one more thing to get wrong.
SEQ_AGGS = ("max_gap", "max_in_window", "max_session_span")
AGG_SPEC = {
    "type": "object",
    "properties": {
        "agg": {"type": "string",
                "enum": ["count", "count_distinct", "min", "max", "mean",
                         *SEQ_AGGS]},
        "of": {"type": ["string", "null"],
               "enum": ["src", "dst", "vt_s", "duration", "prop", None],
               "default": None,
               "description": "src|dst for count_distinct; "
                              "vt_s|duration|prop for min/max/mean; the "
                              "sequence aggregates take none"},
        "prop": {"type": ["string", "null"], "default": None,
                 "minLength": 1,
                 "description": "property key, required when of = 'prop'"},
        # deliberately not called `window`: that name is taken by the
        # operator's own valid-time bounds, and a planner that confuses the
        # two writes a {t_a, t_b} object here
        "span": {"type": ["integer", "null"], "minimum": 1, "default": None,
                 "description": "sliding-window width, required by "
                                "max_in_window; the window is [t, t + span)"},
        "gap": {"type": ["integer", "null"], "minimum": 1, "default": None,
                "description": "session-splitting threshold, required by "
                               "max_session_span; a run continues while "
                               "consecutive events are at most this apart"},
    },
    "required": ["agg"],
    "additionalProperties": False,
}
PROP_FILTER = {
    "type": ["object", "null"],
    "default": None,
    "properties": {
        "prop": {"type": "string", "minLength": 1},
        "cmp": {"type": "string", "enum": PROP_CMPS},
        "value": {},
    },
    "required": ["prop", "cmp", "value"],
    "additionalProperties": False,
    "description": "keep only events whose property compares as stated; "
                   "rows whose value is absent or of another JSON type are "
                   "excluded and counted in `prop_coercion` (D-052)",
}
ENDPOINT_FILTER = {
    "type": ["object", "null"],
    "default": None,
    "properties": {
        "role": {"type": "string", "enum": ["src", "dst", "either"]},
        "uids": {"type": "array", "items": {"type": "string"},
                 "maxItems": 50_000},
    },
    "required": ["role", "uids"],
    "additionalProperties": False,
    "description": "restrict events to those whose endpoint is in `uids` — "
                   "the cohort pre-filter a prior step's result feeds by "
                   "$ref. An empty list is an empty population, not an error",
}
#: `undirected` folds A->B together with B->A; `reciprocal` does that and
#: then keeps only pairs where **both** directions occurred. Both are decided
#: over the whole group set before pagination, which is exactly why they are
#: operator arguments and not a `compute` function over a `$ref` page.
PAIR_MODE = {"type": ["string", "null"],
             "enum": ["undirected", "reciprocal", None], "default": None,
             "description": "requires group_by = [endpoint src, endpoint dst]"}


def _dim_key(d: dict[str, Any]) -> tuple[str, str | None]:
    return (d["dim"], d.get("role"))


def _agg_field(a: dict[str, Any]) -> str:
    """Deterministic output field name for one aggregate spec."""
    if a["agg"] == "count":
        return "count"
    if a["agg"] in SEQ_AGGS:
        # the span stays out of the field name on purpose: a planner has to
        # reproduce this string in a later `filter` step, and
        # `max_in_window_86400000000` is a thing to get wrong. The price is
        # one span per call, refused explicitly rather than as a duplicate.
        return a["agg"]
    if a["agg"] == "count_distinct":
        return f"distinct_{a['of']}"
    if a.get("of") == "prop":
        # `prop_` keeps a property called `vt_s` from colliding with the
        # built-in source of the same name
        return f"{a['agg']}_prop_{a['prop']}"
    return f"{a['agg']}_{a['of']}"


_prop_keys = prop_keys      # the shared definition lives in props.py


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
        if a.get("prop") is not None and a.get("of") != "prop":
            raise InvalidArgError(
                "'prop' is only meaningful with of 'prop'")
        if a.get("span") is not None and a["agg"] != "max_in_window":
            raise InvalidArgError(
                "'span' is only meaningful with 'max_in_window'")
        if a.get("gap") is not None and a["agg"] != "max_session_span":
            raise InvalidArgError(
                "'gap' is only meaningful with 'max_session_span'")
        if a["agg"] in SEQ_AGGS:
            if a.get("of") is not None:
                raise InvalidArgError(
                    f"aggregate {a['agg']!r} takes no 'of'; a sequence is "
                    f"always ordered by vt_s")
            if a["agg"] == "max_in_window" and a.get("span") is None:
                raise InvalidArgError(
                    "aggregate 'max_in_window' requires 'span', the window "
                    "width in the same units as vt_s")
            if a["agg"] == "max_session_span" and a.get("gap") is None:
                raise InvalidArgError(
                    "aggregate 'max_session_span' requires 'gap', the "
                    "largest hole a single run may contain")
        elif a["agg"] == "count":
            if a.get("of") is not None:
                raise InvalidArgError("aggregate 'count' takes no 'of'")
        elif a["agg"] == "count_distinct":
            if a.get("of") not in ("src", "dst"):
                raise InvalidArgError(
                    "aggregate 'count_distinct' requires of 'src' or 'dst'")
        else:
            if a.get("of") not in ("vt_s", "duration", "prop"):
                raise InvalidArgError(
                    f"aggregate {a['agg']!r} requires of "
                    f"'vt_s', 'duration' or 'prop'")
            if a["of"] == "prop" and not a.get("prop"):
                raise InvalidArgError(
                    "of 'prop' requires a property name in 'prop'")
        f = _agg_field(a)
        if f in fields:
            if a["agg"] in SEQ_AGGS:
                # two spans would want one column; name the actual repair
                raise InvalidArgError(
                    f"only one {a['agg']!r} per call, because the output "
                    f"column is named for the aggregate and not for its "
                    f"span; a second span needs a second call")
            raise InvalidArgError(f"duplicate aggregate {f!r}")
        fields.add(f)

    if args.get("pair_mode") is not None and \
            [_dim_key(d) for d in dims] != [("endpoint", "src"),
                                            ("endpoint", "dst")]:
        raise InvalidArgError(
            "pair_mode requires group_by = [endpoint src, endpoint dst]; "
            "folding or matching directions is only defined over a pair")

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


def _cohort_ids(adapter: StorageAdapter, uids: list[str]) -> list[int]:
    """Dense ids for a cohort, tolerating uids the store has never seen.

    A cohort arrives from an earlier step or from a task's input, so a uid
    that does not exist is an ordinary empty contribution rather than an
    error — the same judgment `tgms/eval/baselines.py` makes for seed uids.
    The bulk call is one boundary crossing; only a cohort that actually
    contains an unknown pays for the per-uid fallback.
    """
    if not uids:
        return []
    try:
        return [int(i) for i in adapter.dense_ids(uids)]
    except NotFoundError:
        out = []
        for u in uids:
            try:
                out.append(int(adapter.dense_ids([u])[0]))
            except NotFoundError:
                continue
        return out


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
    # The repair loop consumes these levers, so a lever that exists and is
    # not named here is a lever no planner can reach. `endpoint_filter` and
    # `pair_mode: undirected` both narrow (D-054) and were missing.
    return LimitError(
        f"group count {n} exceeds cap {MAX_GROUPS}; narrow the window, add a "
        f"rel_types filter, restrict to a cohort with endpoint_filter, fold "
        f"directions with pair_mode 'undirected', or use coarser dimensions")


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


def _sequence_agg(a: dict[str, Any], inv: np.ndarray, vt_s: np.ndarray,
                  g: int, t_a: int, t_b: int) -> list[Any]:
    """One of D-056's sequence aggregates, over every group at once.

    Sort the events by (group, vt_s) once and each group becomes a
    contiguous run of non-decreasing times; the three aggregates are then
    array arithmetic on adjacent pairs. `np.maximum.at` reduces per group,
    with -1 as the sentinel for "this group produced no value" — legal
    because every quantity here is a non-negative duration or count.

    Spans are clamped to the valid-time window first. That is exact — a
    window at least as wide as the window itself always covers the whole
    group, and a hole threshold that wide can never be exceeded — and it is
    what keeps `t + span` inside int64 for a `span` that arrived as an
    arbitrary JSON integer.
    """
    kind, n = a["agg"], len(vt_s)
    if n == 0:
        # every window over no events holds none; a duration between events
        # that do not exist is not zero, it is absent
        return [0 if kind == "max_in_window" else None] * g
    order = np.lexsort((vt_s, inv))
    gi, t = inv[order], vt_s[order]

    if kind == "max_in_window":
        span = min(int(a["span"]), t_b - t_a)
        # rank-compress the times so that (group, rank) is one monotone
        # int64 key: searching for `t + span` in the *global* array is only
        # valid because the group prefix confines the answer to this group's
        # block, and falls through to the next block's first index — which
        # is this block's end — when the whole group fits in the window.
        uniq = np.unique(t)
        base = len(uniq) + 1
        key = gi * base + np.searchsorted(uniq, t)
        query = gi * base + np.searchsorted(uniq, t + span, side="left")
        counts = np.searchsorted(key, query, side="left") - np.arange(n)
        out = np.zeros(g, dtype=np.int64)
        np.maximum.at(out, gi, counts)
        return [int(v) for v in out]

    same = gi[1:] == gi[:-1]            # an adjacent pair inside one group
    d = t[1:] - t[:-1]
    out = np.full(g, -1, dtype=np.int64)
    if kind == "max_gap":
        if same.any():
            np.maximum.at(out, gi[1:][same], d[same])
        # -1 survives for a group of fewer than two events: no pair, no gap
        return [None if v < 0 else int(v) for v in out]

    gap = min(int(a["gap"]), t_b - t_a)
    starts = np.empty(n, dtype=bool)     # first event of a group, or of a run
    starts[0] = True
    starts[1:] = ~same | (d > gap)
    si = np.flatnonzero(starts)
    ei = np.append(si[1:], n) - 1
    np.maximum.at(out, gi[si], t[ei] - t[si])
    # a lone event is a run whose span is 0; only an empty group stays -1
    return [None if v < 0 else int(v) for v in out]


def _portable(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    dims, aggs = args["group_by"], args["aggregates"]
    need_rel = any(d["dim"] == "rel_type" for d in dims)
    prop_keys = _prop_keys(args)
    cols = ["src_id", "dst_id", "vt_s", "vt_e"] + (["rel_type"] if need_rel else [])
    if prop_keys:
        cols.append("props")            # opt-in; never on a bare scan
    # The cohort pre-filter pushes down as an incidence filter the scan has
    # carried all along. `touching_ids` is src-OR-dst, so for a role-specific
    # cohort it is a *superset* filter that only prunes; the exact predicate
    # is applied below, where correctness does not depend on the pushdown.
    ef = args.get("endpoint_filter")
    cohort_ids: list[int] = []
    if ef is not None:
        cohort_ids = _cohort_ids(adapter, ef["uids"])
    e = adapter.edges_columnar(
        as_of_tt=args["as_of_tt"], vt_min=t_a, vt_max=t_b,
        rel_types=args["rel_types"], columns=tuple(cols),
        # an empty cohort cannot be pushed down (`IN ()` is not SQL); the
        # mask below makes the population empty either way
        touching_ids=cohort_ids or None)
    # the scan filter is interval *overlap*; an event is containment of vt_s
    m = e["vt_s"] >= t_a
    if ef is not None:
        cohort = np.asarray(cohort_ids, dtype=np.int64)
        in_src = np.isin(e["src_id"], cohort)
        in_dst = np.isin(e["dst_id"], cohort)
        m = m & {"src": in_src, "dst": in_dst,
                 "either": in_src | in_dst}[ef["role"]]
    vt_s, vt_e = e["vt_s"][m], e["vt_e"][m]
    src, dst = e["src_id"][m], e["dst_id"][m]
    rel = e["rel_type"][m] if need_rel else None

    # --- property typing (D-052) ------------------------------------------ #
    # One parse per surviving row, reused by the filter and every aggregate.
    # This is the per-row JSON cost the decision knowingly accepted; the
    # typed column is what removes it, once a measurement asks for it.
    skipped: dict[str, int] = {k: 0 for k in prop_keys}
    bags: list[dict[str, Any]] = []
    if prop_keys:
        bags = [parse_props(p) for p in e["props"][m]]
        pf = args.get("prop_filter")
        if pf is not None:
            verdicts = [matches(b, pf["prop"], pf["cmp"], pf["value"])
                        for b in bags]
            skipped[pf["prop"]] += sum(1 for v in verdicts if v is SKIP)
            keep = np.array([v is not SKIP and bool(v) for v in verdicts],
                            dtype=bool)
            vt_s, vt_e, src, dst = vt_s[keep], vt_e[keep], src[keep], dst[keep]
            if rel is not None:
                rel = rel[keep]
            bags = [b for b, k in zip(bags, keep) if k]
    n = len(vt_s)

    # --- pair modes (D-054) ------------------------------------------------ #
    # Both fold a directed pair onto its canonical (lo, hi) form; `reciprocal`
    # first drops pairs that occurred in only one direction. The transpose
    # test is a set membership over the directed pairs *present in the whole
    # window*, so it is O(n) and — crucially — decided before any pagination.
    if args.get("pair_mode") is not None and n:
        base = int(adapter.num_entities()) + 1
        lo, hi = np.minimum(src, dst), np.maximum(src, dst)
        if args["pair_mode"] == "reciprocal":
            present = set((src.astype(np.int64) * base
                           + dst.astype(np.int64)).tolist())
            # BOTH directions must be present, tested from the canonical
            # form: checking only "the other direction" would be trivially
            # true for whichever direction this row happens to be.
            # A self-pair is its own transpose, which the encoding gives free.
            keep = np.array(
                [(int(a) * base + int(b)) in present
                 and (int(b) * base + int(a)) in present
                 for a, b in zip(lo.tolist(), hi.tolist())], dtype=bool)
            vt_s, vt_e = vt_s[keep], vt_e[keep]
            lo, hi = lo[keep], hi[keep]
            if rel is not None:
                rel = rel[keep]
            if bags:
                bags = [b for b, k in zip(bags, keep) if k]
            n = len(vt_s)
        src, dst = lo, hi

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
        elif a["agg"] in SEQ_AGGS:
            agg_cols[field] = _sequence_agg(a, inv, vt_s, g, t_a, t_b)
        elif a["agg"] == "count_distinct":
            ids = src if a["of"] == "src" else dst
            if n:
                base = int(adapter.num_entities()) + 1
                uk = np.unique(inv * base + ids)
                dist = np.bincount(uk // base, minlength=g)
            else:
                dist = np.zeros(g, dtype=np.int64)
            agg_cols[field] = [int(v) for v in dist]
        elif a["of"] == "prop":
            # a Python loop on purpose: the values are arbitrary JSON
            # numbers, int and float mixed, and the exactness rule is stated
            # over Python ints rather than over any numpy dtype
            key = a["prop"]
            per_group: list[list[Any]] = [[] for _ in range(g)]
            for i, b in enumerate(bags):
                v = numeric_value(b, key)
                if v is SKIP:
                    skipped[key] += 1
                else:
                    per_group[int(inv[i])].append(v)
            fn = {"min": min, "max": max, "mean": _blessed_mean}[a["agg"]]
            agg_cols[field] = [fn(vals) if vals else None for vals in per_group]
            continue
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
    out = paginate(rows, args["limit"], args["cursor"])
    if prop_keys:
        # inside the payload, therefore inside result_digest: an answer
        # cannot rest on a shrunken denominator without saying so
        out["prop_coercion"] = {k: skipped[k] for k in prop_keys}
    return out


# --------------------------------------------------------------------------- #
# native path: one PyO3 crossing, rows built for the requested page only       #
# --------------------------------------------------------------------------- #


def _native(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    try:
        got = adapter.aggregate_events_columnar(
            as_of_tt=args["as_of_tt"],
            t_a=args["window"]["t_a"], t_b=args["window"]["t_b"],
            rel_types=args["rel_types"],
            stride=args.get("stride"),
            group_by=[(d["dim"], d.get("role")) for d in args["group_by"]],
            aggregates=[(a["agg"], a.get("of")) for a in args["aggregates"]],
            max_groups=MAX_GROUPS,
        )
    except InvalidArgError as e:
        # the kernel's capacity refusal is this operator's E_LIMIT, with the
        # same narrowing levers as the portable path
        if "group count" in str(e) and "exceeds cap" in str(e):
            raise LimitError(
                f"{str(e).split('capacity: ', 1)[-1]}; narrow the window, "
                f"add a rel_types filter, or use coarser dimensions") from None
        raise
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
        "prop_filter": PROP_FILTER,
        "endpoint_filter": ENDPOINT_FILTER,
        "pair_mode": PAIR_MODE,
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
    "null labels first). Properties (D-052): min/max/mean over "
    "of='prop' with a `prop` key, and `prop_filter` to select on one; a "
    "value participates only if its JSON type fits — text is never parsed "
    "into a number and a boolean is not one — and every excluded row is "
    "counted per property in `prop_coercion`. Sets (D-054): "
    "`endpoint_filter` {role: src|dst|either, uids} restricts events to a "
    "cohort from an earlier step; `pair_mode` over a "
    "[endpoint src, endpoint dst] grouping folds A->B with B->A "
    "(`undirected`) or additionally keeps only pairs that occurred both "
    "ways (`reciprocal`). Sequences (D-056), each over the group's events "
    "in vt_s order and taking no 'of': `max_gap` is the longest hole "
    "between two consecutive events (null below two events, 0 for "
    "simultaneous ones); `max_in_window` with `span` is the largest number "
    "of events in ANY window [t, t+span) — a sliding window, not the fixed "
    "stride a time_bucket dimension gives, and 0 over an empty group; "
    "`max_session_span` with `gap` is the longest run of events no two of "
    "which are more than `gap` apart, measured first to last. One `span` "
    "and one `gap` per call.",
    cost_fn=scan_estimate,
    validators=[_aggregate_validators],
    output_fields=("rows", "rows_total", "truncated", "cursor",
                   "prop_coercion"),
)
def aggregate_events(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    # A property-touching call takes the portable path on every backend: the
    # two-phase kernel aggregates fixed-width codes and has no notion of a
    # JSON blob, so there is nothing for it to be fast about yet. D-052 took
    # correctness first and made the typed column conditional on a
    # measurement; this is where that trade is actually paid, and the
    # measurement that would justify the column is a measurement of this.
    # D-056's sequence aggregates are portable-only for a different reason:
    # the two-phase kernel reduces each event into a running accumulator and
    # never holds a group's events in order, which is the one thing a gap, a
    # sliding window and a session all need. Pushing them down is a kernel
    # design question, and the measurement that would justify it is a
    # measurement of this.
    if (hasattr(adapter, "aggregate_events_columnar") and not _prop_keys(args)
            and args.get("pair_mode") is None
            and args.get("endpoint_filter") is None
            and not any(a["agg"] in SEQ_AGGS for a in args["aggregates"])):
        return _native(adapter, args)
    return _portable(adapter, args)
