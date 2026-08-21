"""TGIR — the Temporal Graph Intermediate Representation (M2, `TGIR_SPEC.md`).

A **new top-level package**, sibling to `tgms/temporal/` and `tgms/agent/` and
nested inside neither, for a reason that is checkable rather than aesthetic:
`tgms/temporal/algebra.py` already imports `tgms.storage.base` and is imported
by `tgms.tools.server` and `tgms.agent.*`, while TGIR must be importable by all
three *and* must import the registry. So it sits above `temporal` and below
`tools`/`agent`; putting it inside `temporal/` would create the import cycle
`algebra → tgir → algebra`.

**Phase M2.0 is data structures only.** Nothing in this package is wired into
the live call path: `call_operator` does not import it, no operator kernel is
touched, and the four existing suites do not import it either. That zero blast
radius *is* the deliverable — the rollback for this phase is deleting the
directory.

What is here:

- `types` — the §4.1 type lattice, `Column`/`Schema`, and `Sigma = (T_v, T_b)`.
- `expr` — §2.7's row-local expression language (the formal content of R2).
- `node` — the twelve compositional core operators of §2 and the `OpaqueLeaf`
  that carries the fifteen existing operators (R7), with §4.2's schema
  propagation and the static plan errors.
- `metadata` — §5.2.1's completeness lattice with its meet, `Exactness`,
  §5.4's provenance, and the `R` tuple minus value.
- `depscope` — FRESHNESS_SEMANTICS D13.2's wire object, its canonical JSON, and
  `⊎`.
- `anchor` — L13.1's bind-time anchor table.
- `scope_of` — D13.10's `leaf_scope ⊎ ⊎ ins`.
- `guard` — the `∅`-kernel `NullAdapter`, which turns §2.0's classification into
  a checkable property.
- `ttq` — M2.1's read basis: the frontier capture, §6.2's clamp table, and the
  four envelope keys `tt_q` / `pinned` / `clamped` / `dependency`. This is the
  one module the live call path uses, through a local import in
  `call_operator`; everything else is still unwired.

Still to come: `leaves.py` (M2.3's fifteen derivations), `plan.py`/`evaluate.py`
(M2.2), `compiled/` and `rollout.py` (M2.4).
"""

from tgms.tgir.anchor import Anchor, anchor_of, anchor_of_var, anchors_of
from tgms.tgir.depscope import (
    CARVE_PROPS, FULL_SCAN_CHECKPOINTS, INCIDENT_ROLES, KINDS, K_DENSE_ID, K_EDGE,
    K_NODE, PSEUDO_PROPS, SCHEMA_NAME, SCHEMA_VERSION, TOP, TOP_TERM, UNANCHORED,
    Checkpoint, DependencyScope, EdgeKey, Incident, ScopeTerm, Targets,
    store_identity, union_all, vt_carve, vt_closed, vt_from,
)
from tgms.tgir.expr import (
    Arith, BoolOp, Cast, Cmp, Coalesce, Col, Expr, If, IsNull, Lit, MathFn, Not,
    PropRef, TupleExpr,
)
from tgms.tgir.guard import NullAdapter, adapter_for, is_empty_scope_op
from tgms.tgir.metadata import (
    CERTIFICATION_LAYER, MIDDLE, Completeness, Exactness, Provenance, ResultMeta,
    ScanDescriptor, VidSet, comparable, compute_node_digest, le, meet, meet_all,
    meet_exactness,
)
from tgms.tgir.node import (
    AGG_FNS, CORE_NODE_TYPES, DIRECTIONS, EMPTY_SCOPE_OPS, JOIN_TYPES,
    STORE_READING_CORE, Agg, Aggregate, Bounded, EdgePat, EdgeScan, Endpoints,
    Exact, Expand, Filter, HopSpec, Join, Limit, Node, NodePat, NodeScan,
    OpaqueLeaf, Order, Pattern, PatternMatch, Project, PropertyPredicate, SortKey,
    Source, TypeConstraint, Unbounded,
)
from tgms.tgir.scope_of import ScopeBasis, leaf_scope, scope_of
from tgms.tgir.ttq import (
    Frontier, TtQ, as_of_tt_of, basis_of, checkpoints_of, clamp, dependency_of,
    envelope_metadata, frontier_of, store_identity_of,
)
from tgms.tgir.types import (
    BELIEF_MODES, DEFAULT_SIGMA, SCALAR_TAUS, VT_MODES, Column, Schema, Sigma, Tau,
    edge_schema, node_schema,
)

__all__ = [
    "AGG_FNS", "Agg", "Aggregate", "Anchor", "Arith", "BELIEF_MODES", "BoolOp",
    "Bounded", "CARVE_PROPS", "CERTIFICATION_LAYER", "CORE_NODE_TYPES", "Cast",
    "Checkpoint", "Cmp", "Coalesce", "Col", "Column", "Completeness",
    "DEFAULT_SIGMA", "DIRECTIONS", "DependencyScope", "EMPTY_SCOPE_OPS",
    "EdgeKey", "EdgePat", "EdgeScan", "Endpoints", "Exact", "Exactness",
    "Expand", "Expr", "FULL_SCAN_CHECKPOINTS", "Filter", "Frontier", "HopSpec",
    "INCIDENT_ROLES", "TtQ", "as_of_tt_of", "basis_of", "checkpoints_of", "clamp",
    "dependency_of", "envelope_metadata", "frontier_of", "store_identity_of",
    "If", "Incident", "IsNull", "JOIN_TYPES", "Join", "KINDS", "K_DENSE_ID",
    "K_EDGE", "K_NODE", "Limit", "Lit", "MIDDLE", "MathFn", "Node", "NodePat",
    "NodeScan", "Not", "NullAdapter", "OpaqueLeaf", "Order", "PSEUDO_PROPS",
    "Pattern", "PatternMatch", "Project", "PropRef", "PropertyPredicate",
    "Provenance", "ResultMeta", "SCALAR_TAUS", "SCHEMA_NAME", "SCHEMA_VERSION",
    "STORE_READING_CORE", "ScanDescriptor", "Schema", "ScopeBasis", "ScopeTerm",
    "Sigma", "SortKey", "Source", "TOP", "TOP_TERM", "Targets", "Tau",
    "TupleExpr", "TypeConstraint", "UNANCHORED", "Unbounded", "VT_MODES",
    "VidSet", "adapter_for", "anchor_of", "anchor_of_var", "anchors_of",
    "comparable", "compute_node_digest", "edge_schema", "is_empty_scope_op",
    "le", "leaf_scope", "meet", "meet_all", "meet_exactness", "node_schema",
    "scope_of", "store_identity", "union_all", "vt_carve", "vt_closed", "vt_from",
]
