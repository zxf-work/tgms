"""§5.3's metadata propagation — the thirteen rows, plus the three prose rules.

The mechanical parts are mechanical. Four are not, and each is a place §5.3
says an implementation goes wrong first:

1. **`Join{inner}` drops to `unknown`, *below* the meet** — deliberately. No
   ranking key and no cursor survives a join, so the output is not a `top-k` or
   a page of anything. Marked in the spec as deliberate precisely so it does
   not read as a bug here.
2. **`Join{left_outer}` / `{anti}` refuse `E_INCOMPLETE`** unless the probe is
   execution-complete, and `Aggregate` refuses unless its input is. These are
   **runtime** refusals — completeness is not knowable before execution — and
   they carry `IncompletenessRefusal`, a *different* shape from
   `RefusalCertificate`: every field of the latter except the digest is
   inapplicable to them (no ceiling, no estimate, no calibration reference), and
   conflating the two would make `audit()` unsound on one.
3. **`Aggregate`'s domain-fall is the single exception to "never raise"** — and
   its guard is domain **equality** (`tgms/tgir/domain.py`).
4. **`Expand{unbounded}` inherits its input's completeness** and may lower to
   `refused`, never to `timeout-truncated`. IS2 is the worked case: `s5`'s
   `Limit(10)` makes the relation `top-k`, `s6` inherits it, and the plan's
   result is `top-k, exact`. Had `s6` output `complete`, an `Aggregate` above it
   would license `@ExactCardinality` over ten rows presented as the exact
   population — the false certification the lattice exists to foreclose.
"""

from __future__ import annotations

from typing import Any

from tgms.tgir.domain import Domain
from tgms.tgir.metadata import (
    Completeness, Exactness, IncompletenessRefusal, Provenance, ResultMeta, meet,
    meet_all,
)
from tgms.tgir.node import (
    Aggregate, EdgeScan, Expand, Filter, Join, Limit, Node, NodeScan, Order,
    PatternMatch, Project, PropertyPredicate, TypeConstraint, Unbounded,
)

#: Completeness values that assert **execution** completeness (§5.2's table).
#: `paginated` is one of them — delivery is incomplete, execution is not — and
#: that is exactly the distinction the two absence-deriving preconditions turn
#: on. `unknown` is not: it asserts the absence of certification, so it cannot
#: discharge a precondition that requires one.
EXECUTION_COMPLETE: frozenset[Completeness] = frozenset({
    Completeness.COMPLETE, Completeness.PAGINATED, Completeness.TOP_K,
})


def is_execution_complete(meta: ResultMeta) -> bool:
    return meta.completeness in EXECUTION_COMPLETE


def require_execution_complete(node: Node, meta: ResultMeta, which: str,
                               reason: str) -> None:
    """§2.8 / §2.10's runtime precondition.

    "An anti-join against a truncated probe reports false absences — not merely
    uncertified rows, but wrong ones." False invalidation is allowed; false
    certification never is.
    """
    if is_execution_complete(meta):
        return
    raise IncompletenessRefusal.error(
        node_digest=node.node_digest, op=node.op, reason=reason,
        offending=which, offending_meta=meta)


def meta_for(node: Node, inputs: tuple[ResultMeta, ...], *,
             provenance: Provenance | None = None,
             engine_truncated: bool = False) -> ResultMeta:
    """One node's `R`-minus-value, from its inputs'."""
    exactness = _exactness(node, inputs)

    if isinstance(node, (NodeScan, EdgeScan)):
        # "complete over Σ unless the engine reports a cutoff, then
        # timeout-truncated" — no backend reports one today, so the flag is a
        # parameter rather than an assumption
        completeness = (Completeness.TIMEOUT_TRUNCATED if engine_truncated
                        else Completeness.COMPLETE)
        return ResultMeta(node.sigma, completeness, exactness, provenance,
                          domain=Domain.of(node.sigma))

    inherited = inputs[0] if inputs else None

    if isinstance(node, Expand):
        # exact/bounded: the input's. unbounded: **the input's**, provided the
        # fixpoint completed — this node never emits `timeout-truncated`; it is
        # `refused` instead, which the evaluator raises rather than returns.
        assert inherited is not None
        return _inherit(node, inherited, exactness)

    if isinstance(node, (Filter, TypeConstraint)):
        assert inherited is not None
        return _inherit(node, inherited, exactness,
                        domain=inherited.domain.narrow(node, "predicate"))

    if isinstance(node, PropertyPredicate):
        assert inherited is not None
        return _inherit(node, inherited, exactness,
                        domain=inherited.domain.narrow(node, "property"))

    if isinstance(node, Project):
        assert inherited is not None
        return _inherit(node, inherited, exactness)

    if isinstance(node, Order):
        assert inherited is not None
        # reserves `@OrderedBy(f)` — recorded in provenance, **not** certified
        return _inherit(node, inherited, exactness)

    if isinstance(node, Limit):
        assert inherited is not None
        return _limit_meta(node, inherited, exactness)

    if isinstance(node, PatternMatch):
        # complete iff every input relation and every scanned variable domain
        # is complete; otherwise the meet of its inputs'
        base = meet_all(m.completeness for m in inputs) if inputs \
            else Completeness.COMPLETE
        return ResultMeta(node.sigma, base, exactness, provenance,
                          domain=Domain.of(node.sigma))

    if isinstance(node, Join):
        return _join_meta(node, inputs, exactness)

    if isinstance(node, Aggregate):
        return _aggregate_meta(node, inputs, exactness)

    assert inherited is not None
    return _inherit(node, inherited, exactness)


def _inherit(node: Node, source: ResultMeta, exactness: Exactness,
             domain: Domain | None = None) -> ResultMeta:
    return ResultMeta(node.sigma, source.completeness, exactness, None,
                      domain=domain if domain is not None else source.domain)


def _limit_meta(node: Limit, source: ResultMeta, exactness: Exactness) -> ResultMeta:
    """§2.12's two uses, which produce different metadata over identical rows.

    Under an `Order` it is **top-k**: the domain narrows to "the `n` greatest
    rows under the recorded ranking key". Otherwise it is a **page cut**:
    delivery is incomplete, execution is not, and `rows_total` keeps its
    cardinality claim.

    A non-`complete` input gives the **meet** of its value with the new one,
    which is `unknown` whenever the two are incomparable — the honest answer,
    since an operator combining a truncated page with a top-k selection can
    certify neither property.
    """
    kind = Completeness.TOP_K if node.is_top_k else Completeness.PAGINATED
    completeness = kind if source.completeness is Completeness.COMPLETE \
        else meet(source.completeness, kind)
    domain = source.domain.narrow(node, "top-k") if node.is_top_k else source.domain
    return ResultMeta(node.sigma, completeness, exactness, None, domain=domain)


def _join_meta(node: Join, inputs: tuple[ResultMeta, ...],
               exactness: Exactness) -> ResultMeta:
    left, right = inputs
    if node.join_type in ("left_outer", "anti"):
        require_execution_complete(
            node, right, "right",
            f"Join{{{node.join_type}}} derives rows from absence on the right, "
            f"and absence over an incomplete probe is a false absence")
    both_complete = (left.completeness is Completeness.COMPLETE
                     and right.completeness is Completeness.COMPLETE)
    # ... otherwise `unknown`: a deliberate drop *below* the meet, since no
    # ranking key or cursor survives a join
    completeness = Completeness.COMPLETE if both_complete else Completeness.UNKNOWN
    domain = left.domain if node.join_type == "anti" else left.domain
    return ResultMeta(node.sigma, completeness, exactness, None, domain=domain)


def _aggregate_meta(node: Aggregate, inputs: tuple[ResultMeta, ...],
                    exactness: Exactness) -> ResultMeta:
    """§5.3 rule 3's single stated exception, with its guard.

    An `Aggregate` outputs `complete` **over its input's domain** — the enum
    value rises only because the domain fell, which is not a raise. The guard
    is that the output domain *is* the input's: certifying "these are exactly
    the 10 greatest under ⟨key⟩" is sound, certifying "these are exactly the
    person's messages" is not, and only the domain distinguishes them.
    """
    source = inputs[0]
    require_execution_complete(
        node, source, "input",
        "Aggregate is computed over a relation and requires it execution-complete; "
        "a count over a truncated input is a wrong number, not a partial one")
    return ResultMeta(node.sigma, Completeness.COMPLETE, exactness, None,
                      domain=source.domain)


def _exactness(node: Node, inputs: tuple[ResultMeta, ...]) -> Exactness:
    """"an operator's output is `exact` iff every input is `exact` and the
    operator introduces no approximation" — and no v1 operator introduces one."""
    out = Exactness.EXACT
    for meta in inputs:
        if meta.exactness is not Exactness.EXACT:
            out = meta.exactness
    return out


def unbounded_expand_completeness(node: Expand) -> None:
    """§2.3 restriction 3, as an assertion about what this node may emit.

    An unbounded `Expand` is **never** `timeout-truncated`: a partial fixpoint
    produces false absences, so its outcome is `complete` or `Refused`. The
    refusal is raised by the budget rather than returned as a value, which is
    what makes the illegal state unrepresentable here.
    """
    assert isinstance(node.hops, Unbounded)


def domain_of(meta: ResultMeta) -> Domain:
    assert meta.domain is not None
    return meta.domain


def summary(meta: ResultMeta) -> dict[str, Any]:
    return {"completeness": meta.completeness.value,
            "exactness": meta.exactness.value,
            "domain": meta.domain.to_json() if meta.domain else None}


__all__ = [
    "EXECUTION_COMPLETE", "domain_of", "is_execution_complete", "meta_for",
    "require_execution_complete", "summary", "unbounded_expand_completeness",
]
