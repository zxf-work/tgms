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

import os
from time import perf_counter
from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.tgir.eval.adjacency import AdjacencyCache
from tgms.tgir.eval.aggregate import eval_aggregate
from tgms.tgir.eval.expand import eval_expand
from tgms.tgir.eval.expr_eval import eval_expr, eval_predicate
from tgms.tgir.eval.join import eval_join
from tgms.tgir.eval.order import eval_limit, eval_order, limit_truncated
from tgms.tgir.eval.pattern import eval_pattern, label_filter
from tgms.tgir.eval.scan import ScanCache, scan_edges, scan_nodes
from tgms.tgir.eval.select import (
    eval_filter, eval_project, eval_property_predicate, eval_type_constraint,
)
from tgms.tgir.guard import adapter_for
from tgms.tgir.metadata import ResultMeta
from tgms.tgir.node import (
    Aggregate, EdgeScan, Expand, Filter, Join, Limit, Node, NodeScan, Order,
    PatternMatch, Project, PropertyPredicate, TypeConstraint,
)
from tgms.tgir.propagate import meta_for
from tgms.tgir.prune import LiveMap, live_columns
from tgms.tgir.relation import Relation

#: Which phase owns each not-yet-built node kind, so the error names the seam.
#: Node kinds with no evaluator yet. Empty since M3.2 — every one of §2's
#: twelve compositional operators now evaluates.
PENDING: dict[str, str] = {}

#: P3.1's wall-clock instrumentation is cheap but not free (measured: see
#: `docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §6 P3.1's overhead report).
#: Default-on; set to "0" to skip the `perf_counter` pair around `apply` while
#: still recording `rows_out`/`rows_in`/`route`, which cost nothing extra.
TELEMETRY_TIMING_ENV = "TGMS_PLAN_TELEMETRY_TIMING"


def _timing_enabled() -> bool:
    return os.environ.get(TELEMETRY_TIMING_ENV, "1") != "0"


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
        #: Full columnar node reads, shared across every scan in one execution.
        #: `Expand`/`PatternMatch` resolve `into`'s version columns by going
        #: back through `scan_nodes`, so without this the whole node table is
        #: materialized once per binding node as well as once per `NodeScan`.
        self.scans = ScanCache()
        #: Stage-2 admission inputs. With no `stats` the re-check is skipped,
        #: which is what a bare `evaluate_core` outside a plan wants.
        self.stats = stats
        self.plan_digest = plan_digest
        self.ceilings = ceilings
        self.budget = budget
        #: The reason admission was skipped, or `None` when it ran. Recorded
        #: rather than merely permitted: a bypass is a claim that some other
        #: guard covers this execution, and a claim should be inspectable.
        self.admission_bypass: str | None = None
        #: `prop_coercion` counts per node digest (§2.5) — the disclosed
        #: denominator, which rides out on the result metadata.
        self.coercion: dict[str, dict[str, Any]] = {}
        #: Per-node side annotations, keyed by node_digest, namespaced by lane.
        #: "telemetry" (P3.1) and "scan_region" (P1.3) are the two keys in v1; a lane
        #: reads only its own. Digest-excluded — it rides in the `tgir` sub-object.
        self.annotations: dict[str, dict[str, Any]] = {}
        #: §5's `R` minus value, **per node** — "at every node, not only at the
        #: plan's root". Computed *before* the node runs, so a precondition
        #: refusal costs no work and leaves no partial relation.
        self.meta: dict[str, ResultMeta] = {}

    def run(self, node: Node) -> Relation:
        key = node.node_digest
        cached = self.memo.get(key)
        if cached is not None:
            return cached
        inputs = tuple(self.run(i) for i in node.inputs)
        input_meta = tuple(self.meta[i.node_digest] for i in node.inputs)
        # §5.3, before the work: `Join{left_outer, anti}` and `Aggregate` refuse
        # on an input they cannot prove execution-complete, and a refusal that
        # arrived after the join had run would have built a relation nobody may
        # use.
        self.meta[key] = meta_for(node, input_meta)
        if self.stats is not None:
            # stage 2: the same estimate against the inputs' *realized*
            # cardinality, immediately before this node runs. It can only ever
            # refuse more than stage 1 did (§9.7's ruling).
            from tgms.tgir.admission import admit_node
            admit_node(node, self.stats, self.plan_digest,
                       max((i.n for i in inputs), default=0), self.ceilings)
        # P3.1's runtime telemetry: the first timing anywhere in this runtime.
        # `rows_out`/`rows_in` cost nothing beyond an attribute read and a sum
        # over already-materialized relations, so they are always recorded;
        # only the `perf_counter` pair is gated by `TELEMETRY_TIMING_ENV`.
        timing = _timing_enabled()
        started = perf_counter() if timing else None
        out = self.apply(node, inputs)
        fields: dict[str, Any] = {"rows_out": out.n,
                                  "rows_in": sum(i.n for i in inputs)}
        if started is not None:
            fields["wall_ms"] = (perf_counter() - started) * 1000.0
        self._annotate(key, **fields)
        self.memo[key] = out
        return out

    def _annotate(self, node_digest: str, **fields: Any) -> None:
        """Merge `fields` into `annotations[node_digest]["telemetry"]`,
        without disturbing another lane's key or an already-written field
        (`route`, written from `apply`'s `NodeScan` branch, and `estimates`,
        pre-populated by `execute.py::run_plan` before `run` is called)."""
        telemetry = self.annotations.setdefault(node_digest, {}).setdefault("telemetry", {})
        telemetry.update(fields)

    def apply(self, node: Node, inputs: tuple[Relation, ...]) -> Relation:
        """One node, over already-evaluated inputs."""
        # the ∅ guard, live for the core: only the four `reads_store` node kinds
        # ever see the real adapter
        adapter = adapter_for(node, self.adapter)
        live = self.live.get(node.node_digest)

        if isinstance(node, NodeScan):
            # the physical route `scan.py::_anchored` chose — postings/scan/
            # fallback — surfaced for the one node kind the executor actually
            # knows it for (§6 P3.1). `EdgeScan` and every opaque leaf stay
            # undisclosed (`evaluate.py`'s `kind="opaque"` comment; do not
            # pierce it — that is a deliberate non-disclosure, not a gap).
            route_out: dict[str, str] = {}
            out = scan_nodes(node, adapter, live, self.scans, route_out=route_out)
            if "route" in route_out:
                self._annotate(node.node_digest, route=route_out["route"])
            return out
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
                               self.budget, self.scans)
        if isinstance(node, Join):
            return eval_join(node, inputs[0], inputs[1])
        if isinstance(node, Aggregate):
            return eval_aggregate(node, inputs[0])
        if isinstance(node, PatternMatch):
            # §7.4's annotations channel, `"scan_region"` key (P1.3;
            # `M5_LEVEL1_SOUNDNESS.md` §4.2). `label_filter` wraps
            # `eval_pattern`, so the region has to be captured from
            # `eval_pattern` itself via `region_sink` — after `label_filter`
            # the pre-filter `distinct` uid sets PO-P2 depends on are gone.
            sources = {s.var: inputs[i] for i, s in enumerate(node.sources)}
            sink: dict[str, Any] = {}
            out = label_filter(node,
                               eval_pattern(node, sources, adapter, live,
                                            self.budget, self.scans,
                                            region_sink=sink))
            if sink:  # empty on every widening path (W-P1..W-P6) — no lie recorded
                self.annotations.setdefault(node.node_digest, {})["scan_region"] = sink
            return out
        raise NotImplementedError(
            f"{node.op} has no evaluator yet — it is "
            f"{PENDING.get(node.op, 'a later phase')}'s "
            f"(docs/design/M3_IMPLEMENTATION_PLAN.md §4.1)")


def evaluate_core(node: Node, adapter: Any, *, admit_plan: bool = True,
                  ceilings: dict[str, int] | None = None,
                  bypass_admission: str | None = None) -> Relation:
    """Evaluate a compositional plan rooted at `node`.

    Column pruning runs once, before execution, over the whole reachable plan.

    **Admission is on by default, and that is the fix for F1.** This function
    is a public entry point: a harness, a script or a compiled operator can
    reach the evaluator without going through `call_operator`, and until this
    default flipped, everything that did ran unguarded. The measured symptom
    was `version_history` — refused by the cost guard at 1M through its kernel,
    running to completion through its compiled form. *A route that executes
    what the policy refuses is a hole in the admission story*, so the default
    is now the guarded one and skipping it takes a reason.

    §2.13's three refusal points all arm together: stage 1 (`admit`) prices the
    plan before a row is read, stage 2 re-checks per node against realized
    cardinalities (`stats`), and the runtime `Budget` is the backstop.

    **The fifteen leaves' refusal points do not move** (C5). `admit` returns
    immediately for a plan with no core node — "a single-leaf plan is every
    `call_operator` call, and its admission stays at `algebra.py`'s site with
    the operator's own `cost_fn`" — so an `OpaqueLeaf` is priced exactly where
    it always was, by its own estimator, once.

    `bypass_admission` is the **labeled** escape: a caller that is already
    guarded elsewhere passes the reason, which is recorded on the `Execution`
    and surfaced by `run_plan`. An unlabeled bypass is refused, because a
    silent `admit_plan=False` is how the hole opened in the first place.
    """
    from tgms.tgir.admission import Budget, admit

    if not admit_plan and bypass_admission is None:
        raise InvalidArgError(
            "admission cannot be disabled without a reason: pass "
            "bypass_admission='<why this caller is already guarded>'. "
            "An unguarded compositional route is finding F1.")

    stats = None
    budget = None
    plan_digest = node.node_digest
    if bypass_admission is None:
        stats = adapter.stats()
        admit(node, stats, plan_digest, ceilings)
        budget = Budget(plan_digest)
    execution = Execution(adapter, live_columns(node), stats=stats,
                          plan_digest=plan_digest, ceilings=ceilings,
                          budget=budget)
    execution.admission_bypass = bypass_admission
    return execution.run(node)


__all__ = [
    "AdjacencyCache", "Execution", "PENDING", "ScanCache", "eval_aggregate",
    "eval_expand",
    "eval_expr", "eval_filter", "eval_join", "eval_limit", "eval_order",
    "eval_pattern", "eval_predicate", "eval_project", "eval_property_predicate",
    "eval_type_constraint", "evaluate_core", "limit_truncated", "scan_edges",
    "scan_nodes",
]
