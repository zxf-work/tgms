"""Compiled `entity_history` — §6 #1, and the **multi-root** case.

The operator has two payload lists, so §6 requires "several TGIR roots plus a
result-assembly step": one root for the version rows, a second for the incident
edges when `include_edges` is set, assembled in Python. C4 makes the envelope's
field names and per-list counts part of the contract, so the assembly
reproduces `rows` / `rows_total` / `truncated` / `cursor` / `edges` /
`edges_truncated` exactly.

**A recorded blocker, found by building it.** §6 #1 gives the projection as
"`to_json()`'s field list", and `NodeVersion.to_json()` emits ten fields —
including **`source` and `provenance_ref`**. §2.1's `NodeScan` schema has no
column for either, and neither does any columnar route: `versions_columnar`
drops them by design (D-069) and `props_for_vids` returns only the property
bag. The only route is per-identity object materialization, which is the leaf.

So this compilation is byte-identical on eight of ten fields and **cannot** be
byte-identical on the other two. The equivalence receipt records exactly that,
and it is why this operator stays `leaf`: a compiled form that silently dropped
two payload fields would breach C4, and one that fetched them through the
adapter outside the algebra would not be a compilation at all. Adjudication
options — extend §2.1's schema, or drop the two fields from the operator's
payload (a C1/C4 change) — are both spec changes and neither is M3's to make.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.model import OPEN_END
from tgms.temporal.algebra import paginate
from tgms.tgir.eval import evaluate_core

from tgms.tgir.expr import Col
from tgms.tgir.node import EdgeScan, Endpoints, NodeScan, Order, Project, SortKey
from tgms.tgir.types import Sigma

#: This module is an operator *implementation*: `call_operator` has already run
#: `enforce_cost` with this operator's own `cost_fn` before dispatching here.
#: Re-admitting the expansion as a plan would add a second refusal point to a
#: frozen leaf and move where it refuses, which C5 forbids. The bypass is
#: labeled so the claim "already guarded" is visible rather than assumed.
LEAF_GUARDED = "leaf-guarded: call_operator enforced this operator's cost_fn (C5)"

#: `NodeVersion.to_json()`'s field list, minus the two §2.1 cannot express.
ROW_COLS = ("vid", "uid", "label", "vt_s", "vt_e", "tt_s", "tt_e", "props")

#: The two the compiled form cannot produce. Named so the receipt can be
#: explicit rather than approximate.
UNCOMPILABLE_COLS = ("source", "provenance_ref")

#: `_edge_rows`' field list (`ops_snapshot.py`) — the incident-edge shape.
EDGE_COLS = ("eid", "vid", "src", "dst", "rel_type", "vt_s", "vt_e")


def rows_plan(args: dict[str, Any]):
    """Root 1: the identity's believed node versions, in `(vt_s, vid)` order.

    That *is* the scan's own canonical order (§2.1), so unlike `version_history`
    this root needs no explicit `Order` — but one is written anyway, because
    relying on a scan's incidental order for an operator's contract is what
    M3.0's receipt caught on the edge side.
    """
    sigma = Sigma((Sigma.default().t_v[0],), args["as_of_tt"])
    scan = NodeScan("v", uids=(args["uid"],), belief="current", vt_mode="overlap",
                    sigma_=sigma)
    # Σ does not inherit: §3.1 says "a plan may declare Σ at its root; nodes
    # inherit it", but `node.py` gives every node its own field with an
    # OPEN_END default, and §3.5's no-widening check then rejects a plan whose
    # upper nodes were left at the default. So a compiled form threads Σ
    # explicitly through every node — recorded, because it is a real ergonomic
    # gap a plan author meets immediately.
    ordered = Order(scan, (SortKey(Col("v.vt_s")), SortKey(Col("v.vid"))),
                    sigma_=sigma)
    return Project(ordered, tuple((c, Col(f"v.{c}")) for c in ROW_COLS),
                   sigma_=sigma)


def edges_plan(args: dict[str, Any]):
    """Root 2: the edges incident to the identity, either role.

    `endpoints` is the incidence pushdown, and it selects each matching edge
    version **exactly once** — which is why §2.2 keeps it out of `Join`: a join
    against a uid list would multiply an edge with both endpoints in the cohort.
    """
    sigma = Sigma((Sigma.default().t_v[0],), args["as_of_tt"])
    scan = EdgeScan("e", endpoints=Endpoints("either", (args["uid"],)),
                    belief="current", vt_mode="overlap", sigma_=sigma)
    return Project(scan, tuple((c, Col(f"e.{c}")) for c in EDGE_COLS),
                   sigma_=sigma)


def run(adapter: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Evaluate both roots and assemble the payload."""
    rows = evaluate_core(rows_plan(args), adapter,
                          bypass_admission=LEAF_GUARDED).rows()
    for row in rows:
        row["vt_s"], row["vt_e"] = int(row["vt_s"]), int(row["vt_e"])
        row["tt_s"] = int(row["tt_s"])
        row["tt_e"] = int(row["tt_e"]) if row["tt_e"] is not None else OPEN_END
    out = paginate(rows, args["limit"], args["cursor"])

    if args["include_edges"]:
        edges = evaluate_core(edges_plan(args), adapter,
                               bypass_admission=LEAF_GUARDED).rows()
        for edge in edges:
            edge["vt_s"], edge["vt_e"] = int(edge["vt_s"]), int(edge["vt_e"])
        limit = args["limit"]
        # the leaf takes the first `limit` incident edges in scan order and
        # reports a separate truncation flag — there is no edge cursor
        out["edges"] = edges[:limit]
        out["edges_truncated"] = len(edges) > limit
    return out


def missing_columns(leaf_payload: dict[str, Any]) -> tuple[str, ...]:
    """Which of `UNCOMPILABLE_COLS` the leaf actually emitted — so a receipt
    states the divergence from the data rather than from this docstring."""
    for row in leaf_payload.get("rows", ()):
        return tuple(c for c in UNCOMPILABLE_COLS if c in row)
    return ()


_ = np

__all__ = ["EDGE_COLS", "ROW_COLS", "UNCOMPILABLE_COLS", "edges_plan",
           "missing_columns", "rows_plan", "run"]
