"""M2.3's gate: the two `targets`-shape obligations of TGIR_SPEC §2.0.

Both are failures of a term's **second** conjunct while the first looks right,
which is why they need a machine rather than a reviewer:

1. **A node-column binder carries a `nodes` arm.** A write to a node emits a
   *node* footprint, and `targets_match` routes node footprints to the `nodes`
   arm **only** — an `incident` arm is an edge-side test and never fires for
   them. So an operator whose output binds node-version columns (a label, a
   node's props, a node `vid`) and whose scope carries edge-only targets is
   silently unsound, not merely imprecise.

2. **`𝒟` in `kinds` obliges an incident arm.** `𝒟 = {assert_edge,
   ingest_events}` registers a dense entity id without necessarily writing a
   node version, which flips a uid-scoped operator's *outcome*. But both
   members write **edge** footprints, so a term naming only a `nodes` arm
   admits `𝒟` in its first conjunct and can never satisfy its second: `𝒟`'s
   presence would be **inert**. The operative member is `assert_edge`, whose
   footprints are edge-shaped and nothing else; `ingest_events` also emits an
   unconditional *node* arm (D13.22), so it is not inert on its own.

Checked over **every** emitted scope, for every operator, across argument
shapes that change the derivation (`include_edges`, a `label` dimension, an
`endpoint_filter`, `of: "duration"`). Operators still on the coarse `"*"`
discharge both obligations trivially, and are checked anyway so that a future
derivation cannot land unexamined.

    uv run python scripts/check_scope_shape.py
"""

from __future__ import annotations

import sys
from typing import Any

from tgms.tgir.depscope import TOP, Checkpoint, DependencyScope, ScopeTerm
from tgms.tgir.leaf import sigma_for
from tgms.tgir.leaves import BINDS_NODE_VERSIONS, LEAF_SCOPES, terms_for
from tgms.temporal.algebra import REGISTRY, ensure_all_registered, validate_args

FAILURES: list[str] = []
W = {"t_a": 0, "t_b": 100}
BASIS = (Checkpoint(0, "seed"),)

#: Argument shapes per operator. More than one where the derivation branches —
#: those branches are the whole point of the check.
SHAPES: dict[str, list[dict[str, Any]]] = {
    "entity_history": [{"uid": "u1"}, {"uid": "u1", "include_edges": True}],
    "neighborhood_evolution": [{"uid": "u1", "t1": 10, "t2": 20}],
    "aggregate_events": [
        {"group_by": [], "aggregates": [{"agg": "count"}], "window": W},
        {"group_by": [{"dim": "label", "role": "src"}],
         "aggregates": [{"agg": "count"}], "window": W},
        {"group_by": [{"dim": "endpoint", "role": "src"}],
         "aggregates": [{"agg": "count"}], "window": W, "rel_types": ["R"],
         "endpoint_filter": {"role": "src", "uids": ["u1", "u2"]}},
        {"group_by": [], "aggregates": [{"agg": "max", "of": "duration"}], "window": W},
        {"group_by": [{"dim": "label", "role": "dst"}],
         "aggregates": [{"agg": "mean", "of": "duration"}], "window": W,
         "endpoint_filter": {"role": "either", "uids": []}},
    ],
    "version_history": [{"kind": "node", "window": W}, {"kind": "edge", "window": W}],
    "snapshot_subgraph": [{"seeds": ["u1"], "t_valid": 10}],
    "diff_snapshots": [{"t1": 10, "t2": 20}],
    "resolve_entities": [{"query": "u1"}],
    "graph_metric_timeseries": [{"metric": "edge_event_count", "window": W, "stride": 10}],
    "burst_detection": [{"target": {"kind": "edge_event_rate"}, "window": W, "stride": 10}],
    "count_temporal_motifs": [{"motif": "M_2node_pingpong", "delta": 5, "window": W}],
    "find_temporal_motif_instances": [{"motif": "M_2node_pingpong", "delta": 5,
                                       "window": W}],
    "temporal_reachability": [{"src": "u1", "window": W}],
    "temporal_paths": [{"src": "u1", "dst": "u2", "window": W}],
    "co_active": [{"a_spec": {"src": "u1"}, "b_spec": {"src": "u2"},
                   "allen_relation": {"relation": "overlaps"}}],
    "compute": [{"fn": "count", "input": [{"x": 1}]}],
}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def has_nodes_arm(term: ScopeTerm) -> bool:
    """Can this term match a *node* footprint at all?"""
    return term.targets is TOP or getattr(term.targets, "nodes", None) is not None


def has_edge_side_arm(term: ScopeTerm) -> bool:
    """Can this term match an *edge* footprint at all?"""
    if term.targets is TOP:
        return True
    return (getattr(term.targets, "edges", None) is not None
            or getattr(term.targets, "incident", None) is not None)


def admits(term: ScopeTerm, kind: str) -> bool:
    return term.kinds is TOP or kind in term.kinds


def scope_for(op: str, args: dict[str, Any]) -> DependencyScope:
    filled = validate_args(op, dict(args))
    terms = terms_for(op, filled, sigma_for(op, filled))
    return DependencyScope("shape-check", 0, terms, BASIS)


def main() -> int:
    ensure_all_registered()
    missing = sorted(set(REGISTRY) - set(SHAPES))
    check("every operator has at least one argument shape", not missing, str(missing))

    for op in sorted(SHAPES):
        binds_nodes = BINDS_NODE_VERSIONS.get(op)
        for i, args in enumerate(SHAPES[op]):
            scope = scope_for(op, args)
            tag = f"{op}[{i}]"
            if not scope.terms:
                # ∅ — `compute` reads nothing, so neither obligation applies
                check(f"{tag}: an empty scope carries no targets obligation",
                      op == "compute", "only compute may be ∅ in M2")
                continue

            # obligation 1 — checked over the scope, since `terms` is a
            # disjunction and the node arm may live in its own term (FF-3)
            if binds_nodes is not None and binds_nodes(validate_args(op, dict(args))):
                check(f"{tag}: binds node versions ⇒ some term carries a nodes arm",
                      any(has_nodes_arm(t) for t in scope.terms),
                      "node footprints route to the nodes arm only")

            # obligation 2 — per term: `assert_edge`'s footprints are
            # edge-shaped and nothing else, so admitting it beside a
            # nodes-only target is inert
            for j, term in enumerate(scope.terms):
                if admits(term, "assert_edge") and has_nodes_arm(term):
                    check(f"{tag}.term{j}: admits assert_edge ⇒ carries an edge-side arm",
                          has_edge_side_arm(term),
                          "otherwise 𝒟's presence in kinds is inert (L13.3)")

            # a term that can match nothing at all is a derivation bug, not a
            # precision choice: D13.5's `[]` reads as "no member matches"
            for j, term in enumerate(scope.terms):
                vacuous = (term.kinds is not TOP and not term.kinds) or \
                    (term.props is not TOP and not term.props) or \
                    (term.targets is not TOP
                     and not any((getattr(term.targets, a, None) not in (None, ()))
                                 for a in ("nodes", "edges", "incident")))
                check(f"{tag}.term{j}: is not vacuous", not vacuous,
                      "an empty component matches nothing (D13.5)")

    derived = sorted(LEAF_SCOPES)
    print(f"\nderived: {derived}")
    print(f"coarse '*': {sorted(set(REGISTRY) - set(LEAF_SCOPES) - {'compute'})}")
    if FAILURES:
        print(f"\n{len(FAILURES)} shape obligation(s) violated:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nevery emitted scope satisfies both §2.0 targets-shape obligations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
