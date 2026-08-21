"""L13.1's bind-time anchor sets (FRESHNESS_SEMANTICS §13.2).

For a node `n` and an output column `c` of node-identity type, `anchor(n, c)` is
a set of uids known at **plan time** such that, **under every belief state**, the
values of `c` in `⟦n⟧` are contained in it — or ⊤ when no finite such set is
derivable. It is computed bottom-up alongside `scope_of`, from `(op, bound_args,
input anchors)`.

> **Do not paraphrase this as "narrow when the upstream scope is narrow".** That
> phrasing is unsound. The counterexample is
> `EdgeScan(endpoints={role: "dst", uids: [$p]})`: it carries the narrow scope
> `{incident: {dst, [$p]}}`, yet its **`src`** column ranges over every account
> that has written to `$p`, so an `Expand{exact(1)}` from `src` has `anchor = ⊤`
> despite a narrow upstream.

The invariant that makes anchors Level 0: **an anchor is a superset of
*achievable* values at every belief state, never the values observed at
`tt_q`** — this function receives no rows.
"""

from __future__ import annotations

from typing import Union

from tgms.core.errors import InvalidArgError
from tgms.tgir.depscope import TOP, _Top
from tgms.tgir.node import (
    Aggregate, EdgeScan, Expand, Filter, Join, Limit, Node, NodeScan, OpaqueLeaf,
    Order, PatternMatch, Project, PropertyPredicate, TypeConstraint,
)

Anchor = Union[_Top, frozenset[str]]
AnchorMap = dict[str, Anchor]


def anchors_of(node: Node) -> AnchorMap:
    """`{column name → anchor}` for every node-identity column the node emits.

    Columns absent from the map are ⊤ by definition; `anchor_of` reads it that
    way so a caller can never mistake a missing key for a narrowing.
    """
    if isinstance(node, NodeScan):
        # `NodeScan(uids=U)` — bind-time bound by R5. With only `labels`, ⊤.
        if node.uids:
            return {f"{node.as_}.uid": frozenset(node.uids)}
        return {}

    if isinstance(node, EdgeScan):
        src, dst = f"{node.as_}.src", f"{node.as_}.dst"
        ep = node.endpoints
        if ep is None:
            return {}
        uids = frozenset(ep.uids)
        if ep.role == "src":
            return {src: uids}
        if ep.role == "dst":
            return {dst: uids}
        if ep.role == "both":
            # the one real narrowing the enum offers
            return {src: uids, dst: uids}
        # "either": neither column is contained in U — the edge qualifies on
        # `src ∈ U` *or* `dst ∈ U`, so the narrowing is never even attempted.
        return {}

    if isinstance(node, Expand):
        # `anchor(into) = ⊤`; inherited columns pass through.
        return dict(anchors_of(node.input))

    if isinstance(node, (Filter, PropertyPredicate, TypeConstraint, Order, Limit)):
        # pass through — they only remove rows
        return dict(anchors_of(node.input))

    if isinstance(node, Project):
        # pass through for columns copied verbatim; ⊤ for computed ones.
        upstream = anchors_of(node.input)
        out: AnchorMap = dict(upstream) if node.keep == "all" else {}
        for name, e in node.bindings:
            cols = e.columns()
            if len(cols) == 1 and _is_verbatim(e) and cols[0] in upstream:
                out[name] = upstream[cols[0]]
        return out

    if isinstance(node, Join):
        # each column keeps its own side's anchor
        out = dict(anchors_of(node.left))
        if node.join_type != "anti":
            out.update(anchors_of(node.right))
        return out

    if isinstance(node, Aggregate):
        # pass through for group keys that are copied columns; ⊤ otherwise
        upstream = anchors_of(node.input)
        out = {}
        for name, e in node.group_by:
            cols = e.columns()
            if len(cols) == 1 and _is_verbatim(e) and cols[0] in upstream:
                out[name] = upstream[cols[0]]
        return out

    if isinstance(node, PatternMatch):
        # `anchor(v)` from `r`; ⊤ for unanchored variables. D13.10a: `sources`
        # is an *input relation*, so this narrowing is resolved-args rather
        # than plan-time — a strictly plan-time implementation substitutes ⊤.
        out = {}
        for s in node.sources:
            up = anchors_of(s.relation)
            col = s.column or _sole_uid_column(s.relation)
            if col is not None and col in up:
                out[f"{s.var}.uid"] = up[col]
        return out

    if isinstance(node, OpaqueLeaf):
        # ⊤, except a column a bind-time uid argument pins. M2.0 takes the
        # conservative half of that rule: ⊤ everywhere. The per-operator
        # pinning rides with M2.3's fifteen derivations.
        return {}

    return {}


def anchor_of(node: Node, column: str) -> Anchor:
    """`anchor(n, c)` — ⊤ unless the table above derives a finite set."""
    return anchors_of(node).get(column, TOP)


def uid_column(node: Node, ref: str) -> str:
    """Resolve what `Expand.from` names to a node-identity **column**.

    §2.3 writes `from: var`, while L13.1's anchor is per column and its stated
    trap is about expanding from `EdgeScan`'s `src` — which is a column of an
    *edge* variable, not a variable with a uid of its own. Both spellings are
    therefore accepted: a bare variable resolves to `<var>.uid`, and a
    node-identity column is taken as written.
    """
    schema = node.out_schema
    if ref in schema and schema.tau_of(ref).base == "uid":
        return ref
    candidate = f"{ref}.uid"
    if candidate in schema:
        return candidate
    raise InvalidArgError(f"not a node-identity column or variable: {ref!r}",
                          bound=list(schema.names))


def anchor_of_var(node: Node, ref: str) -> Anchor:
    """The anchor of what `Expand`'s `from` names — a variable or a column."""
    try:
        column = uid_column(node, ref)
    except InvalidArgError:
        return TOP
    return anchor_of(node, column)


def _is_verbatim(expr: object) -> bool:
    from tgms.tgir.expr import Col
    return isinstance(expr, Col)


def _sole_uid_column(relation: Node) -> str | None:
    cols = [c.name for c in relation.out_schema if c.tau.base == "uid"]
    return cols[0] if len(cols) == 1 else None


__all__ = ["Anchor", "AnchorMap", "anchor_of", "anchor_of_var", "anchors_of", "uid_column"]
