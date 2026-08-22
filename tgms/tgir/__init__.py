"""TGIR — the Temporal Graph Intermediate Representation (M2, `TGIR_SPEC.md`).

A **new top-level package**, sibling to `tgms/temporal/` and `tgms/agent/` and
nested inside neither, for a reason that is checkable rather than aesthetic:
`tgms/temporal/algebra.py` already imports `tgms.storage.base` and is imported
by `tgms.tools.server` and `tgms.agent.*`, while TGIR must be importable by all
three *and* must import the registry. So it sits above `temporal` and below
`tools`/`agent`; putting it inside `temporal/` would create the import cycle
`algebra → tgir → algebra`.

**What is wired, as of M2.2.** `call_operator` builds a single-leaf plan for
every operator call and evaluates it here, so `leaf`, `evaluate`, `guard`,
`rollout` and `ttq` are on the live path; `node`, `types`, `expr`, `metadata`,
`depscope`, `anchor`, `scope_of` and `plan` support them. **No operator kernel
is touched** (M2 rule 1.3) — the leaf delegates to `REGISTRY[op].fn` with the
same arguments, which is why every `result_digest` in the tree is unchanged.
`TGIR_PLAN_PATH=off` restores the pre-M2.2 direct call.

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
- `leaf` / `evaluate` / `plan` — M2.2's opaque-leaf path: Σ per §5.2, the
  kernel delegation, the `R` tuple, and the single-node plan every call becomes.
- `rollout` — the `TGIR_PLAN_PATH` escape hatch, and M2.4's `COMPILE_MODE`.
- `ttq` — M2.1's read basis: the frontier capture, §6.2's clamp table, and the
  four envelope keys `tt_q` / `pinned` / `clamped` / `dependency`.

Still to come: `leaves.py` (M2.3's fifteen per-operator scope derivations) and
`compiled/` (M2.4's core expansions, all default-off).
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
#: `evaluate` and `scope_of` are **modules**, and the functions of the same
#: name inside them are deliberately *not* re-exported here: binding the
#: function to the package attribute shadows the submodule, so
#: `from tgms.tgir import evaluate` would hand back a function and
#: `tgms.tgir.evaluate.evaluate_leaf` would raise `AttributeError`. Import them
#: from their module (`from tgms.tgir.evaluate import evaluate`).
from tgms.tgir.evaluate import (
    Evaluation, evaluate_leaf, leaf_completeness, leaf_meta, leaf_provenance,
    meta_for, meta_json,
)
from tgms.tgir.leaf import LEAF_VT_MODE, build_leaf, sigma_for
from tgms.tgir.plan import Plan
from tgms.tgir.rollout import COMPILE_MODE, PLAN_PATH_ENV, compile_mode, plan_path_enabled
from tgms.tgir.scope_of import ScopeBasis, leaf_scope
from tgms.tgir.ttq import (
    Frontier, TtQ, as_of_tt_of, basis_of, checkpoints_of, clamp, dependency_of,
    envelope_metadata, frontier_of, store_identity_of,
)
from tgms.tgir.types import (
    BELIEF_MODES, DEFAULT_SIGMA, SCALAR_TAUS, VT_MODES, Column, Schema, Sigma, Tau,
    edge_schema, node_schema,
)

__all__ = [
    "adapter_for", "Agg", "AGG_FNS", "Aggregate", "Anchor", "anchor_of", "anchor_of_var",
    "anchors_of", "Arith", "as_of_tt_of", "basis_of", "BELIEF_MODES", "BoolOp", "Bounded",
    "build_leaf", "CARVE_PROPS", "Cast", "CERTIFICATION_LAYER", "Checkpoint",
    "checkpoints_of", "clamp", "Cmp", "Coalesce", "Col", "Column", "comparable",
    "COMPILE_MODE", "compile_mode", "Completeness", "compute_node_digest",
    "CORE_NODE_TYPES", "DEFAULT_SIGMA", "dependency_of", "DependencyScope", "DIRECTIONS",
    "edge_schema", "EdgeKey", "EdgePat", "EdgeScan", "EMPTY_SCOPE_OPS", "Endpoints",
    "envelope_metadata", "evaluate_leaf", "Evaluation", "Exact", "Exactness",
    "Expand", "Expr", "Filter", "Frontier", "frontier_of", "FULL_SCAN_CHECKPOINTS",
    "HopSpec", "If", "Incident", "INCIDENT_ROLES", "is_empty_scope_op", "IsNull", "Join",
    "JOIN_TYPES", "K_DENSE_ID", "K_EDGE", "K_NODE", "KINDS", "le", "leaf_completeness",
    "leaf_meta", "leaf_provenance", "leaf_scope", "LEAF_VT_MODE", "Limit", "Lit", "MathFn",
    "meet", "meet_all", "meet_exactness", "meta_for", "meta_json", "MIDDLE", "Node",
    "node_schema", "NodePat", "NodeScan", "Not", "NullAdapter", "OpaqueLeaf", "Order",
    "Pattern", "PatternMatch", "Plan", "plan_path_enabled", "PLAN_PATH_ENV", "Project",
    "PropertyPredicate", "PropRef", "Provenance", "PSEUDO_PROPS", "ResultMeta",
    "SCALAR_TAUS", "ScanDescriptor", "Schema", "SCHEMA_NAME", "SCHEMA_VERSION",
    "ScopeBasis", "ScopeTerm", "Sigma", "sigma_for", "SortKey", "Source", "store_identity",
    "store_identity_of", "STORE_READING_CORE", "Targets", "Tau", "TOP", "TOP_TERM", "TtQ",
    "TupleExpr", "TypeConstraint", "UNANCHORED", "Unbounded", "union_all", "VidSet",
    "vt_carve", "vt_closed", "vt_from", "VT_MODES"
]
