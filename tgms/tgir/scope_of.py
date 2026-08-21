"""`scope_of` — the compositionality interface (FRESHNESS_SEMANTICS D13.10).

```
scope_of : (op, bound_args, Σ, [input_scope]) → DependencyScope
scope_of(op, args, Σ, ins)  =  leaf_scope(op, args, Σ, out_schema)  ⊎  ⊎ ins
```

Union is list concatenation (D13.8); normalization is optional and always
widening, and this implementation skips it. Three prohibitions carried verbatim
from D13.14, because a plan-level implementation must not shortcut them:

1. **Every node's scope enters the union — including nodes whose rows never
   reach the answer.** This is not merely about coverage: it is load-bearing for
   the soundness of `Expand{exact(1)}`'s narrowing (L13.2), which is sound
   *because* the seed-supplying node's scope is unioned in. **A seeds-only input
   is never scope-elided.**
2. **A node's scope is derived from its recorded bound args**, never from
   hypothetical or re-resolved ones.
3. **A node that failed or was refused still contributes its scope** — a
   correction can make it succeed. This is the opposite of how `completeness`
   behaves, and it is deliberate.

M2.0 implements the four core store-reading nodes (D13.15) and gives every
opaque leaf the coarse `"*"` term — explicitly legal under §5.5.4 constraint 1
("`\"*\"` everywhere is a valid v1 answer for any operator whose derivation is
not yet written"), with `compute`'s `∅` as the one exception from day one. The
fifteen per-operator derivations are M2.3 (`leaves.py`), and D13.1 makes every
later narrowing a strict improvement rather than a compatibility event.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgms.tgir.anchor import anchor_of_var, anchors_of
from tgms.tgir.depscope import (
    FULL_SCAN_CHECKPOINTS, K_DENSE_ID, K_EDGE, K_NODE, TOP, TOP_TERM, Checkpoint,
    DependencyScope, Incident, ScopeTerm, Targets, _Top,
)
from tgms.tgir.node import (
    Bounded, EdgeScan, Exact, Expand, Node, NodeScan, OpaqueLeaf, PatternMatch,
)


@dataclass(frozen=True, slots=True)
class ScopeBasis:
    """The read-time half of D13.2 that `scope_of` cannot derive from the plan:
    the store identity, the belief frontier the read was served from, and the
    log cursor. `scope_of` is plan-time; the basis is stamped per read (§5.6)."""

    store: str
    tt_q: int
    pinned: bool = False
    clamped: bool = False
    checkpoints: tuple[Checkpoint, ...] = FULL_SCAN_CHECKPOINTS

    def scope(self, *terms: ScopeTerm) -> DependencyScope:
        return DependencyScope(self.store, self.tt_q, tuple(terms), self.checkpoints,
                               self.pinned, self.clamped)


def _union_kinds(*groups: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for g in groups:
        for k in g:
            if k not in out:
                out.append(k)
    return tuple(out)


def leaf_scope(node: Node, basis: ScopeBasis) -> DependencyScope:
    """The node's own scope, before its inputs' are unioned in.

    `∅` for every node that is a pure function of its inputs and bind-time
    arguments — §2.0's obligation-6 classification, which is `Node.reads_store`.
    """
    if isinstance(node, NodeScan):
        # D13.15: `kinds = 𝒩 ∪ 𝒟`. The `incident` arm is the `𝒟` reach
        # (L13.3): `assert_edge`/`ingest_events` register a dense id without
        # writing a node version and thereby flip the consumer's *outcome*, but
        # both write **edge** footprints, which route to the edges/incident arms
        # only — so a term naming a `nodes` arm alone admits `𝒟` in its first
        # conjunct and can never satisfy its second.
        targets: _Top | Targets = TOP
        if node.uids:
            uids = tuple(node.uids)
            targets = Targets(nodes=uids, incident=Incident("either", uids))
        # `props` is `"*"`: §2.1 emits the whole `props` bag and `vt_e` on every
        # row, so `@recut` is unconditional and there is no Level-0 escape by
        # projection (FF-1).
        return basis.scope(ScopeTerm(
            kinds=_union_kinds(K_NODE, K_DENSE_ID), targets=targets, rel_types=TOP,
            vt=tuple((i.start, i.end) for i in node.sigma.t_v),
            vt_mode=node.vt_mode, props=TOP))

    if isinstance(node, EdgeScan):
        ep = node.endpoints
        targets = (Targets(incident=Incident(ep.role, tuple(ep.uids))) if ep is not None
                   else Targets(edges=TOP))
        return basis.scope(ScopeTerm(
            kinds=K_EDGE, targets=targets,
            rel_types=tuple(node.rel_types) if node.rel_types else TOP,
            vt=tuple((i.start, i.end) for i in node.sigma.t_v),
            vt_mode=node.vt_mode, props=TOP))

    if isinstance(node, Expand):
        return basis.scope(_expand_term(node))

    if isinstance(node, PatternMatch):
        rel_types: tuple[str, ...] | _Top = TOP
        declared = [e.rel_type for e in node.pattern.edge_pats]
        if all(r is not None for r in declared):
            rel_types = tuple(dict.fromkeys(r for r in declared if r is not None))
        # "`\"*\"` unless **every** node variable is anchored to a bound
        # relation" (D13.15). The anchors come from the `PatternMatch`'s own
        # bottom-up map, which resolves each source's identity column.
        node_vars = [p.var for p in node.pattern.node_pats]
        bound = anchors_of(node)
        targets = TOP
        if node_vars and all(f"{v}.uid" in bound for v in node_vars):
            uids: list[str] = []
            for v in node_vars:
                a = bound[f"{v}.uid"]
                if a is TOP:
                    uids = []
                    break
                uids.extend(sorted(a))  # type: ignore[arg-type]
            if uids:
                # It binds node columns, so L13.2a applies: the `incident` arm
                # comes **in addition to** `nodes: "*"`, never instead of it.
                targets = Targets(nodes=TOP,
                                  incident=Incident("either", tuple(dict.fromkeys(uids))))
        return basis.scope(ScopeTerm(
            kinds=_union_kinds(K_EDGE, K_NODE), targets=targets, rel_types=rel_types,
            vt=tuple((i.start, i.end) for i in node.sigma.t_v),
            vt_mode="overlap", props=TOP))

    if isinstance(node, OpaqueLeaf):
        if not node.reads_store:
            # `compute` — `terms: []`, the empty scope ∅ (§6 #15, D13.2).
            return DependencyScope.empty(basis.store, basis.tt_q,
                                         checkpoints=basis.checkpoints,
                                         pinned=basis.pinned, clamped=basis.clamped)
        return basis.scope(TOP_TERM)

    # `Filter`, `PropertyPredicate`, `TypeConstraint`, `Project`, `Join`,
    # `Aggregate`, `Order`, `Limit` — `∅` (D13.11). A filter narrows the
    # declared *domain* and never the scope (D13.12), and the rule is
    # self-enforcing: every selection operator is `∅`-scoped and scopes only
    # ever union, so no code path exists in which a predicate could narrow one.
    return DependencyScope.empty(basis.store, basis.tt_q, checkpoints=basis.checkpoints,
                                 pinned=basis.pinned, clamped=basis.clamped)


def _expand_term(node: Expand) -> ScopeTerm:
    """D13.15's `Expand` row.

    `"*"` at every *multi-hop* bound: a set of new edges can form an entirely
    new path from a seed, so no static endpoint set is sound for `exact(k≥2)`,
    `bounded` or `unbounded` — the concrete cost of R6, and the highest-value
    Level-1 refinement target.
    """
    vt = tuple((i.start, i.end) for i in node.sigma.t_v)
    kinds = _union_kinds(K_EDGE, K_NODE)
    rel_types: tuple[str, ...] | _Top = (node.rel_type,) if node.rel_type else TOP
    anchor = anchor_of_var(node.input, node.from_)
    targets: _Top | Targets = TOP

    if isinstance(node.hops, Exact) and anchor is not TOP:
        seeds = tuple(sorted(anchor))  # type: ignore[arg-type]
        if node.hops.k == 0:
            # A *node* arm, since it traverses no edge (L13.2a). The `𝒟` arm
            # L13.3 would otherwise require is dropped here under L13.3's own
            # exemption: a core scan has no unknown-uid outcome to flip.
            targets = Targets(nodes=seeds)
        elif node.hops.k == 1:
            # L13.2: the edge arm narrows, **and only** the edge arm. `exact(1)`
            # binds `into`'s node columns, so a node write on a *current*
            # neighbour of a seed changes an output row — and node footprints
            # test against the `nodes` arm only, which is why it stays `"*"`.
            role = {"out": "src", "in": "dst", "both": "either"}[node.dir]
            targets = Targets(nodes=TOP, incident=Incident(role, seeds))
    if isinstance(node.hops, Bounded) and node.hops.a == node.hops.b == 0:
        # `bounded(0, 0)` still traverses no edge, but it is a *node* relation
        # and is deliberately not normalized to `exact(0)` (§2.3); its targets
        # stay `"*"` rather than being narrowed by the `exact(0)` rule.
        targets = TOP
    return ScopeTerm(kinds=kinds, targets=targets, rel_types=rel_types, vt=vt,
                     vt_mode="overlap", props=TOP)


def scope_of(node: Node, basis: ScopeBasis,
             input_scopes: tuple[DependencyScope, ...] | None = None) -> DependencyScope:
    """`leaf_scope(node) ⊎ ⊎ ins`.

    `input_scopes` defaults to recursing over `node.inputs`, which is the
    bottom-up derivation D13.10 asks for. Passing them explicitly is what a plan
    walker does when it has already computed each step's scope — and it must
    pass **every** input's, including seeds-only ones (prohibition 1).
    """
    ins = (input_scopes if input_scopes is not None
           else tuple(scope_of(i, basis) for i in node.inputs))
    out = leaf_scope(node, basis)
    for s in ins:
        out = out.union(s)
    return out


__all__ = ["ScopeBasis", "leaf_scope", "scope_of"]
