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
    # Unit ceilings are memory-shaped BACKSTOPS since D-087: the admission
    # frontier (docs/eval_guardrail.md) measured the old defaults refusing
    # 18% of finishable calls with the optimum 256x higher, so time is the
    # primary axis now and these sit 256x up from the D-030-era values.
    "rows_scanned_est": 5_120_000_000,
    "expansions_est": 1_280_000_000,
    # The primary ceiling. Estimated from per-operator coefficients below.
    "time_est_ms": 10_000,
}

#: ms of wall time per MILLION estimated units (rows + expansions), by
#: operator — measured, not guessed: medians from the 90-cell admission
#: frontier receipt (eval-guardrail-frontier.json, xzgpu, D-086). These are
#: HOST-CALIBRATED; a slower deployment scales them uniformly with
#: TGMS_TIME_COEFF_SCALE. Operators absent here get the conservative
#: maximum, so an uncalibrated operator over-refuses rather than
#: over-admits. count_temporal_motifs is deliberately absent: its
#: coefficient depends on node_filter presence, so _motif_cost supplies its
#: own time_est_ms.
TIME_COEFF_MS_PER_M = {
    "graph_metric_timeseries": 55.0,
    "burst_detection": 55.0,          # same scan shape as the timeseries
    "co_active": 115.0,
    "temporal_reachability": 170.0,
    "temporal_paths": 170.0,          # frontier-bounded; reach coeff is the cap
    "entity_history": 55.0,           # postings read; scan coeff is generous
    "snapshot_subgraph": 115.0,
    "diff_snapshots": 115.0,
    # The documented off-class operator (D-058): materializes one Python
    # object per version — measured 153.9 s per 10M rows, i.e. ~15,390 ms/M,
    # 116x the columnar rate. The first draft of this table let it fall to
    # the "conservative" fallback below, which is 90x too cheap for it; the
    # pinned refusal test caught that immediately.
    "version_history": 15_400.0,
    # The TGIR core's unbounded `Expand` — not an operator, which is why the
    # key is a node name. Measured on CollegeMsg (native, replay-built), same
    # store digest 7efd7f4f0ec02cb8 and the same two shapes, on two hosts:
    #
    #   host             ten anchors   hoisted    receipt
    #   macOS-arm64        183.3       156.8      calib/expand-unbounded-2026-08-21.md
    #   Linux x86_64       343.8       269.3      calib/expand-unbounded-2026-08-22.md
    #
    # **343.8 is the shipped value (guardrail-policy-v2).** The dearer of the
    # two *shapes* is taken, as before, so the estimate over-refuses rather than
    # over-admits on the shape it was not measured against — §2.13's own
    # instruction for an uncalibrated operator, applied to a calibrated one at
    # its worst measured shape.
    #
    # Why the host, not the shape, decided this. Every other coefficient in
    # this table is xzgpu-calibrated (D-087's 90-cell frontier; D-096 froze the
    # policy on that basis and set TGMS_TIME_COEFF_SCALE as the *host* knob,
    # e.g. 0.6 for iTiger). This entry, added at M3.1, was the one row measured
    # somewhere else — and 183.3/343.8 means it was 1.88x too cheap on the only
    # host whose numbers this project may publish. A coefficient that is too
    # cheap over-ADMITS, which is the one direction §2.13 says the guard must
    # not fail in, so the macOS value was not a conservative choice on Linux;
    # it was the unsafe one. Landing 343.8 makes the table host-consistent for
    # the first time.
    #
    # M3 was measured and scored under guardrail-policy-v1, i.e. under 183.3.
    # Its result — no row refused by the cost guard at the frozen policy, on
    # bitcoin-otc and CollegeMsg (TGIR_M3_MEASURED_REPORT.md "Honest
    # disclosure") — stands as a **v1 fact** and is not re-scored here. Nothing
    # in benchmarks/tgir-v1/ is rewritten by this change.
    #
    # Worth noting against `_MAX_COEFF` below: the "conservative" columnar-class
    # fallback (170) is now **2.0x too cheap** for this node (it was 8% too
    # cheap against the macOS value), so an uncalibrated unbounded expansion
    # over-admits by twice as much as the old comment claimed.
    "tgir_expand_unbounded": 343.8,
}
#: Fallback for uncalibrated operators: the dearest *columnar-class* rate,
#: not the global max — version_history's 15,400 would refuse any unlisted
#: 1M-row scan outright, which is over-refusal of exactly the kind the
#: frontier measured this refresh out of.
_MAX_COEFF = 170.0


def add_time_estimate(op: str, estimate: dict[str, int]) -> dict[str, int]:
    """Attach `time_est_ms` unless the cost_fn already supplied one."""
    import os

    if "time_est_ms" in estimate:
        return estimate
    units = estimate.get("rows_scanned_est", 0) + estimate.get("expansions_est", 0)
    coeff = TIME_COEFF_MS_PER_M.get(op, _MAX_COEFF)
    scale = float(os.environ.get("TGMS_TIME_COEFF_SCALE", "1.0"))
    return {**estimate, "time_est_ms": int(units * coeff * scale / 1_000_000)}


def point_read_estimate(args, stats):
    """Cost of reading one identity's versions through the postings (D-087).

    The pre-refresh model (`scan_estimate`) priced this as a full-store
    scan — 1,015,199 estimated rows for a 0.13 ms read at 1M, seven orders
    of magnitude of error, and the first thing any tightened ceiling would
    wrongly refuse. What the operator actually returns is one identity's
    version list, whose expected size is the store's mean.
    """
    n = stats.get("n_node_versions", 0)
    entities = max(stats.get("n_entities", 1), 1)
    return {"rows_scanned_est": max(1, -(-n // entities)), "expansions_est": 0}

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
