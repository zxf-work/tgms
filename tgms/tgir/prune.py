"""Plan-time column pruning — `live_columns(root) → {node_digest: {column}}`.

§2.1 and §2.2 make the scans emit eight and eleven columns, most of which most
plans never read — and two families are the expensive ones the storage layer's
projection pushdown exists to avoid: `props` is the whole canonical-JSON blob
per row (D-052, D-069), and node `props` have no columnar route at all.

So M3 computes, **once per plan before execution**, which columns each node's
output is actually read for, propagating backwards from the root. A scan
materializes exactly its live set.

**This is not an optimization the spec forbids.** §3.7 forbids *plan rewriting*
— fusing operators, pushing predicates into scan parameters, hoisting a `Limit`
— because a rewrite changes a node's recorded bound args and therefore its
dependency scope (D13.14 prohibition 2), and because §3.5's ruling is that
filters narrow the declared domain and never the scope. Pruning changes no
node's arguments, no row and no order: only which arrays get built. The node
digests, the scopes and the results are identical with it and without it, which
is the property `tests/test_tgir_eval_core.py` pins.

A lazily-resolving `Relation` was considered and rejected: it puts store access
behind a property accessor, which is exactly what §2.0's `∅`-classification
guard exists to make impossible to do by accident.
"""

from __future__ import annotations

from tgms.tgir.node import (
    Aggregate, EdgeScan, Expand, Filter, Join, Limit, Node, NodeScan, Order,
    PatternMatch, Project, PropertyPredicate, TypeConstraint,
)

#: Columns a node needs from its own inputs, beyond what its consumers ask for.
LiveMap = dict[str, frozenset[str]]


def live_columns(root: Node) -> LiveMap:
    """Live columns per node, keyed by `node_digest`.

    Walks top-down: the root is live in its whole output schema, and each node
    adds what its own arguments read before passing the demand to its inputs. A
    DAG node reached twice accumulates the union of both demands, which is why
    the walk revisits rather than memoizing on first sight.
    """
    live: dict[str, set[str]] = {}
    _walk(root, set(root.out_schema.names), live)
    return {digest: frozenset(names) for digest, names in live.items()}


def _walk(node: Node, demanded: set[str], live: dict[str, set[str]]) -> None:
    key = node.node_digest
    before = live.get(key)
    wanted = set(demanded) & set(node.out_schema.names)
    if before is not None and wanted <= before:
        return                      # this subtree has already seen this demand
    live.setdefault(key, set()).update(wanted)

    own = _own_reads(node)
    for i in node.inputs:
        available = set(i.out_schema.names)
        # what this node passes through, plus what it reads itself
        _walk(i, (live[key] | own) & available, live)


def _own_reads(node: Node) -> set[str]:
    """The columns a node's own arguments read, independent of its consumers."""
    out: set[str] = set()
    if isinstance(node, Filter):
        out |= set(node.pred.columns())
    elif isinstance(node, PropertyPredicate):
        out.add(f"{node.var}.props")
    elif isinstance(node, TypeConstraint):
        out.add(f"{node.var}." + ("label" if node.labels is not None else "rel_type"))
    elif isinstance(node, Project):
        for _name, expr in node.bindings:
            out |= set(expr.columns())
    elif isinstance(node, Order):
        for key in node.keys:
            out |= set(key.key.columns())
    elif isinstance(node, Join):
        out |= {lhs for lhs, _ in node.on} | {rhs for _, rhs in node.on}
    elif isinstance(node, Aggregate):
        for _name, expr in node.group_by:
            out |= set(expr.columns())
        for agg in node.aggregates:
            if agg.of is not None:
                out |= set(agg.of.columns())
    elif isinstance(node, Expand):
        out.add(node.from_column)
    elif isinstance(node, (NodeScan, EdgeScan, Limit, PatternMatch)):
        pass                        # no argument of these reads an input column
    return out


__all__ = ["LiveMap", "live_columns"]
