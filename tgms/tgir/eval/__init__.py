"""The core evaluators — one per node type, evaluated as written (§3.7).

**No plan rewriting whatsoever.** Not for purity: a rewrite that changes a
node's bound arguments makes its dependency scope wrong (D13.14 prohibition 2 —
"a node's scope is derived from its **recorded bound args**, never from
hypothetical or re-resolved ones"), and §3.5's ruling that filters narrow the
declared domain and *never* the scope means a pushed-down predicate would
silently narrow a scope that must not narrow. The freshness contract makes one
class of optimization unsound, so this evaluator does none of it. Column
pruning (`tgms/tgir/prune.py`) is the one plan-time pass, and it changes no
argument, no row and no order.

**Σ reaches exactly the `reads_store` nodes.** Every other node is evaluated
with a `NullAdapter`, so §2.0's `∅` classification is a runtime-checkable
property for the core exactly as M2 made it for `compute`: a pure evaluator that
started reading store state would raise by name at its first access instead of
rotting into silent unsoundness.

M3.0 implements the scans, the three selections, `Project`, `Order`, `Limit` and
`Join{inner}`. `Expand` (M3.1), `PatternMatch`, `Aggregate` and the two
absence-deriving joins (M3.2) still raise `NotImplementedError`, now naming the
phase that owns each rather than the whole core.
"""

from __future__ import annotations

from typing import Any

from tgms.tgir.eval.adjacency import AdjacencyCache
from tgms.tgir.eval.expand import eval_expand
from tgms.tgir.eval.expr_eval import eval_expr, eval_predicate
from tgms.tgir.eval.join import eval_join
from tgms.tgir.eval.order import eval_limit, eval_order, limit_truncated
from tgms.tgir.eval.scan import scan_edges, scan_nodes
from tgms.tgir.eval.select import (
    eval_filter, eval_project, eval_property_predicate, eval_type_constraint,
)
from tgms.tgir.guard import adapter_for
from tgms.tgir.node import (
    EdgeScan, Expand, Filter, Join, Limit, Node, NodeScan, Order, Project,
    PropertyPredicate, TypeConstraint,
)
from tgms.tgir.prune import LiveMap, live_columns
from tgms.tgir.relation import Relation

#: Which phase owns each not-yet-built node kind, so the error names the seam.
PENDING: dict[str, str] = {
    "PatternMatch": "M3.2",
    "Aggregate": "M3.2",
}


class Execution:
    """One plan execution: the memo, the pruning map, and the side metadata a
    node produces beyond its rows.

    The memo is keyed by `node_digest`, so a DAG that reaches one subtree twice
    evaluates it **once** — which is also what makes the shared-subtree case
    cheap enough that a plan author never has to think about it.
    """

    def __init__(self, adapter: Any, live: LiveMap | None = None, *,
                 stats: dict[str, Any] | None = None, plan_digest: str = "",
                 ceilings: dict[str, int] | None = None,
                 budget: Any = None) -> None:
        self.adapter = adapter
        self.live = live or {}
        self.memo: dict[str, Relation] = {}
        #: Adjacency indexes, shared across every `Expand` in one execution —
        #: a multi-hop chain builds its index once, not once per hop.
        self.adjacency = AdjacencyCache(adapter)
        #: Stage-2 admission inputs. With no `stats` the re-check is skipped,
        #: which is what a bare `evaluate_core` outside a plan wants.
        self.stats = stats
        self.plan_digest = plan_digest
        self.ceilings = ceilings
        self.budget = budget
        #: `prop_coercion` counts per node digest (§2.5) — the disclosed
        #: denominator, which rides out on the result metadata.
        self.coercion: dict[str, dict[str, Any]] = {}

    def run(self, node: Node) -> Relation:
        key = node.node_digest
        cached = self.memo.get(key)
        if cached is not None:
            return cached
        inputs = tuple(self.run(i) for i in node.inputs)
        if self.stats is not None:
            # stage 2: the same estimate against the inputs' *realized*
            # cardinality, immediately before this node runs. It can only ever
            # refuse more than stage 1 did (§9.7's ruling).
            from tgms.tgir.admission import admit_node
            admit_node(node, self.stats, self.plan_digest,
                       max((i.n for i in inputs), default=0), self.ceilings)
        out = self.apply(node, inputs)
        self.memo[key] = out
        return out

    def apply(self, node: Node, inputs: tuple[Relation, ...]) -> Relation:
        """One node, over already-evaluated inputs."""
        # the ∅ guard, live for the core: only the four `reads_store` node kinds
        # ever see the real adapter
        adapter = adapter_for(node, self.adapter)
        live = self.live.get(node.node_digest)

        if isinstance(node, NodeScan):
            return scan_nodes(node, adapter, live)
        if isinstance(node, EdgeScan):
            return scan_edges(node, adapter, live)
        if isinstance(node, Filter):
            return eval_filter(node, inputs[0])
        if isinstance(node, PropertyPredicate):
            out, coercion = eval_property_predicate(node, inputs[0])
            self.coercion[node.node_digest] = coercion
            return out
        if isinstance(node, TypeConstraint):
            return eval_type_constraint(node, inputs[0])
        if isinstance(node, Project):
            return eval_project(node, inputs[0])
        if isinstance(node, Order):
            return eval_order(node, inputs[0])
        if isinstance(node, Limit):
            return eval_limit(node, inputs[0])
        if isinstance(node, Expand):
            return eval_expand(node, inputs[0], adapter, self.adjacency, live,
                               self.budget)
        if isinstance(node, Join):
            return eval_join(node, inputs[0], inputs[1])
        raise NotImplementedError(
            f"{node.op} has no evaluator yet — it is "
            f"{PENDING.get(node.op, 'a later phase')}'s "
            f"(docs/design/M3_IMPLEMENTATION_PLAN.md §4.1)")


def evaluate_core(node: Node, adapter: Any, *, admit_plan: bool = False,
                  ceilings: dict[str, int] | None = None) -> Relation:
    """Evaluate a compositional plan rooted at `node`.

    Column pruning runs once, before execution, over the whole reachable plan.
    With `admit_plan`, §2.13's plan-level admission runs first and the two
    later refusal points (stage 2 and the runtime budget) are armed — which is
    what `run_plan` will pass in M3.4. It is **off** by default so that
    evaluating a node in a test or a receipt is not also a policy decision.
    """
    from tgms.tgir.admission import Budget, admit

    stats = None
    budget = None
    plan_digest = node.node_digest
    if admit_plan:
        stats = adapter.stats()
        admit(node, stats, plan_digest, ceilings)
        budget = Budget(plan_digest)
    return Execution(adapter, live_columns(node), stats=stats,
                     plan_digest=plan_digest, ceilings=ceilings,
                     budget=budget).run(node)


__all__ = [
    "AdjacencyCache", "Execution", "PENDING", "eval_expand", "eval_expr",
    "eval_filter", "eval_join", "eval_limit", "eval_order", "eval_predicate",
    "eval_project", "eval_property_predicate", "eval_type_constraint",
    "evaluate_core", "limit_truncated", "scan_edges", "scan_nodes",
]
