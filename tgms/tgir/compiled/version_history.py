"""Compiled `version_history` — §6 #2.

§6 #2 gives the compilation as "a scan under the window, projected to
`VERSION_COLS`". That is incomplete in one respect M3.0's scan-equivalence
receipt found and the coordinator has since ruled on:

> **The leaf orders by `(tt_s, vid)`** (`ops_versions.py:151`), while §2.2
> declares the core scan's canonical order as `(vt_s, vid)` under
> `belief = current`. Result ordering is part of the answer
> (`eval_semantics.md` §4), so the compiled form needs an **explicit
> `Order(tt_s, vid)`** — and `tt_s` is not on the columnar route, so the
> ordered form also forces the `versions_columnar` fallback, priced at
> `version_history`'s own measured 15,400 ms/M.

That price is not a regression: it is the same scan the leaf already performs.
The leaf reads `versions_columnar` too — which is why its coefficient is what
it is.

The projection is `VERSION_COLS[kind]`, which deliberately **drops `props`,
`source` and `provenance_ref`** (D-069), so unlike `entity_history` this
operator's whole payload is inside §2's scan schema.
"""

from __future__ import annotations

from typing import Any

from tgms.core.model import OPEN_END
from tgms.temporal.algebra import paginate
from tgms.tgir.eval import evaluate_core

from tgms.tgir.expr import Col
from tgms.tgir.node import EdgeScan, NodeScan, Order, Project, SortKey
from tgms.tgir.types import Sigma

#: This module is an operator *implementation*: `call_operator` has already run
#: `enforce_cost` with this operator's own `cost_fn` before dispatching here.
#: Re-admitting the expansion as a plan would add a second refusal point to a
#: frozen leaf and move where it refuses, which C5 forbids. The bypass is
#: labeled so the claim "already guarded" is visible rather than assumed.
LEAF_GUARDED = "leaf-guarded: call_operator enforced this operator's cost_fn (C5)"

#: Exactly the operator's own output columns, per kind.
VERSION_COLS = {
    "node": ("vid", "uid", "label", "vt_s", "vt_e", "tt_s", "tt_e"),
    "edge": ("vid", "eid", "src", "dst", "rel_type", "disc", "vt_s", "vt_e",
             "tt_s", "tt_e"),
}


def plan(args: dict[str, Any]):
    """The single-root plan. One `Order`, because the leaf's canonical order is
    not the scan's."""
    kind = args["kind"]
    window = args["window"]
    sigma = Sigma.in_window(window["t_a"], window["t_b"], as_of_tt=args["as_of_tt"])
    var = "v"
    if kind == "node":
        scan: Any = NodeScan(var, belief=args["belief"], vt_mode="overlap",
                             sigma_=sigma)
    else:
        rel_types = args.get("rel_types")
        scan = EdgeScan(var, rel_types=tuple(rel_types) if rel_types else None,
                        belief=args["belief"], vt_mode="overlap", sigma_=sigma)
    # Σ is threaded explicitly through every node: it does not inherit (§3.1's
    # "nodes inherit it" has no mechanism in `node.py`), and §3.5's
    # no-widening check rejects a plan that leaves an upper node at the default.
    ordered = Order(scan, (SortKey(Col(f"{var}.tt_s")), SortKey(Col(f"{var}.vid"))),
                    sigma_=sigma)
    return Project(ordered, tuple((c, Col(f"{var}.{c}")) for c in VERSION_COLS[kind]),
                   sigma_=sigma)


def run(adapter: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Evaluate and assemble the operator's payload.

    Pagination is `algebra.paginate` — the same plaintext decimal offsets M2's
    C6 froze — applied at the plan's output boundary, which is where §2.12 puts
    a page cut.
    """
    relation = evaluate_core(plan(args), adapter,
                              bypass_admission=LEAF_GUARDED)
    rows = relation.rows()
    for row in rows:
        # the censoring rule the scan already applied, restated as the
        # operator's own int conversion: a belief that ended after T_b had not
        # ended yet
        row["tt_e"] = int(row["tt_e"]) if row["tt_e"] is not None else OPEN_END
        row["tt_s"] = int(row["tt_s"])
        row["vt_s"], row["vt_e"] = int(row["vt_s"]), int(row["vt_e"])
    return paginate(rows, args["limit"], args["cursor"])


__all__ = ["VERSION_COLS", "plan", "run"]
