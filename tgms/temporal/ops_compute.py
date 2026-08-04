"""O13 `compute` (WP2.1): deterministic post-processing over prior step
outputs, so plans never need LLM arithmetic. Functions: count, sum, min, max,
mean(field), median(field), topk(field, k), filter(field, cmp, value),
ratio(x, y), diff(x, y), percent(x, y), interval_relation(a, b).

The `input` list arrives via $ref from a prior step, as do the scalar
operands `x` and `y`; the operator itself never touches the store.

THE BLESSED ARITHMETIC RULE (the `AR` capability). Every quotient here — the
means, the medians of an even-sized group, the ratios and the percentages —
is formed the same way, because a number that reaches an answer must hash
identically wherever it was computed:

  * where every contributing value is an `int`, the quotient is formed in
    exact integer arithmetic and returned as an `int` when it is exact;
    otherwise exactly one IEEE rounding is applied, in D-044's form
    `q, r = divmod(num, den)` -> `float(q) + r / den`;
  * where any contributing value is a `float`, the terms are summed with
    `math.fsum` (correctly rounded and order-independent) and divided once.

Staying integral is not a nicety. Valid times are epoch microseconds and
OPEN_END is 2**62, so a float64 cannot hold the sums this operator averages;
D-044 widened `mean_duration` to float only because its output column is
typed, and `compute` returns a bare `value` with no column type to satisfy.
Type follows the data here, as it already did for `sum`.
"""

from __future__ import annotations

import math
from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import LIMIT, operator, paginate, required

FNS = ["count", "sum", "min", "max", "mean", "median", "topk", "filter",
       "ratio", "diff", "percent", "intersect", "difference", "union",
       "derive", "join", "interval_relation"]
#: row-wise ops for `derive`. Closed on purpose: these are what the blocked
#: questions ask for, and an expression language is a façade decision.
DERIVE_OPS = ["add", "sub", "mul", "div", "floordiv", "concat"]
#: set operations over two lists of scalars (uids, in practice)
SET_FNS = ("intersect", "difference", "union")
#: aggregate a group of rows down to one number
REDUCERS = ("sum", "min", "max", "mean", "median")
#: combine two scalars from earlier steps
BINARY = ("ratio", "diff", "percent")
CMPS = ["eq", "ne", "lt", "le", "gt", "ge", "contains"]

INTERVAL = {
    "type": "object",
    "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
    "required": ["start", "end"], "additionalProperties": False,
}

ARGS = {
    "fn": required({"type": "string", "enum": FNS}),
    "input": {"type": ["array", "null"], "maxItems": 50_000, "default": None,
              "description": "rows or scalars from a prior step ($ref)"},
    "field": {"type": ["string", "null"], "default": None,
              "description": "object field to aggregate/compare on"},
    "k": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000, "default": None},
    "cmp": {"type": ["string", "null"], "enum": CMPS + [None], "default": None},
    "value": {"default": None,
              "description": "comparison value for filter"},
    "field2": {"type": ["string", "null"], "default": None,
               "description": "second field for derive (with `field`)"},
    "op": {"type": ["string", "null"], "enum": DERIVE_OPS + [None],
           "default": None, "description": "row-wise operation for derive"},
    "into": {"type": ["string", "null"], "default": None, "minLength": 1,
             "description": "name of the column derive adds"},
    "on": {"type": ["string", "null"], "default": None,
           "description": "join key in `input`"},
    "other_on": {"type": ["string", "null"], "default": None,
                 "description": "join key in `other`; defaults to `on`"},
    "how": {"type": "string", "enum": ["inner", "left"], "default": "inner",
            "description": "inner keeps matched keys only; left keeps every "
                           "left row and fills the right side"},
    "fill": {"default": None,
             "description": "value for the right side on an unmatched left "
                            "row (how = left)"},
    "other_prefix": {"type": "string", "default": "r_", "minLength": 1,
                     "description": "prefix for the right side's columns"},
    "other": {"type": ["array", "null"], "maxItems": 50_000, "default": None,
              "description": "second list for intersect/difference/union ($ref)"},
    "other_field": {"type": ["string", "null"], "default": None,
                    "description": "field to project from `other`'s rows; "
                                   "defaults to `field`"},
    "x": {"type": ["number", "null"], "default": None,
          "description": "first operand of ratio/diff/percent ($ref)"},
    "y": {"type": ["number", "null"], "default": None,
          "description": "second operand of ratio/diff/percent ($ref)"},
    "a": {**INTERVAL, "type": ["object", "null"], "default": None},
    "b": {**INTERVAL, "type": ["object", "null"], "default": None},
    "limit": LIMIT,
}


def _values(rows: list[Any], f: str | None) -> list[Any]:
    if f is None:
        return rows
    out = []
    for r in rows:
        if not isinstance(r, dict) or f not in r:
            raise InvalidArgError(f"compute: field {f!r} missing from input row")
        out.append(r[f])
    return out


def _numbers(rows: list[Any], f: str | None, fn: str) -> list[int | float]:
    """Values of `field`, checked to be finite numbers. `True` is not 1 here:
    a boolean reaching an arithmetic operator is a plan bug, not a 1."""
    vals = _values(rows, f)
    for v in vals:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise InvalidArgError(f"compute {fn}: non-numeric values")
        if isinstance(v, float) and not math.isfinite(v):
            raise InvalidArgError(f"compute {fn}: non-finite value {v!r}")
    return vals


def _quotient(num: int | float, den: int | float, fn: str) -> int | float:
    """`num / den` under the blessed rule (see the module docstring)."""
    if den == 0:
        raise InvalidArgError(f"compute {fn}: division by zero")
    if isinstance(num, int) and isinstance(den, int):
        q, r = divmod(num, den)
        return q if r == 0 else float(q) + r / den
    return num / den


def _mean(vals: list[int | float]) -> int | float:
    if all(isinstance(v, int) for v in vals):
        return _quotient(sum(vals), len(vals), "mean")
    return math.fsum(vals) / len(vals)


def _operands(args: dict[str, Any], fn: str) -> tuple[int | float, int | float]:
    x, y = args["x"], args["y"]
    if x is None or y is None:
        raise InvalidArgError(f"compute {fn} requires x and y")
    for name, v in (("x", x), ("y", y)):
        if isinstance(v, float) and not math.isfinite(v):
            raise InvalidArgError(f"compute {fn}: non-finite {name} {v!r}")
    return x, y


def _members(rows: list[Any], f: str | None, fn: str) -> set[Any]:
    """A set of scalars from a list of rows or bare values. Members must be
    hashable scalars: a set of dicts is a plan bug, not an empty set."""
    out = set()
    for v in _values(rows, f):
        if isinstance(v, (dict, list)):
            raise InvalidArgError(
                f"compute {fn}: set members must be scalars, got "
                f"{type(v).__name__} — name the column with `field`")
        out.add(v)
    return out


def _sorted_members(s: set[Any]) -> list[Any]:
    """Canonical order for a set answer. Sorting by (type name, value) keeps
    a mixed-type set total-orderable, so the digest never depends on which
    order the inputs arrived in."""
    return sorted(s, key=lambda v: (type(v).__name__, v))


def _derive_one(a: Any, b: Any, op: str) -> Any:
    """One row's derived value. `concat` is the only op that takes
    non-numbers, because its purpose is building a composite *key*."""
    if op == "concat":
        return f"{a}|{b}"
    for v in (a, b):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise InvalidArgError(
                f"compute derive {op}: non-numeric operand {v!r}")
    if op in ("div", "floordiv") and b == 0:
        raise InvalidArgError(f"compute derive {op}: division by zero")
    if op == "div":
        return _quotient(a, b, "derive")        # D-051's rule, unchanged
    if op == "floordiv":
        return a // b
    return {"add": a + b, "sub": a - b, "mul": a * b}[op]


def _rows_of(rows: list[Any], fn: str) -> list[dict[str, Any]]:
    for r in rows:
        if not isinstance(r, dict):
            raise InvalidArgError(
                f"compute {fn}: expected rows with named fields, got "
                f"{type(r).__name__}")
    return rows


def _keyed(rows: list[Any], key: str, fn: str, side: str) -> dict[Any, dict]:
    """Index a row set by `key`, refusing duplicates.

    Uniqueness is the join's *bound*: a grouped result has one row per group
    and always satisfies it, and requiring it caps the output at
    min(|left|, |right|) rather than their product. Enforcing it here means
    the join needs no cost model at all.
    """
    out: dict[Any, dict] = {}
    for r in _rows_of(rows, fn):
        if key not in r:
            raise InvalidArgError(
                f"compute {fn}: {side} rows have no field {key!r}")
        if r[key] in out:
            raise InvalidArgError(
                f"compute {fn}: duplicate key {r[key]!r} on the {side} side; "
                f"join keys must be unique (group first, or pick a key that "
                f"is)")
        out[r[key]] = r
    return out


def _cmp(x: Any, cmp: str, v: Any) -> bool:
    if cmp == "contains":
        return isinstance(x, (str, list)) and v in x
    try:
        return {"eq": x == v, "ne": x != v, "lt": x < v, "le": x <= v,
                "gt": x > v, "ge": x >= v}[cmp]
    except TypeError:
        raise InvalidArgError(
            f"compute: cannot compare {type(x).__name__} with {type(v).__name__}") from None


def allen_relation(a: dict[str, int], b: dict[str, int]) -> str:
    """Full Allen classification for half-open integer intervals."""
    (as_, ae), (bs, be) = (a["start"], a["end"]), (b["start"], b["end"])
    if ae < bs:
        return "before"
    if ae == bs:
        return "meets"
    if be < as_:
        return "after"
    if be == as_:
        return "met_by"
    if as_ == bs and ae == be:
        return "equals"
    if as_ == bs:
        return "starts" if ae < be else "started_by"
    if ae == be:
        return "finishes" if as_ > bs else "finished_by"
    if bs < as_ and ae < be:
        return "during"
    if as_ < bs and be < ae:
        return "contains"
    return "overlaps" if as_ < bs else "overlapped_by"


@operator(
    "compute",
    ARGS,
    "Deterministic computation over a prior step's rows: count/sum/min/max/"
    "mean/median (optionally over `field`), topk(field, k), "
    "filter(field, cmp, value); arithmetic over two scalars from earlier "
    "steps: ratio(x, y) = x/y, diff(x, y) = x-y, percent(x, y) = 100*x/y; "
    "set operations over two lists (`input` and `other`, optionally "
    "projected with `field`/`other_field`): intersect, difference, union — "
    "deduplicated and canonically ordered; row work: "
    "derive(field, field2|value, op, into) adds one computed column "
    "(add/sub/mul/div/floordiv/concat) and join(other, on, other_on, how, "
    "fill) aligns two prior steps on a key whose values must be unique on "
    "both sides; "
    "or interval_relation(a, b) -> Allen relation name. Never do arithmetic "
    "in prose — use this.",
    output_fields=("value", "rows", "rows_total", "truncated", "cursor"),
)
def compute(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    fn = args["fn"]
    if fn == "interval_relation":
        if args["a"] is None or args["b"] is None:
            raise InvalidArgError("interval_relation requires a and b")
        for iv in (args["a"], args["b"]):
            if not (iv["start"] < iv["end"]):
                raise InvalidArgError(f"invalid interval {iv}")
        return {"value": allen_relation(args["a"], args["b"]), "truncated": False}
    if fn == "derive":
        rows, f, f2, val = args["input"], args["field"], args["field2"], args["value"]
        op, into = args["op"], args["into"]
        if rows is None or f is None or op is None or into is None:
            raise InvalidArgError(
                "compute derive requires input, field, op and into")
        if (f2 is None) == (val is None):
            raise InvalidArgError(
                "compute derive takes exactly one of field2 or value")
        out = []
        for r in _rows_of(rows, "derive"):
            if into in r:
                raise InvalidArgError(
                    f"compute derive: {into!r} already exists; derive adds a "
                    f"column and never replaces one")
            b = r[f2] if f2 is not None else val
            if f2 is not None and f2 not in r:
                raise InvalidArgError(f"compute derive: field {f2!r} missing")
            out.append({**r, into: _derive_one(_values([r], f)[0], b, op)})
        return paginate(out, args["limit"], None)
    if fn == "join":
        rows, other, on = args["input"], args["other"], args["on"]
        if rows is None or other is None or on is None:
            raise InvalidArgError("compute join requires input, other and on")
        other_on, pre, how = args["other_on"] or on, args["other_prefix"], args["how"]
        right = _keyed(other, other_on, "join", "right")
        left = _keyed(rows, on, "join", "left")
        merged = []
        for k in sorted(left, key=lambda v: (type(v).__name__, v)):
            r = right.get(k)
            if r is None:
                if how == "inner":
                    continue
                r = {c: args["fill"] for c in
                     {c for row in other for c in row} | {other_on}}
            row = dict(left[k])
            for c, v in r.items():
                name = f"{pre}{c}"
                if name in row:
                    raise InvalidArgError(
                        f"compute join: {name!r} collides with a left column; "
                        f"choose a different other_prefix")
                row[name] = v
            merged.append(row)
        return paginate(merged, args["limit"], None)
    if fn in SET_FNS:
        rows, other = args["input"], args["other"]
        if rows is None or other is None:
            raise InvalidArgError(f"compute {fn} requires input and other")
        a = _members(rows, args["field"], fn)
        b = _members(other, args["other_field"] or args["field"], fn)
        out = {"intersect": a & b, "difference": a - b, "union": a | b}[fn]
        return paginate(_sorted_members(out), args["limit"], None)
    if fn in BINARY:
        x, y = _operands(args, fn)
        val = (x - y if fn == "diff"
               else _quotient(x if fn == "ratio" else 100 * x, y, fn))
        return {"value": val, "truncated": False}

    rows = args["input"]
    if rows is None:
        raise InvalidArgError(f"compute fn={fn} requires input")
    if fn == "count":
        return {"value": len(rows), "truncated": False}
    if fn in REDUCERS:
        vals = _numbers(rows, args["field"], fn)
        if not vals and fn != "sum":
            raise InvalidArgError(f"compute {fn}: empty input")
        if fn in ("sum", "min", "max"):
            val = {"sum": sum, "min": min, "max": max}[fn](vals) if vals else 0
        elif fn == "mean":
            val = _mean(vals)
        else:
            # median: an odd group has an exact middle datum and no
            # arithmetic is invented for it; an even one is the blessed mean
            # of the two straddling values
            s = sorted(vals)
            n = len(s)
            val = s[n // 2] if n % 2 else _mean([s[n // 2 - 1], s[n // 2]])
        return {"value": val, "truncated": False}
    if fn == "topk":
        if args["field"] is None or args["k"] is None:
            raise InvalidArgError("topk requires field and k")
        vals = _values(rows, args["field"])
        order = sorted(range(len(rows)),
                       key=lambda i: (-(vals[i] if isinstance(vals[i], (int, float))
                                        else 0), str(rows[i])))
        return paginate([rows[i] for i in order[: args["k"]]],
                        args["limit"], None)
    if fn == "filter":
        if args["cmp"] is None:
            raise InvalidArgError("filter requires cmp")
        vals = _values(rows, args["field"])
        kept = [r for r, v in zip(rows, vals) if _cmp(v, args["cmp"], args["value"])]
        return paginate(kept, args["limit"], None)
    raise InvalidArgError(f"unknown compute fn {fn}")
