"""Plan-level admission and the `RefusalCertificate` (§2.13, EVIDENCE_MODEL §2).

§2.13 fixes the model: each node contributes an estimate, the plan's
`time_est_ms` is the **sum over nodes**, each unit axis likewise, admission runs
**before execution over the whole DAG**, and a refusal produces
`RefusalCertificate = ⟨plan_digest, policy_version, estimates, ceiling hit,
calibration_ref⟩`. That certificate did not exist in the code — `enforce_cost`
raises a `CostError` carrying `{estimate, ceilings, suggestions}` with no plan
digest, no policy version and no calibration reference (§9.4). This module
builds it.

**Two stages, per the coordinator's §9.5/§9.7 ruling.**

- **Stage 1** is §2.13 literally: walk the DAG bottom-up propagating estimated
  cardinalities, sum each axis, enforce once against the plan.
- **Stage 2** re-evaluates each node against its inputs' **realized**
  cardinality immediately before that node runs. It is strictly a *second
  opportunity to refuse* — it can never admit something stage 1 refused — so it
  does not weaken §2.13. It exists because `Join` and `PatternMatch`
  cardinalities are the two the static model cannot estimate (no distinct-key
  statistics exist anywhere in `stats()`), and because a plan admitted on a
  wrong guess otherwise fails as an OOM rather than as a policy-certified
  refusal.
- **A runtime budget** inside the expansion fixpoint refuses with the same
  certificate marked `stage: "runtime"`, producing `completeness = refused` and
  **never** `timeout-truncated` — the distinction is load-bearing, because
  `timeout-truncated` on an unbounded `Expand` is precisely the false absence
  §2.3 forbids.

**What does not change: the fifteen leaves.** Admission for a single-leaf plan
stays exactly where M2 left it (`algebra.py`, the operator's own `cost_fn`, no
plan-level sum). Plan-level admission runs only over core nodes. Otherwise M3
silently re-prices every operator call in the tree — which M2's C5 froze and
`temporal_paths_refused` pins in the frozen-digest receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tgms.core.errors import CostError
from tgms.temporal.guardrails import DEFAULT_CEILINGS, SUGGESTIONS
from tgms.tgir.cost import cost_of, scale, time_estimate
from tgms.tgir.node import Node, OpaqueLeaf

#: EVIDENCE_MODEL §9's frozen policy, and the receipt its coefficients were
#: measured from. Both are constants of the *policy*, not of a call.
POLICY_VERSION = "guardrail-policy-v1"
CALIBRATION_REF = "docs/eval_guardrail.md#frontier"


@dataclass(frozen=True, slots=True)
class RefusalCertificate:
    """`⟨plan_digest, policy_version, estimates, ceiling hit, calibration_ref⟩`.

    "A policy-certified refusal proves the admission decision was consistent
    with the declared policy; it never proves nonexistence or that the plan
    truly would have exceeded budget" (§2.13). `stage` records *which* of the
    three refusal points fired, which is what the freeze's secondary admission
    axis reports.
    """

    plan_digest: str
    stage: str                          # "plan" | "node" | "runtime"
    estimates: dict[str, int]
    ceilings: dict[str, int]
    policy_version: str = POLICY_VERSION
    calibration_ref: str = CALIBRATION_REF
    node_digest: str | None = None
    suggestions: tuple[str, ...] = tuple(SUGGESTIONS)

    def to_json(self) -> dict[str, Any]:
        out = {
            "plan_digest": self.plan_digest,
            "stage": self.stage,
            "estimates": dict(self.estimates),
            "ceilings": dict(self.ceilings),
            "policy_version": self.policy_version,
            "calibration_ref": self.calibration_ref,
            "suggestions": list(self.suggestions),
        }
        if self.node_digest is not None:
            out["node_digest"] = self.node_digest
        return out

    def raise_(self, what: str) -> None:
        """Raise as `E_COST`, **additively**: `estimate`, `ceilings` and
        `suggestions` keep the exact shape the planner repair loop already
        consumes (`guardrails.enforce_cost`), and the certificate rides beside
        them under a new key. Renaming or removing one would fire
        `docs/STABILITY.md` §3 (C13)."""
        raise CostError(
            f"estimated cost for {what} exceeds ceilings",
            estimate=dict(self.estimates),
            ceilings=dict(self.ceilings),
            suggestions=list(self.suggestions),
            refusal_certificate=self.to_json(),
        )


#: The runtime backstop's default, **measured rather than guessed**.
#:
#: `ops_paths.MAX_EXPANSIONS = 500_000` is the precedent, and it is the wrong
#: size for this unit: bo33's nine-edge motif over bitcoin-otc — the largest
#: real pattern in the corpus, and one `TGIR_SPEC.md` §7.2 explicitly says the
#: `rating > 0` pushdown "keeps admissible" — charges **12.1 million** candidate
#: bindings and completes in ~27 s. A 500k backstop would refuse the corpus
#: instead of catching a runaway.
#:
#: So the default sits ~4× above that measurement. The backstop exists to turn
#: an OOM into a policy-certified refusal, not to second-guess a workload the
#: spec names admissible; a genuine blow-up (a cross product) passes this in
#: seconds.
DEFAULT_EXPANSION_BUDGET = 50_000_000


@dataclass
class Budget:
    """The runtime backstop. Today's precedent is `ops_paths.MAX_EXPANSIONS`
    raising `CostError` mid-execution; this is that, with a certificate.

    The unit is one *candidate*: a neighbour visited by an expansion, or a
    partial binding considered by a pattern search.
    """

    plan_digest: str
    limit: int = DEFAULT_EXPANSION_BUDGET
    spent: int = 0
    ceilings: dict[str, int] = field(default_factory=dict)

    def charge(self, expansions: int) -> None:
        self.spent += int(expansions)
        if self.spent > self.limit:
            RefusalCertificate(
                plan_digest=self.plan_digest, stage="runtime",
                estimates={"expansions_est": self.spent},
                ceilings={"expansions_est": self.limit},
            ).raise_("the plan's expansion budget")


def plan_estimate(root: Node, stats: dict[str, Any]) -> dict[str, Any]:
    """Stage 1's walk: bottom-up cardinality propagation, then the per-axis sum.

    A node reached twice in a DAG is estimated **once** — the same dedup by
    `node_digest` the evaluator applies, so the sum prices what will actually
    run rather than counting a shared subtree twice.
    """
    per_node: dict[str, dict[str, Any]] = {}
    totals = {"rows_scanned_est": 0, "expansions_est": 0}
    time_ms = 0.0

    def visit(node: Node) -> int:
        key = node.node_digest
        cached = per_node.get(key)
        if cached is not None:
            return int(cached["out_card"])
        in_card = max((visit(i) for i in node.inputs), default=0)
        estimate = cost_of(node, stats, in_card)
        estimate["time_est_ms"] = time_estimate(node, estimate) * scale()
        per_node[key] = estimate
        totals["rows_scanned_est"] += int(estimate["rows_scanned_est"])
        totals["expansions_est"] += int(estimate["expansions_est"])
        nonlocal time_ms
        time_ms += float(estimate["time_est_ms"])
        return int(estimate["out_card"])

    visit(root)
    return {**totals, "time_est_ms": int(time_ms), "per_node": per_node}


def admit(root: Node, stats: dict[str, Any], plan_digest: str,
          ceilings: dict[str, int] | None = None) -> dict[str, Any]:
    """Stage 1. Returns the plan estimate, or raises `E_COST` with a
    certificate.

    **A plan with no core node is not priced here at all** — a single-leaf plan
    is every `call_operator` call, and its admission stays at
    `algebra.py`'s site with the operator's own `cost_fn`.
    """
    if not has_core_node(root):
        return {}
    estimate = plan_estimate(root, stats)
    _enforce(plan_digest, "plan", estimate, ceilings, None)
    return estimate


def admit_node(node: Node, stats: dict[str, Any], plan_digest: str, in_card: int,
               ceilings: dict[str, int] | None = None) -> None:
    """Stage 2: the same estimate against a **realized** input cardinality,
    immediately before the node runs. Refuse-more-only by construction — it
    computes the same function of a number that is no longer a guess."""
    estimate = dict(cost_of(node, stats, in_card))
    estimate["time_est_ms"] = int(time_estimate(node, estimate) * scale())
    _enforce(plan_digest, "node", estimate, ceilings, node.node_digest)


def _enforce(plan_digest: str, stage: str, estimate: dict[str, Any],
             ceilings: dict[str, int] | None, node_digest: str | None) -> None:
    limits = {**DEFAULT_CEILINGS, **(ceilings or {})}
    over = {k: limits[k] for k in limits if int(estimate.get(k, 0)) > limits[k]}
    if over:
        RefusalCertificate(
            plan_digest=plan_digest, stage=stage,
            estimates={k: int(v) for k, v in estimate.items() if k != "per_node"},
            ceilings=over, node_digest=node_digest,
        ).raise_(f"the plan{'' if node_digest is None else ' node'}")


def has_core_node(root: Node) -> bool:
    """True when the plan contains anything other than opaque leaves."""
    seen: set[str] = set()

    def walk(node: Node) -> bool:
        key = node.node_digest
        if key in seen:
            return False
        seen.add(key)
        if not isinstance(node, OpaqueLeaf):
            return True
        return any(walk(i) for i in node.inputs)

    return walk(root)


__all__ = ["Budget", "CALIBRATION_REF", "DEFAULT_EXPANSION_BUDGET",
           "POLICY_VERSION", "RefusalCertificate",
           "admit", "admit_node", "has_core_node", "plan_estimate"]
