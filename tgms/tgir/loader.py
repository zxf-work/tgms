"""Plan artifacts: JSON ⇄ `Node`.

The scoring harness's deliverable is 52 *artifacts* a reviewer can read and
diff, so plans have to be data. This is the inverse of the `to_json()` methods
`node.py` and `expr.py` already carry, plus the `dump` half they do not.

`node_digest` is already defined over canonical args, so the round-trip
`load(dump(plan)).node_digest == plan.node_digest` is a free and strong property
test — `tests/test_tgir_artifacts.py` runs it over every checked-in artifact.

**The file format is internal and unstable.** It carries `plan_format` so a
reader can refuse a version it does not know, and it is deliberately not a CLI
surface: publishing it would freeze the node encoding at the moment it is least
stable (§5).

**Σ is declared once, at the plan.** §3.1 says "a plan may declare Σ at its
root; nodes inherit it", and `node.py` has no inheritance mechanism — every node
carries its own field defaulting to `OPEN_END`, so a hand-written plan that
declares Σ only at the scan is rejected by §3.5's no-widening check. The loader
closes that gap where it belongs, at the artifact boundary: `sigma` at the top
level is applied to every node, and a node may still carry its own to narrow a
subtree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, Interval
from tgms.tgir import expr as expr_module
from tgms.tgir import node as node_module
from tgms.tgir.expr import Expr
from tgms.tgir.node import (
    Agg, Bounded, EdgePat, Endpoints, Exact, HopSpec, Node, NodePat, Pattern,
    SortKey, Source, Unbounded,
)
from tgms.tgir.types import Sigma, Tau

PLAN_FORMAT = 1

#: The twelve core node types, by their spec names.
NODE_TYPES = {t.__name__: t for t in node_module.CORE_NODE_TYPES}

#: Constructor argument names that are *not* inputs, per node type. Everything
#: else in a node object is passed through to the dataclass.
_INPUT_FIELDS = {
    "Expand": ("input",), "Filter": ("input",), "PropertyPredicate": ("input",),
    "TypeConstraint": ("input",), "Project": ("input",), "Order": ("input",),
    "Limit": ("input",), "Aggregate": ("input",), "Join": ("left", "right"),
}


def load_file(path: str | Path) -> Node:
    return load(json.loads(Path(path).read_text()))


def load(document: dict[str, Any]) -> Node:
    """A plan document → its root node."""
    version = document.get("plan_format")
    if version != PLAN_FORMAT:
        raise InvalidArgError(
            f"unknown plan_format {version!r}; this reader knows {PLAN_FORMAT}")
    sigma = _sigma(document.get("sigma"))
    return _node(document["root"], sigma)


def dump(root: Node, *, plan_id: str = "", sigma: Sigma | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"plan_format": PLAN_FORMAT, "root": _dump_node(root)}
    if plan_id:
        out["plan_id"] = plan_id
    if sigma is not None:
        out["sigma"] = {"t_v": sigma.vt_json(), "t_b": sigma.t_b}
    return out


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _sigma(spec: Any) -> Sigma:
    if spec is None:
        return Sigma.default()
    intervals = tuple(Interval(int(a), int(b)) for a, b in spec.get(
        "t_v", [[0, OPEN_END]]))
    return Sigma(intervals, int(spec.get("t_b", OPEN_END)))


def _node(spec: dict[str, Any], sigma: Sigma) -> Node:
    op = spec.get("op")
    if op not in NODE_TYPES:
        raise InvalidArgError(f"unknown node op {op!r}", known=sorted(NODE_TYPES))
    cls = NODE_TYPES[op]
    own = _sigma(spec["sigma"]) if "sigma" in spec else sigma

    kwargs: dict[str, Any] = {}
    for field, value in spec.items():
        if field in ("op", "sigma", "inputs"):
            continue
        kwargs[_field_name(field)] = _value(op, field, value, own)

    inputs = [_node(child, own) for child in spec.get("inputs", [])]
    for position, name in enumerate(_INPUT_FIELDS.get(op, ())):
        if position < len(inputs):
            kwargs[name] = inputs[position]
    kwargs["sigma_"] = own
    return cls(**kwargs)


def _field_name(field: str) -> str:
    return {"as": "as_", "from": "from_", "type": "join_type",
            "dir": "dir"}.get(field, field)


def _value(op: str, field: str, value: Any, sigma: Sigma) -> Any:
    if value is None:
        return None
    if field in ("labels", "uids", "rel_types") and isinstance(value, list):
        return tuple(value)
    if field == "endpoints":
        return Endpoints(value["role"], tuple(value["uids"]))
    if field == "hops":
        return _hops(value)
    if field == "pred":
        return _expr(value)
    if field == "bindings":
        return tuple((name, _expr(e)) for name, e in value)
    if field == "on":
        return tuple((left, right) for left, right in value)
    if field == "keys":
        return tuple(SortKey(_expr(k["key"]), k.get("dir", "asc"),
                             k.get("nulls", "nulls_last")) for k in value)
    if field == "group_by":
        return tuple((name, _expr(e)) for name, e in value)
    if field == "aggregates":
        return tuple(Agg(a["fn"], a["alias"],
                         _expr(a["of"]) if a.get("of") else None) for a in value)
    if field == "pattern":
        return Pattern(
            tuple(NodePat(p["var"], p.get("label")) for p in value["nodes"]),
            tuple(EdgePat(p["var"], p["src"], p["dst"], p.get("rel_type"),
                          p.get("directed", True)) for p in value["edges"]))
    if field == "sources":
        return tuple(Source(s["var"], _node(s["relation"], sigma), s.get("column"))
                     for s in value)
    return value


def _hops(spec: Any) -> HopSpec:
    if "exact" in spec:
        return Exact(int(spec["exact"]))
    if "bounded" in spec:
        low, high = spec["bounded"]
        return Bounded(int(low), int(high))
    if "unbounded" in spec:
        return Unbounded(int(spec["unbounded"]))
    raise InvalidArgError(f"unknown hop spec {spec!r}")


def _expr(spec: Any) -> Expr:
    if not isinstance(spec, dict):
        raise InvalidArgError(f"not an expression: {spec!r}")
    if "lit" in spec:
        return expr_module.Lit(spec["lit"], _tau(spec.get("tau")))
    if "col" in spec:
        return expr_module.Col(spec["col"])
    if "prop" in spec:
        column, key = spec["prop"]
        return expr_module.PropRef(column, key)
    if "arith" in spec:
        return expr_module.Arith(spec["arith"], _expr(spec["l"]), _expr(spec["r"]))
    if "fn" in spec:
        return expr_module.MathFn(spec["fn"], _expr(spec["arg"]))
    if "cmp" in spec:
        return expr_module.Cmp(spec["cmp"], _expr(spec["l"]), _expr(spec["r"]))
    if "not" in spec:
        return expr_module.Not(_expr(spec["not"]))
    if "bool" in spec:
        return expr_module.BoolOp(spec["bool"], _expr(spec["l"]), _expr(spec["r"]))
    if "is_null" in spec:
        return expr_module.IsNull(_expr(spec["is_null"]))
    if "coalesce" in spec:
        left, right = spec["coalesce"]
        return expr_module.Coalesce(_expr(left), _expr(right))
    if "if" in spec:
        cond, then, otherwise = spec["if"]
        return expr_module.If(_expr(cond), _expr(then), _expr(otherwise))
    if "tuple" in spec:
        return expr_module.TupleExpr(tuple(_expr(e) for e in spec["tuple"]))
    if "cast" in spec:
        return expr_module.Cast(_expr(spec["cast"]), _tau(spec["to"]))
    raise InvalidArgError(f"unknown expression {sorted(spec)!r}")


def _tau(spec: Any) -> Tau | None:
    if spec is None:
        return None
    text = str(spec)
    nullable = text.endswith("?")
    base = text[:-1] if nullable else text
    return Tau(base, (), nullable)


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------

def _dump_node(node: Node) -> dict[str, Any]:
    out: dict[str, Any] = {"op": node.op}
    out.update(node.canonical_args())
    out["sigma"] = {"t_v": node.sigma.vt_json(), "t_b": node.sigma.t_b}
    inputs = [_dump_node(i) for i in node.inputs]
    if isinstance(node, node_module.PatternMatch):
        # a pattern's inputs *are* its sources, and `sources` already names them
        inputs = []
    if inputs:
        out["inputs"] = inputs
    return _rename_out(out)


def _rename_out(spec: dict[str, Any]) -> dict[str, Any]:
    mapping = {"as_": "as", "from_": "from", "join_type": "type"}
    return {mapping.get(k, k): v for k, v in spec.items()}


__all__ = ["NODE_TYPES", "PLAN_FORMAT", "dump", "load", "load_file"]
