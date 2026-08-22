"""`run_plan` — a TGIR plan's result, in the same envelope an operator returns.

**Through the same functions, not a parallel implementation** (§5): `tt_q` /
`pinned` / `clamped` / `dependency` come from `ttq.envelope_metadata`, the scope
union from `scope_of` walked over the plan's nodes, the `tgir` sub-object from
the same `ResultMeta.to_json()` shape M2.2 built, and the payload through
`algebra._canonicalize_floats` and `digest`. A TGIR plan result and an operator
result are the same kind of object, so M4 inherits one envelope rather than two.

**`prop_coercion` finds its home here** (M3.0's open flag). §2.5 requires the
counts — "an answer must not rest on a shrunken denominator without saying so" —
and M3.0 could only record them per node digest on the `Execution`, because
there was no plan envelope to put them on. There is now: they ride in the `tgir`
sub-object, keyed by the node that produced them, and they are digest-excluded
like every other envelope field.
"""

from __future__ import annotations

from typing import Any

from tgms.core.model import digest
from tgms.temporal.algebra import _canonicalize_floats, paginate
from tgms.tgir.admission import admit, has_core_node
from tgms.tgir.eval import Execution
from tgms.tgir.plan import Plan
from tgms.tgir.propagate import summary
from tgms.tgir.prune import live_columns
from tgms.tgir.relation import Relation
from tgms.tgir.scope_of import ScopeBasis, scope_of
from tgms.tgir.ttq import basis_of, dependency_of


def run_plan(plan: Plan | Any, adapter: Any, *, tt_source: Any = None,
             cost_ceilings: dict[str, int] | None = None,
             limit: int = 1000, cursor: str | None = None,
             plan_id: str = "") -> dict[str, Any]:
    """Admit, execute and wrap a plan.

    `limit`/`cursor` cut the *output boundary* page — §2.12's only legitimate
    place for one — using `algebra.paginate`, so the cursor stays the plaintext
    decimal offset M2's C6 froze.
    """
    root = plan.root if isinstance(plan, Plan) else plan
    wrapped = plan if isinstance(plan, Plan) else Plan.of(root, plan_id)

    stats = adapter.stats()
    if has_core_node(root):
        admit(root, stats, wrapped.plan_digest, cost_ceilings)

    execution = Execution(adapter, live_columns(root), stats=stats,
                          plan_digest=wrapped.plan_digest, ceilings=cost_ceilings)
    relation = execution.run(root)

    payload = _canonicalize_floats(paginate(relation.rows(), limit, cursor))
    return {
        "op": "tgir_plan",
        "plan_id": wrapped.plan_id,
        "args_echo": {"plan_digest": wrapped.plan_digest, "limit": limit,
                      "cursor": cursor},
        "dataset_extent": {"vt_min": stats.get("vt_min"),
                           "vt_max": stats.get("vt_max")},
        **payload,
        **_freshness(root, adapter, tt_source),
        "tgir": _tgir(wrapped, root, relation, execution),
        "result_digest": digest(payload),
    }


def _freshness(root: Any, adapter: Any, tt_source: Any) -> dict[str, Any]:
    """`tt_q` / `pinned` / `clamped` / `dependency` for a whole plan.

    The scope is `scope_of` walked over the DAG — `leaf_scope ⊎ ⊎ ins`, with
    **every** node's scope entering the union including nodes whose rows never
    reach the answer (D13.14 prohibition 1). M3 changes nothing about that
    derivation; it consumes it.
    """
    basis: ScopeBasis = basis_of(adapter, root.sigma.t_b, tt_source)
    scope = scope_of(root, basis)
    return {"tt_q": basis.tt_q, "pinned": basis.pinned, "clamped": basis.clamped,
            "dependency": scope.to_json()}


def _tgir(plan: Plan, root: Any, relation: Relation,
          execution: Execution) -> dict[str, Any]:
    meta = execution.meta.get(root.node_digest)
    out: dict[str, Any] = {
        "plan_digest": plan.plan_digest,
        "node_digest": root.node_digest,
        "plan": plan.to_json(),
        "schema": relation.schema.to_json(),
        "rows_total": relation.n,
    }
    if meta is not None:
        out.update(summary(meta))
    if execution.coercion:
        # §2.5's disclosed denominator, keyed by the node that shrank it
        out["prop_coercion"] = dict(execution.coercion)
    return out


def dependency_for(root: Any, adapter: Any, tt_source: Any = None) -> dict[str, Any]:
    return dependency_of("tgir_plan", basis_of(adapter, root.sigma.t_b, tt_source),
                         None).to_json()


__all__ = ["dependency_for", "run_plan"]
