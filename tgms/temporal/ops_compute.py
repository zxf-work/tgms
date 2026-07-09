"""O13 `compute` (WP2.1): deterministic post-processing over prior step
outputs, so plans never need LLM arithmetic. Functions: count, sum, min, max,
topk(field, k), filter(field, cmp, value), interval_relation(a, b).

The `input` list arrives via $ref from a prior step; the operator itself
never touches the store.
"""

from __future__ import annotations

from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import LIMIT, operator, paginate, required

FNS = ["count", "sum", "min", "max", "topk", "filter", "interval_relation"]
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
    "Deterministic computation over a prior step's rows: count/sum/min/max "
    "(optionally over `field`), topk(field, k), filter(field, cmp, value), "
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

    rows = args["input"]
    if rows is None:
        raise InvalidArgError(f"compute fn={fn} requires input")
    if fn == "count":
        return {"value": len(rows), "truncated": False}
    if fn in ("sum", "min", "max"):
        vals = _values(rows, args["field"])
        if not vals and fn != "sum":
            raise InvalidArgError(f"compute {fn}: empty input")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in vals):
            raise InvalidArgError(f"compute {fn}: non-numeric values")
        val = {"sum": sum, "min": min, "max": max}[fn](vals) if vals else 0
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
