"""Level-0 dependency scopes for the opaque leaves (FRESHNESS_SEMANTICS §9).

M2.1 gave every operator the coarse all-`"*"` term, which §5.5.4 constraint 1
makes explicitly legal. This module replaces it **per operator**, following
§9's rows. Under the coordinator's scope cut, three are derived — the three the
M2 plan's §7.2 orders first, because their derivations are short and their
value is highest — and the other twelve keep `"*"`.

**Rollback is one line and is never a correctness event.** Delete an entry from
`LEAF_SCOPES` below and that operator reverts to the constant `"*"`; by D13.1
widening is always sound, so a reverted derivation can cost precision and can
never cost correctness. Nothing else in the tree needs to change.

Two things a reader should not expect to find here:

- **`kinds` rarely narrows.** `𝒩 ∪ 𝒟` is all five wire kinds, and a kinds set
  naming all five *is* ⊤ (D13.5, one spelling), so any node-touching scan's
  `kinds` canonicalizes to `"*"`. The narrowing for those operators lives in
  `targets`, `vt` and `props` — writing an enumeration that means ⊤ would only
  look like precision. `ℰ` and `𝒩` alone are four of five and do narrow.
- **No interval adjustment.** A derivation copies Σ's window and stops
  (D13.6). The right-closure lives on the footprint side, in `vt_closed` /
  `vt_from`.
"""

from __future__ import annotations

from typing import Any, Callable

from tgms.tgir.depscope import (
    TOP, TOP_TERM, Incident, K_DENSE_ID, K_EDGE, K_NODE, ScopeTerm, Targets,
)
from tgms.tgir.types import Sigma

#: `Pᵥ` — the value-arm property vocabulary (§9.7, D13.7a). It names
#: `@identity`, `@extent` and `@event_key` and **not** `@recut`/`@version`,
#: which is exactly what keeps an event-keyed operator's window against the
#: carve arm.
#:
#: §9 writes `Pᵥ` as "⊤ over real property keys ∪ {…}", which the wire format
#: cannot encode: `props` is `"*"` (everything, pseudo-keys included) or a
#: list. Emitting the pseudo-keys alone matches **exactly the same footprints**,
#: because every footprint D13.22 derives either carries `"*"` (both asserts,
#: `ingest_events`' edge arm) or carries `@extent` beside its real keys
#: (`correct`, `retract`, `ingest_events`' node arm) — the sole exception being
#: the carve arm, which carries `{@recut, @version}` and is the one this
#: vocabulary is designed to exclude. So dropping the unencodable "real keys"
#: half changes no verdict.
P_VALUE: tuple[str, ...] = ("@identity", "@extent", "@event_key")

#: `@recut` promotes a term into the carve arm's reach: the arm carries
#: `vt = "*"`, so it overlaps any window (D13.21a).
P_CARVE_REACHED: tuple[str, ...] = P_VALUE + ("@recut",)


def _uid_list(args: dict[str, Any], key: str = "uid") -> tuple[str, ...] | None:
    uid = args.get(key)
    return (uid,) if isinstance(uid, str) and uid else None


# ---------------------------------------------------------------------------
# §9.1 — entity_history, the narrow anchor
# ---------------------------------------------------------------------------

def entity_history_terms(args: dict[str, Any], sigma: Sigma) -> tuple[ScopeTerm, ...]:
    """One identity, as a node **and** as an edge endpoint.

    `V = ⊤` because the operator takes no window: it returns the identity's
    whole believed version list, and §9.1 rejects narrowing `V` to the extent
    actually returned — a new version outside that extent is still a new row.
    `P = ⊤` because rows are `to_json()` of the version, `props` and `vid`
    included, which also makes the operator carve-reachable.

    **The incident arm is not optional, and not about `include_edges`.**
    `assert_edge` and `ingest_events` call `ensure_entities` for their
    endpoints, registering a dense id **without necessarily writing a node
    version** — so an edge op naming a previously unknown `uid` flips this
    operator's outcome from `E_NOT_FOUND` to an empty result. An outcome change
    is a change (§9.1's surprise, L13.3). A `nodes`-only term would admit `𝒟`
    in its first conjunct and never satisfy its second, because both `𝒟`
    members write *edge* footprints.

    **Three terms, not one** — the shape §13.6's worked example emits. Merging
    them into a single term with `kinds: "*"` and `props: "*"` would be sound
    (it is a superset) and measurably coarser: on the common
    `include_edges = false` call it would let *every* edge `correct` incident to
    `uid` invalidate a result that reads no edges, where the `𝒟` term admits
    only the one effect that can reach it — a dense id coming into existence,
    which is `@identity`.
    """
    uids = _uid_list(args)
    if uids is None:
        return (TOP_TERM,)
    terms = [
        # the node term: `𝒩`, which is four of the five wire kinds and so a
        # real narrowing — `assert_edge` is not in it
        ScopeTerm(kinds=K_NODE, targets=Targets(nodes=uids), rel_types=TOP,
                  vt=TOP, vt_mode="overlap", props=TOP),
        # the `𝒟` term: an edge op that merely *mentions* `uid` registers its
        # dense id, and that is the whole of its reach here
        ScopeTerm(kinds=K_DENSE_ID, targets=Targets(incident=Incident("either", uids)),
                  rel_types=TOP, vt=TOP, vt_mode="overlap", props=("@identity",)),
    ]
    if args.get("include_edges"):
        # now the incident edges are in the answer, so every effect on them is
        terms.append(ScopeTerm(
            kinds=K_EDGE, targets=Targets(incident=Incident("either", uids)),
            rel_types=TOP, vt=TOP, vt_mode="overlap", props=TOP))
    return tuple(terms)


# ---------------------------------------------------------------------------
# §9.4 — neighborhood_evolution, the second narrow anchor
# ---------------------------------------------------------------------------

def neighborhood_evolution_terms(args: dict[str, Any],
                                 sigma: Sigma) -> tuple[ScopeTerm, ...]:
    """Edges incident to one identity, over `[t1, t2+1)`.

    `K = ℰ` — a genuine four-of-five narrowing: node ops cannot change this
    answer, because unlike `snapshot_subgraph` the operator does not gate on
    node validity. Its outputs are neighbour **uids** and a degree count, read
    from *edge endpoints*, so it binds no node-version column and carries no
    `nodes` arm.

    `P` excludes `@recut`/`@version` under **L9.1 (carve neutrality for
    interval counts)**: splitting a believed interval into consecutive
    fragments leaves `#{versions with vt_s <= b < vt_e}` unchanged at every
    instant `b`, which is exactly what this operator computes. That exclusion
    is what keeps `V = [t1, t2+1)` worth having — with `P = ⊤` the carve arm's
    `vt = "*"` would overlap the window and the narrowing would buy nothing.

    L9.1 is about **instant** counts only. It is false for event counts keyed
    on `vt_s` (that is CE-5), so it must not be carried to `aggregate_events`.
    """
    uids = _uid_list(args)
    t1, t2 = args.get("t1"), args.get("t2")
    if uids is None or not isinstance(t1, int) or not isinstance(t2, int):
        return (TOP_TERM,)
    return (ScopeTerm(
        kinds=K_EDGE,
        targets=Targets(incident=Incident("either", uids)),
        rel_types=TOP,
        vt=((t1, t2 + 1),),
        vt_mode="instant",
        props=("@identity", "@extent"),
    ),)


# ---------------------------------------------------------------------------
# §9.7 — aggregate_events, the aggregate anchor
# ---------------------------------------------------------------------------

def aggregate_events_terms(args: dict[str, Any], sigma: Sigma) -> tuple[ScopeTerm, ...]:
    """One edge term, plus a node term when a `label` dimension is grouped on.

    The edge term is memo §17's *relation type × temporal interval × predicate
    scope* made precise: `K = ℰ`, `T = rel_types` or ⊤, `I =
    endpoint_filter` or ⊤, `V = window`, `P = Pᵥ`.

    **The `label` term (gate finding FF-3) is a soundness requirement, not a
    precision one.** A `label` dimension resolves each endpoint's label through
    `_labels_at` → `adapter.nodes_columnar`, so the answer is a function of node
    version state; a `K = ℰ` domain excludes `assert_node("B","Bot",…)`, which
    can change the group keys, the row count and the digest, and would fail
    `intersects`' *first* conjunct. Its `rel_types` **must** be `"*"`:
    `intersects` consults `rel_types` only for edge footprints, so a
    rel-type-restricted node term would be meaningless — or unsound if an
    implementer "fixed" it by consulting `rel_types` anyway.

    **The `duration` exception (RG-1).** `min`/`max`/`mean` with
    `of: "duration"` compute `vt_e − vt_s`, a function of *both* endpoints, so a
    refinement outside the window changes the answer: an edge believed
    `[0,100)` gives `max_duration = 100` over `window = [0,20)`; a `correct`
    over `[50,60)` leaves `[0,50)` in-window and the answer becomes `50`, while
    the value arm's `vt = [50,61)` misses `[0,20)`. Such a call adds `@recut` to
    `P` and is thereby carve-reachable: it keeps `K`, `I` and `T` and loses `V`.
    `of: "vt_s"` does not — an event key is bounded by the value arm — and the
    three sequence aggregates read only the `vt_s` array, so they stay `Pᵥ`.
    """
    window = args.get("window")
    if not isinstance(window, dict) or "t_a" not in window or "t_b" not in window:
        return (TOP_TERM,)
    vt = ((int(window["t_a"]), int(window["t_b"])),)

    rel_types = args.get("rel_types")
    rel = tuple(rel_types) if rel_types else TOP

    terms = [ScopeTerm(
        kinds=K_EDGE,
        targets=_endpoint_targets(args),
        rel_types=rel,
        vt=vt,
        vt_mode="event",
        props=P_CARVE_REACHED if _reads_duration(args) else P_VALUE,
    )]

    if _groups_on_label(args):
        terms.append(ScopeTerm(
            kinds=K_NODE,
            # §9.7 writes this arm as `endpoint_filter.uids ∪ "*"`, whose
            # literal reading is ⊤ — and ⊤ is also the only sound reading: a
            # `label` dimension may name the *opposite* endpoint from the one
            # `endpoint_filter` restricts, so the labels read are not confined
            # to the filtered uid set.
            targets=Targets(nodes=TOP),
            rel_types=TOP,        # never rel-type-restricted: see the docstring
            vt=vt,
            vt_mode="event",
            props=("@label", "@identity", "@extent"),
        ))
    return tuple(terms)


def _endpoint_targets(args: dict[str, Any]) -> Any:
    """`I` for the edge term: the cohort pre-filter, or ⊤.

    An **empty** `uids` list is a legal argument meaning an empty population
    (the schema says so), and its answer is constant-empty. Rather than emit a
    vacuous term for it, this widens to ⊤ — precision is worthless on an answer
    nothing can change, and `[]` is the one spelling D13.5 warns reads as "no
    member matches".
    """
    ep = args.get("endpoint_filter")
    if not isinstance(ep, dict) or not ep.get("uids"):
        return Targets(edges=TOP)
    return Targets(incident=Incident(ep["role"], tuple(ep["uids"])))


def _groups_on_label(args: dict[str, Any]) -> bool:
    return any(isinstance(d, dict) and d.get("dim") == "label"
               for d in (args.get("group_by") or []))


def _reads_duration(args: dict[str, Any]) -> bool:
    return any(isinstance(a, dict) and a.get("of") == "duration"
               for a in (args.get("aggregates") or []))


# ---------------------------------------------------------------------------
# the rollout table — one line per operator, and the rollback
# ---------------------------------------------------------------------------

Derivation = Callable[[dict[str, Any], Sigma], "tuple[ScopeTerm, ...]"]

#: **Delete a line to roll that operator back to `"*"`.** Every absent operator
#: takes the coarse default, which stays a valid v1 answer (§5.5.4 constraint 1)
#: and is a widening, never a correctness event (D13.1).
LEAF_SCOPES: dict[str, Derivation] = {
    "entity_history": entity_history_terms,
    "neighborhood_evolution": neighborhood_evolution_terms,
    "aggregate_events": aggregate_events_terms,
}

#: Does this operator's output bind **node-version** columns — a label, a
#: node's props, a node `vid` — as opposed to bare uids read off edge
#: endpoints? §2.0's first `targets`-shape obligation applies only to the
#: former, and `scripts/check_scope_shape.py` reads this table.
#:
#: Declared only for the operators with a derived scope: the rest carry ⊤
#: targets, which discharges the obligation trivially. `aggregate_events`
#: depends on its arguments — the `label` dimension is exactly what makes it
#: read node versions (FF-3).
BINDS_NODE_VERSIONS: dict[str, Callable[[dict[str, Any]], bool]] = {
    # rows are `to_json()` of node versions: label, props and vid
    "entity_history": lambda args: True,
    # neighbours are uids read off edge endpoints; the series is a count
    "neighborhood_evolution": lambda args: False,
    "aggregate_events": _groups_on_label,
}


def terms_for(op: str, args: dict[str, Any], sigma: Sigma) -> tuple[ScopeTerm, ...]:
    """The Level-0 terms for one bound call, or the coarse `"*"` term.

    A derivation that cannot read what it needs — an unvalidated or partial
    argument set, as when a *failed* step still has to contribute its scope
    (D13.14 prohibition 3) — falls back to ⊤ rather than guessing.
    """
    derive = LEAF_SCOPES.get(op)
    if derive is None:
        return (TOP_TERM,)
    return derive(args, sigma)


__all__ = [
    "BINDS_NODE_VERSIONS", "Derivation", "LEAF_SCOPES", "P_CARVE_REACHED",
    "P_VALUE", "aggregate_events_terms", "entity_history_terms",
    "neighborhood_evolution_terms", "terms_for",
]
