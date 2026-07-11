"""Cost guardrails (WP1.4).

Operators declare a `cost_fn(args, stats) -> {rows_scanned_est, expansions_est}`;
the router rejects with E_COST *before* execution when estimates exceed
ceilings. The error payload carries the estimate and actionable narrowing
suggestions — the planner repair loop consumes these verbatim.
"""

from __future__ import annotations

from typing import Any

from tgms.core.errors import CostError

DEFAULT_CEILINGS = {
    "rows_scanned_est": 20_000_000,
    "expansions_est": 5_000_000,
}

SUGGESTIONS = [
    "narrow the valid-time window",
    "add a node_filter / seed set",
    "restrict rel_types",
    "reduce hops / max_hops",
]


def enforce_cost(op: str, estimate: dict[str, int],
                 ceilings: dict[str, int] | None = None) -> None:
    limits = {**DEFAULT_CEILINGS, **(ceilings or {})}
    over = {k: (estimate.get(k, 0), limits[k]) for k in limits
            if estimate.get(k, 0) > limits[k]}
    if over:
        raise CostError(
            f"estimated cost for {op} exceeds ceilings",
            estimate=estimate,
            ceilings={k: v for k, (_, v) in over.items()},
            suggestions=SUGGESTIONS,
        )


def window_fraction(args: dict[str, Any], stats: dict[str, Any]) -> float:
    """Fraction of the dataset's valid-time extent covered by args['window']."""
    w = args.get("window")
    vt_min, vt_max = stats.get("vt_min"), stats.get("vt_max")
    if not w or vt_min is None or vt_max is None or vt_max <= vt_min:
        return 1.0
    lo = max(w["t_a"], vt_min)
    hi = min(w["t_b"], vt_max)
    return max(0.0, min(1.0, (hi - lo) / (vt_max - vt_min)))


def scan_estimate(args: dict[str, Any], stats: dict[str, Any]) -> dict[str, int]:
    """Default cost model: one interval-pruned scan over edge versions."""
    rows = int(stats.get("n_edge_versions", 0) * window_fraction(args, stats)) + \
        stats.get("n_node_versions", 0)
    return {"rows_scanned_est": rows, "expansions_est": 0}
