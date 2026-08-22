"""Building the `OpaqueLeaf` for one operator call (R7; M2 plan §5).

The fifteen existing operators become **opaque algorithmic TGIR operators** —
plan nodes that carry the full result-metadata tuple while delegating to the
exact kernel that runs today. This module is the constructor: it derives Σ from
the bound arguments (§5.2), reads the output schema off `OperatorSpec`, and
carries the `∅` classification that decides whether the kernel may see a live
storage adapter.

**Σ is per call, never per operator**, which is the whole reason the leaf is a
plan node rather than a registry annotation: `as_of_tt`, `window`, `t_valid`,
`t1`/`t2` all move per call, and so does the output schema (`entity_history`'s
`edges` list exists only when `include_edges`).
"""

from __future__ import annotations

from typing import Any

from tgms.core.model import OPEN_END, Interval
from tgms.tgir.node import OpaqueLeaf
from tgms.tgir.types import Sigma

#: §5.2's table, transcribed. `T_v` is derived per operator from the bound
#: args; `vt_mode` is the keying mode the kernel reads its intervals under
#: (§3.2), which is a *scan* parameter rather than part of Σ (§3.1).
#:
#: `overlap`  — `x.vt_s < v_b ∧ v_a < x.vt_e`; the general window reading.
#: `instant`  — `x.vt_s ≤ t < x.vt_e`; all snapshot semantics.
#: `event`    — `v_a ≤ x.vt_s < v_b`; "an edge version *is* an event at `vt_s`".
LEAF_VT_MODE: dict[str, str] = {
    "entity_history": "overlap",
    "resolve_entities": "overlap",
    "co_active": "overlap",
    "version_history": "overlap",
    "aggregate_events": "event",
    "graph_metric_timeseries": "event",
    "burst_detection": "event",
    "count_temporal_motifs": "event",
    "find_temporal_motif_instances": "event",
    "temporal_reachability": "event",
    "temporal_paths": "event",
    "snapshot_subgraph": "instant",
    "diff_snapshots": "instant",
    "neighborhood_evolution": "instant",
    "compute": "overlap",
}

#: Operators whose `T_v` is the whole valid-time extent because they take no
#: window at all (§5.2). `co_active`'s is reserved for M3 (§6 note (a)).
_UNWINDOWED = frozenset({"entity_history", "resolve_entities", "co_active"})

#: The default scope: `T_v = [0, OPEN_END)`, matching every operator's own
#: default and the fourteen store readers' `as_of_tt` default.
FULL_EXTENT = Interval(0, OPEN_END)


def sigma_for(op: str, args: dict[str, Any]) -> Sigma:
    """Σ = `(T_v, T_b)` for one bound call (§5.2).

    `T_b = as_of_tt` for all fourteen store-reading operators; `compute` has
    none — its Σ is *inherited* (§6 #15), and a standalone call inherits the
    default.
    """
    t_b = args.get("as_of_tt")
    t_b = OPEN_END if t_b is None else int(t_b)

    if op in _UNWINDOWED or op == "compute":
        return Sigma((FULL_EXTENT,), t_b)
    if op == "snapshot_subgraph":
        return Sigma.at_instant(int(args["t_valid"]), as_of_tt=t_b)
    if op == "diff_snapshots":
        # the one leaf whose Σ is a *pair* of instants — and, with
        # `snapshot_subgraph`, the first operator in the system to meet §5.5.5's
        # carve arm, because an instant Σ is what makes the `vt` narrowing worth
        # something to lose
        return Sigma.at_instants(int(args["t1"]), int(args["t2"]), as_of_tt=t_b)
    if op == "neighborhood_evolution":
        return Sigma.in_window(int(args["t1"]), int(args["t2"]) + 1, as_of_tt=t_b)
    window = args.get("window")
    if isinstance(window, dict) and "t_a" in window and "t_b" in window:
        return Sigma.in_window(int(window["t_a"]), int(window["t_b"]), as_of_tt=t_b)
    # an operator whose window is optional and absent reads the whole extent
    return Sigma((FULL_EXTENT,), t_b)


def build_leaf(op: str, bound_args: dict[str, Any],
               out_fields: tuple[str, ...]) -> OpaqueLeaf:
    """The plan node for one call, from post-`validate_args` arguments.

    `withhold_adapter` is derived from the `∅` classification and cannot be
    talked out of it: constructing `compute` with the adapter *not* withheld is
    refused by `OpaqueLeaf` itself.
    """
    return OpaqueLeaf.build(op, bound_args, out_fields,
                            sigma=sigma_for(op, bound_args),
                            vt_mode=LEAF_VT_MODE.get(op, "overlap"))


__all__ = ["FULL_EXTENT", "LEAF_VT_MODE", "build_leaf", "sigma_for"]
