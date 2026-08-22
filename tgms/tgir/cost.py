"""Per-node cost estimates for plan-level admission (§2.13, M3 plan §3.8a).

`guardrails.enforce_cost` is per *operator call*, stateless, keyed by a
`REGISTRY` name, with the estimate coming from `OperatorSpec.cost_fn(args,
stats)`. Core nodes have no registry entry and therefore no `cost_fn`,
`cost_fn` has no input-cardinality parameter, and there is no cardinality
estimator anywhere in the tree. This module is that missing piece, and every
shape below is derived from an existing estimator so the coefficients stay
comparable with the fifteen operators'.

**Selectivity is 1.0 by fiat** — §2.4 states outright that it is not estimated
in v1, and inventing one here would be a cost-based optimizer, which is a stated
non-goal. The consequence is honest and worth naming: a static estimate cannot
see a blow-up `Join`, which is exactly the argument for admission's second stage.
"""

from __future__ import annotations

from typing import Any

from tgms.temporal.guardrails import TIME_COEFF_MS_PER_M, _MAX_COEFF
from tgms.tgir.node import (
    Aggregate, Bounded, EdgeScan, Exact, Expand, Filter, Join, Limit, Node,
    NodeScan, Order, PatternMatch, Project, PropertyPredicate, TypeConstraint,
    Unbounded,
)
from tgms.tgir.types import Sigma

#: The coefficient key M3.1's calibration receipt writes into
#: `TIME_COEFF_MS_PER_M`. Named for the node rather than for an operator,
#: because it prices a node that no operator is.
UNBOUNDED_EXPAND_COEFF = "tgir_expand_unbounded"

#: §2.13: "uncalibrated core operators take the conservative columnar-class
#: fallback (170 ms/M), so a new operator over-refuses rather than
#: over-admits."
CORE_COEFF: dict[str, float] = {
    "NodeScan": 55.0,        # the scan-class rate `entity_history` is priced at
    "EdgeScan": 55.0,
    "Filter": 55.0,          # a mask over an in-memory relation
    "PropertyPredicate": 170.0,   # a per-row dict lookup, not a numpy op
    "TypeConstraint": 55.0,
    "Project": 115.0,
    "Order": 115.0,
    "Limit": 55.0,
    "Join": 170.0,
    "Aggregate": 115.0,
}


def window_fraction_of(sigma: Sigma, stats: dict[str, Any]) -> float:
    """The share of the store's valid-time extent Σ covers — the same shape
    `guardrails.window_fraction` computes for the operators, over Σ instead of
    over an operator's `window` argument."""
    vt_min, vt_max = stats.get("vt_min"), stats.get("vt_max")
    if vt_min is None or vt_max is None or vt_max <= vt_min:
        return 1.0
    covered = 0
    for interval in sigma.t_v:
        lo, hi = max(interval.start, vt_min), min(interval.end, vt_max)
        covered += max(0, hi - lo)
    return max(0.0, min(1.0, covered / (vt_max - vt_min)))


def branching(stats: dict[str, Any]) -> float:
    """`e_w / entities` — §2.3's branching factor, and the same ratio
    `ops_paths` uses. Mean, not max: the motif lesson (one heavy sender does not
    sit on every hop of every path) is why."""
    entities = max(1, int(stats.get("n_entities", 1)))
    return max(1.0, int(stats.get("n_edge_versions", 0)) / entities)


def cost_of(node: Node, stats: dict[str, Any], in_card: int) -> dict[str, int]:
    """`{rows_scanned_est, expansions_est, out_card}` for one node.

    `out_card` is not part of §2.13's axes — it is the cardinality the *next*
    node is estimated against, which is the piece `cost_fn(args, stats)` has no
    parameter for and the reason this function exists at all.
    """
    if isinstance(node, NodeScan):
        rows = int(stats.get("n_node_versions", 0) * window_fraction_of(node.sigma,
                                                                       stats))
        if node.uids:
            # a bind-time uid set is a postings read, not a store scan —
            # `guardrails.point_read_estimate`'s shape
            rows = min(rows, len(node.uids) * 8)
        return {"rows_scanned_est": rows, "expansions_est": 0, "out_card": rows}

    if isinstance(node, EdgeScan):
        rows = int(stats.get("n_edge_versions", 0) * window_fraction_of(node.sigma,
                                                                       stats))
        if node.rel_types:
            counts = stats.get("rel_type_counts") or {}
            total = sum(counts.values()) or 1
            # the ONLY selectivity signal `stats()` carries
            share = sum(counts.get(r, 0) for r in node.rel_types) / total
            rows = int(rows * share)
        return {"rows_scanned_est": rows, "expansions_est": 0, "out_card": rows}

    if isinstance(node, Expand):
        return _expand_cost(node, stats, in_card)

    if isinstance(node, (Filter, PropertyPredicate, TypeConstraint)):
        # §2.4: selectivity is not estimated in v1, so the output is the input
        return {"rows_scanned_est": in_card, "expansions_est": 0,
                "out_card": in_card}

    if isinstance(node, (Project, Order)):
        return {"rows_scanned_est": in_card, "expansions_est": 0,
                "out_card": in_card}

    if isinstance(node, Limit):
        return {"rows_scanned_est": in_card, "expansions_est": 0,
                "out_card": min(in_card, node.n)}

    if isinstance(node, Join):
        # build + probe is |L| + |R|; the output proxy is max(|L|, |R|), which
        # is a *guess* — with no distinct-key statistics anywhere in `stats()`
        # a static estimate cannot see a blow-up join (§7.3, and the argument
        # for stage 2)
        left, right = (cost_of(i, stats, 0)["out_card"] if not i.inputs else in_card
                       for i in node.inputs)
        return {"rows_scanned_est": left + right, "expansions_est": 0,
                "out_card": max(left, right)}

    if isinstance(node, Aggregate):
        return {"rows_scanned_est": in_card, "expansions_est": 0,
                "out_card": max(1, in_card // 2)}

    if isinstance(node, PatternMatch):
        domains = max(1, in_card)
        edges = len(node.pattern.edge_pats)
        return {"rows_scanned_est": domains,
                "expansions_est": domains * edges,
                "out_card": domains}

    return {"rows_scanned_est": in_card, "expansions_est": 0, "out_card": in_card}


def _expand_cost(node: Expand, stats: dict[str, Any],
                 in_card: int) -> dict[str, int]:
    """§2.3's three shapes.

    `exact(k)`: `in_card × branching^k`. `bounded(a,b)`: the sum over `j ≤ b`.
    `unbounded`: the frontier accumulation `_reach_cost` uses, to a cap — and it
    is the least-calibrated number in the guardrail, which is why M3.1 owes a
    measurement rather than an assumption (§2.3, §7.4).
    """
    b = branching(stats)
    spec = node.hops
    seeds = max(1, in_card)
    if isinstance(spec, Exact):
        expansions = seeds * (b ** spec.k)
    elif isinstance(spec, Bounded):
        expansions = seeds * sum(b ** j for j in range(spec.a, spec.b + 1))
    else:
        assert isinstance(spec, Unbounded)
        # the fixpoint is bounded by the reachable set, so the frontier
        # accumulation saturates at the windowed edge count rather than
        # compounding without limit
        windowed = int(stats.get("n_edge_versions", 0)
                       * window_fraction_of(node.sigma, stats))
        expansions = min(seeds * (b ** 6), max(windowed, seeds))
    return {"rows_scanned_est": int(seeds), "expansions_est": int(expansions),
            "out_card": int(min(expansions, max(1, stats.get("n_entities", 1))
                                * seeds))}


def time_estimate(node: Node, estimate: dict[str, int]) -> float:
    """`time_est_ms` for one node, from its unit estimate and its coefficient.

    An uncalibrated node takes `_MAX_COEFF` — the dearest *columnar-class*
    rate, not the global maximum, for the reason `guardrails` records: the
    global maximum would refuse any unlisted 1M-row scan outright.
    """
    units = estimate.get("rows_scanned_est", 0) + estimate.get("expansions_est", 0)
    coeff = _coefficient(node)
    return units / 1_000_000.0 * coeff


def _coefficient(node: Node) -> float:
    if isinstance(node, Expand) and isinstance(node.hops, Unbounded):
        return TIME_COEFF_MS_PER_M.get(UNBOUNDED_EXPAND_COEFF, _MAX_COEFF)
    if isinstance(node, Expand):
        return _MAX_COEFF
    return CORE_COEFF.get(type(node).__name__, _MAX_COEFF)


def scale() -> float:
    """`TGMS_TIME_COEFF_SCALE`, honoured exactly as `guardrails` honours it, so
    a slower deployment scales the core and the operators together."""
    import os

    try:
        return float(os.environ.get("TGMS_TIME_COEFF_SCALE", "1"))
    except ValueError:
        return 1.0


__all__ = ["CORE_COEFF", "UNBOUNDED_EXPAND_COEFF", "branching", "cost_of", "scale",
           "time_estimate", "window_fraction_of"]
