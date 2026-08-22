"""M2.3 — the Level-0 dependency scopes, tested against corrections.

A scope is only as good as the question "which writes can invalidate this
result?", so these tests ask exactly that: for each derived operator, a matrix
of corrections that **must** intersect its scope and corrections that **must
not**.

- A **must-intersect** case is a *soundness* claim: this write can change the
  answer, so a scope that misses it would let `check` return `FRESH` on a stale
  result. That is the one error class the contract forbids outright.
- A **must-not-intersect** case is a *precision* claim: this write provably
  cannot change the answer, and a scope that matched it would make the
  narrowing worthless. Widening is always sound (D13.1), so these are the
  assertions that would silently rot if the derivation were coarsened —
  nothing else in the tree can fail when precision is lost.

`intersects` and the footprint derivation live **here**, not in `tgms/`: M4
owns `check()` and `CorrectionFootprint`, and M2 is explicitly not to ship
them. Transcribing D13.20–D13.23 into the test also makes this an independent
implementation rather than a mirror of the production code — the scope is the
only thing under test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tgms.core.model import OPEN_END
from tgms.temporal.algebra import ensure_all_registered, validate_args
from tgms.tgir.depscope import TOP, ScopeTerm
from tgms.tgir.leaf import sigma_for
from tgms.tgir.leaves import LEAF_SCOPES, terms_for

# ---------------------------------------------------------------------------
# the test's own oracle: D13.20-D13.23, transcribed
# ---------------------------------------------------------------------------

STAR = "*"


@dataclass(frozen=True)
class FP:
    """One `CorrectionOpFootprint` arm (D13.20). `arm` is diagnostic only."""

    kind: str
    entity_kind: str
    identity: dict[str, Any]
    rel_type: Any
    vt: Any
    props: Any
    arm: str = "value"


def vt_closed(vt_s: int, vt_e: int) -> tuple[tuple[int, int], ...]:
    """D13.21: closed at the right, because a carve fragment can start exactly
    at the op's `vt_e`."""
    return ((vt_s, min(vt_e + 1, OPEN_END)),)


def vt_from(t: int) -> tuple[tuple[int, int], ...]:
    return ((t, OPEN_END),)


def _edge_identity(ref: dict[str, Any]) -> dict[str, Any]:
    return {"eid": f"{ref['src']}|{ref['dst']}|{ref['rel_type']}|{ref.get('disc', '')}",
            "src": ref["src"], "dst": ref["dst"], "rel_type": ref["rel_type"],
            "disc": ref.get("disc", "")}


def footprints(op: dict[str, Any]) -> list[FP]:
    """D13.22's table. Every `assert`/`correct`/`retract` emits **two**
    footprints — a value arm and an unbounded carve arm (D13.21a);
    `ingest_events` emits two value arms and no carve arm, because it
    supersedes nothing."""
    kind = op["op"]
    if kind == "assert_node":
        value = FP(kind, "node", {"uid": op["uid"]}, None,
                   vt_closed(op["vt_s"], op["vt_e"]), STAR)
    elif kind == "assert_edge":
        value = FP(kind, "edge", _edge_identity(op), op["rel_type"],
                   vt_closed(op["vt_s"], op["vt_e"]), STAR)
    elif kind == "correct":
        ref = op["ref"]
        keys = tuple(op.get("props", {}))
        if ref["kind"] == "node":
            value = FP(kind, "node", {"uid": ref["uid"]}, None,
                       vt_closed(op["vt_s"], op["vt_e"]),
                       keys + ("@label", "@extent", "@event_key"))
        else:
            value = FP(kind, "edge", _edge_identity(ref), ref["rel_type"],
                       vt_closed(op["vt_s"], op["vt_e"]),
                       keys + ("@extent", "@event_key"))
    elif kind == "retract":
        ref = op["ref"]
        if ref["kind"] == "node":
            value = FP(kind, "node", {"uid": ref["uid"]}, None, vt_from(op["t"]),
                       ("@extent", "@event_key"))
        else:
            value = FP(kind, "edge", _edge_identity(ref), ref["rel_type"],
                       vt_from(op["t"]), ("@extent", "@event_key"))
    elif kind == "ingest_events":
        events = op["events"]
        lo = min(e["vt_s"] for e in events)
        hi = max(e.get("vt_e", e["vt_s"] + 1) for e in events)
        edge_arm = FP(kind, "edge",
                      {"src": tuple(e["src"] for e in events),
                       "dst": tuple(e["dst"] for e in events)},
                      tuple(e["rel_type"] for e in events), vt_closed(lo, hi), STAR)
        node_arm = FP(kind, "node",
                      {"uid": tuple({e["src"] for e in events} | {e["dst"] for e in events})},
                      None, vt_closed(lo, OPEN_END),
                      ("@identity", "@extent", "@event_key"))
        return [edge_arm, node_arm]           # no carve arm: it supersedes nothing
    else:
        raise AssertionError(f"unmodelled op kind: {kind}")

    carve = FP(value.kind, value.entity_kind, value.identity, value.rel_type,
               STAR, ("@recut", "@version"), arm="carve")
    return [value, carve]


def as_set(value: Any) -> set:
    if isinstance(value, (tuple, list, set)):
        return set(value)
    return {value}


def meets(a: Any, b: Any) -> bool:
    """D13.23's primitive: **every field on both sides is a set**, a scalar is
    its singleton, `"*"` is the universe. There is no scalar membership
    operator, because the coarsened arms carry lists and a scalar test silently
    returns false against them — a false negative, therefore unsound (CO-8)."""
    if a is TOP or a == STAR or b is TOP or b == STAR:
        return True
    return bool(as_set(a) & as_set(b))


def vt_overlaps(a: Any, b: Any) -> bool:
    """Plain half-open overlap, with no adjustment on either side — D13.21
    already did it. `"*"` on either side overlaps everything, which is how the
    carve arm reaches a narrow-`vt` scope."""
    if a is TOP or a == STAR or b is TOP or b == STAR:
        return True
    return any(x[0] < y[1] and y[0] < x[1] for x in a for y in b)


def incident_match(incident: Any, fp: FP) -> bool:
    if incident is None:
        return False
    if incident is TOP:
        return True
    uids, role = incident.uids, incident.role
    src, dst = fp.identity.get("src"), fp.identity.get("dst")
    if role == "src":
        return meets(uids, src)
    if role == "dst":
        return meets(uids, dst)
    if role == "either":
        return meets(uids, src) or meets(uids, dst)
    if role == "both":
        return meets(uids, src) and meets(uids, dst)
    raise AssertionError(f"unrecognized role {role!r} must be UNDECIDABLE, never false")


def targets_match(targets: Any, fp: FP) -> bool:
    if targets is TOP:
        return True
    if fp.entity_kind == "node":
        # an absent arm is ∅: a scope with only an edge arm never matches a
        # node write, which is the whole of §2.0's first obligation
        return False if targets.nodes is None else meets(targets.nodes, fp.identity["uid"])
    return (targets.edges is not None and meets(targets.edges, fp.identity.get("eid"))) \
        or incident_match(targets.incident, fp)


def intersects(term: ScopeTerm, fp: FP) -> bool:
    """D13.23's five decidable conjuncts."""
    return (meets(term.kinds, fp.kind)
            and targets_match(term.targets, fp)
            and (fp.entity_kind != "edge" or meets(term.rel_types, fp.rel_type))
            and vt_overlaps(term.vt, fp.vt)
            and meets(term.props, fp.props))


def hits(terms: tuple[ScopeTerm, ...], op: dict[str, Any]) -> bool:
    """`terms` is a **disjunction**, and each footprint arm is matched
    separately (D13.23, property 4): a term that matches either arm fires."""
    return any(intersects(t, fp) for fp in footprints(op) for t in terms)


def arms_that_hit(terms: tuple[ScopeTerm, ...], op: dict[str, Any]) -> list[str]:
    return [fp.arm for fp in footprints(op) if any(intersects(t, fp) for t in terms)]


def scope(op: str, args: dict[str, Any]) -> tuple[ScopeTerm, ...]:
    ensure_all_registered()
    filled = validate_args(op, dict(args))
    return terms_for(op, filled, sigma_for(op, filled))


# ---------------------------------------------------------------------------
# the ops the matrix is written against
# ---------------------------------------------------------------------------

def assert_node(uid: str, vt_s: int = 0, vt_e: int = 100) -> dict[str, Any]:
    return {"op": "assert_node", "uid": uid, "vt_s": vt_s, "vt_e": vt_e}


def assert_edge(src: str, dst: str, rel_type: str = "R",
                vt_s: int = 0, vt_e: int = 100) -> dict[str, Any]:
    return {"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel_type,
            "disc": "", "vt_s": vt_s, "vt_e": vt_e}


def correct_node(uid: str, props: dict | None = None,
                 vt_s: int = 0, vt_e: int = 100) -> dict[str, Any]:
    return {"op": "correct", "ref": {"kind": "node", "uid": uid},
            "props": props or {"w": 1}, "vt_s": vt_s, "vt_e": vt_e}


def correct_edge(src: str, dst: str, rel_type: str = "R", props: dict | None = None,
                 vt_s: int = 0, vt_e: int = 100) -> dict[str, Any]:
    return {"op": "correct", "ref": {"kind": "edge", "src": src, "dst": dst,
                                     "rel_type": rel_type, "disc": ""},
            "props": props or {"w": 1}, "vt_s": vt_s, "vt_e": vt_e}


def retract_edge(src: str, dst: str, t: int, rel_type: str = "R") -> dict[str, Any]:
    return {"op": "retract", "ref": {"kind": "edge", "src": src, "dst": dst,
                                     "rel_type": rel_type, "disc": ""}, "t": t}


def retract_node(uid: str, t: int) -> dict[str, Any]:
    return {"op": "retract", "ref": {"kind": "node", "uid": uid}, "t": t}


def ingest(src: str, dst: str, vt_s: int, rel_type: str = "R") -> dict[str, Any]:
    return {"op": "ingest_events",
            "events": [{"src": src, "dst": dst, "rel_type": rel_type, "vt_s": vt_s}]}


# ---------------------------------------------------------------------------
# §9.1 — entity_history
# ---------------------------------------------------------------------------

EH = {"uid": "u1"}
EH_EDGES = {"uid": "u1", "include_edges": True}

ENTITY_HISTORY_MATRIX = [
    # (label, args, op, must_intersect)
    ("a node write to the identity", EH, assert_node("u1"), True),
    ("a correction to the identity", EH, correct_node("u1"), True),
    ("a retraction of the identity", EH, retract_node("u1", 50), True),
    # §9.1's surprise: an edge op *mentioning* uid registers a dense id, which
    # flips the operator's outcome from E_NOT_FOUND to an empty result — even
    # with include_edges false, and even though no node version is written
    ("an edge asserted FROM the identity (the 𝒟 case)", EH,
     assert_edge("u1", "u9"), True),
    ("an edge asserted TO the identity (the 𝒟 case)", EH,
     assert_edge("u9", "u1"), True),
    ("events ingested naming the identity", EH, ingest("u1", "u9", 10), True),
    # the operator IS carve-reachable: its rows carry props and vid
    ("a carve on the identity's node version", EH, correct_node("u1"), True),
    # precision
    ("a node write to another identity", EH, assert_node("u2"), False),
    ("a correction to another identity", EH, correct_node("u2"), False),
    ("an edge between two other identities", EH, assert_edge("u7", "u8"), False),
    # the 𝒟 term admits only @identity, so an edge *correction* cannot reach a
    # result that reads no edges — this is what the three-term split buys
    ("an edge correction incident to it, without include_edges", EH,
     correct_edge("u1", "u9"), False),
    ("a retraction of an incident edge, without include_edges", EH,
     retract_edge("u1", "u9", 10), False),
    # ... and with include_edges the same writes are in the answer
    ("an edge correction incident to it, WITH include_edges", EH_EDGES,
     correct_edge("u1", "u9"), True),
    ("a retraction of an incident edge, WITH include_edges", EH_EDGES,
     retract_edge("u1", "u9", 10), True),
    ("an unrelated edge, even WITH include_edges", EH_EDGES,
     assert_edge("u7", "u8"), False),
]


@pytest.mark.parametrize("label,args,op,must", ENTITY_HISTORY_MATRIX,
                         ids=[m[0] for m in ENTITY_HISTORY_MATRIX])
def test_entity_history_scope(label, args, op, must):
    terms = scope("entity_history", args)
    assert hits(terms, op) is must, (
        f"entity_history{args}: {label} should "
        f"{'intersect' if must else 'NOT intersect'}; arms hit: "
        f"{arms_that_hit(terms, op)}")


def test_entity_history_is_carve_reachable_by_design():
    """§9.1: `P = ⊤` because rows are `to_json()` of the version, `props` and
    `vid` included. Costless here — `V` is already `"*"`, so there is no window
    for the carve arm to take away."""
    terms = scope("entity_history", EH)
    assert "carve" in arms_that_hit(terms, correct_node("u1"))
    assert all(t.vt is TOP for t in terms)


# ---------------------------------------------------------------------------
# §9.4 — neighborhood_evolution
# ---------------------------------------------------------------------------

NE = {"uid": "u1", "t1": 10, "t2": 20}

NEIGHBORHOOD_MATRIX = [
    ("an incident edge asserted inside the window", NE,
     assert_edge("u1", "u9", vt_s=15, vt_e=16), True),
    ("an incident edge retracted inside the window", NE,
     retract_edge("u1", "u9", 12), True),
    ("an incident edge corrected across the window's end", NE,
     correct_edge("u1", "u9", vt_s=19, vt_e=25), True),
    ("events ingested on the identity inside the window", NE,
     ingest("u1", "u9", 15), True),
    ("an incident edge whose interval spans the window", NE,
     assert_edge("u9", "u1", vt_s=0, vt_e=100), True),
    # precision claims
    ("a node write to the very identity", NE, assert_node("u1"), False),
    ("a correction to the identity's node version", NE, correct_node("u1"), False),
    ("an edge between two other identities", NE, assert_edge("u7", "u8"), False),
    ("an incident edge asserted after the window", NE,
     assert_edge("u1", "u9", vt_s=30, vt_e=40), False),
    ("an incident edge asserted before the window", NE,
     assert_edge("u1", "u9", vt_s=0, vt_e=5), False),
]


@pytest.mark.parametrize("label,args,op,must", NEIGHBORHOOD_MATRIX,
                         ids=[m[0] for m in NEIGHBORHOOD_MATRIX])
def test_neighborhood_evolution_scope(label, args, op, must):
    terms = scope("neighborhood_evolution", args)
    assert hits(terms, op) is must, (
        f"neighborhood_evolution: {label} should "
        f"{'intersect' if must else 'NOT intersect'}; arms hit: "
        f"{arms_that_hit(terms, op)}")


def test_neighborhood_evolution_survives_the_carve_arm():
    """The phase's most valuable precision claim (L9.1).

    A pure re-cut of an incident edge — the carve arm, `vt = "*"`,
    `props = {@recut, @version}` — must **not** intersect, or `V = [t1, t2+1)`
    buys nothing: the arm's `"*"` overlaps every window, so the only thing that
    can exclude it is `P`. Splitting a believed interval leaves
    `#{versions with vt_s <= b < vt_e}` unchanged at every instant `b`, and
    instant counts are exactly what this operator computes.

    The *value* arm of that same correction still fires when it overlaps the
    window, so a correction that actually changes coverage is caught. Only the
    carving half is excluded.
    """
    terms = scope("neighborhood_evolution", NE)
    op = correct_edge("u1", "u9", vt_s=12, vt_e=14)
    assert arms_that_hit(terms, op) == ["value"], "the carve arm must not reach L9.1"
    # a correction entirely outside the window reaches neither arm
    assert arms_that_hit(terms, correct_edge("u1", "u9", vt_s=40, vt_e=50)) == []


def test_neighborhood_evolution_narrows_kinds_for_real():
    """`K = ℰ` is four of five wire kinds — a genuine narrowing, unlike the
    node-touching scans whose `𝒩 ∪ 𝒟` canonicalizes to `"*"`."""
    (term,) = scope("neighborhood_evolution", NE)
    assert term.kinds is not TOP
    assert set(term.kinds) == {"assert_edge", "correct", "retract", "ingest_events"}
    assert "assert_node" not in term.kinds
    assert term.vt == ((10, 21),) and term.vt_mode == "instant"


# ---------------------------------------------------------------------------
# §9.7 — aggregate_events
# ---------------------------------------------------------------------------

W = {"t_a": 0, "t_b": 50}
AE = {"group_by": [], "aggregates": [{"agg": "count"}], "window": W}
AE_RELS = {**AE, "rel_types": ["MSG"]}
AE_COHORT = {**AE, "endpoint_filter": {"role": "src", "uids": ["u1", "u2"]}}
AE_LABEL = {"group_by": [{"dim": "label", "role": "src"}],
            "aggregates": [{"agg": "count"}], "window": W}
AE_DURATION = {"group_by": [], "aggregates": [{"agg": "max", "of": "duration"}],
               "window": W}
AE_VT_S = {"group_by": [], "aggregates": [{"agg": "max", "of": "vt_s"}], "window": W}

AGGREGATE_MATRIX = [
    ("an edge event inside the window", AE, assert_edge("u1", "u2", vt_s=10, vt_e=11),
     True),
    ("a correction inside the window", AE,
     correct_edge("u1", "u2", vt_s=10, vt_e=20), True),
    ("a retraction inside the window", AE, retract_edge("u1", "u2", 5), True),
    ("events ingested inside the window", AE, ingest("u1", "u2", 10), True),
    # precision claims
    ("an edge event after the window", AE,
     assert_edge("u1", "u2", vt_s=60, vt_e=70), False),
    ("a node write, with no label dimension", AE, assert_node("u1"), False),
    ("a node correction, with no label dimension", AE, correct_node("u1"), False),
    # rel_types is consulted only for edge footprints (D13.23, property 1)
    ("an event of another relation type", AE_RELS,
     assert_edge("u1", "u2", rel_type="OTHER", vt_s=10, vt_e=11), False),
    ("an event of the named relation type", AE_RELS,
     assert_edge("u1", "u2", rel_type="MSG", vt_s=10, vt_e=11), True),
    # the endpoint cohort
    ("an event from a cohort member", AE_COHORT,
     assert_edge("u1", "u9", vt_s=10, vt_e=11), True),
    ("an event from outside the cohort", AE_COHORT,
     assert_edge("u7", "u8", vt_s=10, vt_e=11), False),
    ("an event INTO the cohort, which role=src does not cover", AE_COHORT,
     assert_edge("u7", "u1", vt_s=10, vt_e=11), False),
    # FF-3: the label dimension makes the answer a function of node state
    ("an endpoint relabelled, WITH a label dimension", AE_LABEL,
     assert_node("B", vt_s=10, vt_e=20), True),
    ("an endpoint relabelled, without a label dimension", AE,
     assert_node("B", vt_s=10, vt_e=20), False),
    ("a node correction, WITH a label dimension", AE_LABEL,
     correct_node("B", vt_s=10, vt_e=20), True),
    ("a node write outside the window, WITH a label dimension", AE_LABEL,
     assert_node("B", vt_s=60, vt_e=70), False),
]


@pytest.mark.parametrize("label,args,op,must", AGGREGATE_MATRIX,
                         ids=[m[0] for m in AGGREGATE_MATRIX])
def test_aggregate_events_scope(label, args, op, must):
    terms = scope("aggregate_events", args)
    assert hits(terms, op) is must, (
        f"aggregate_events: {label} should "
        f"{'intersect' if must else 'NOT intersect'}; arms hit: "
        f"{arms_that_hit(terms, op)}")


def test_aggregate_events_keeps_its_window_against_the_carve_arm():
    """Gate Appendix A.3's payoff, and the reason `P = Pᵥ` rather than `⊤`.

    An event-keyed operator is sensitive to carving only through `@event_key`,
    which the **value** arm carries and bounds. Naming `@recut` instead would
    hand the carve arm's `vt = "*"` an overlap with every window, and
    `V = [t_a, t_b)` would stop meaning anything.
    """
    terms = scope("aggregate_events", AE)
    assert arms_that_hit(terms, correct_edge("u1", "u2", vt_s=10, vt_e=20)) == ["value"]
    assert "@event_key" in terms[0].props and "@recut" not in terms[0].props


def test_duration_aggregates_are_carve_reachable_and_lose_the_window():
    """RG-1, with §9.7's own scenario.

    An edge believed `[0,100)` gives `max_duration = 100` over `window =
    [0,20)`. A `correct` over `[50,60)` leaves `[0,50)` as the only in-window
    version and the answer becomes `50` — yet the value arm's `vt = [50,61)`
    misses `[0,20)` entirely. Only `@recut` catches it, so a `duration`
    aggregate must carry it: the call keeps `K`, `I` and `T` and loses `V`.

    `of: "vt_s"` does not, and neither do the sequence aggregates: an event key
    is bounded by the value arm.
    """
    window = {"t_a": 0, "t_b": 20}
    duration = {"group_by": [], "aggregates": [{"agg": "max", "of": "duration"}],
                "window": window}
    counting = {"group_by": [], "aggregates": [{"agg": "count"}], "window": window}
    op = correct_edge("u1", "u2", vt_s=50, vt_e=60)   # entirely outside the window

    assert arms_that_hit(scope("aggregate_events", duration), op) == ["carve"]
    assert arms_that_hit(scope("aggregate_events", counting), op) == []
    assert "@recut" in scope("aggregate_events", AE_DURATION)[0].props
    assert "@recut" not in scope("aggregate_events", AE_VT_S)[0].props
    for agg in ("max_gap", "max_in_window", "max_session_span"):
        args = {"group_by": [], "window": W,
                "aggregates": [{"agg": agg, **({"span": 5} if agg == "max_in_window"
                                               else {"gap": 5} if agg == "max_session_span"
                                               else {})}]}
        assert "@recut" not in scope("aggregate_events", args)[0].props, agg


def test_the_label_term_is_never_rel_type_restricted():
    """§9.7's box: `intersects` consults `rel_types` only for edge footprints,
    so a rel-type-restricted node term would be meaningless — or unsound if an
    implementer "fixed" it by consulting `rel_types` anyway."""
    terms = scope("aggregate_events", {**AE_LABEL, "rel_types": ["MSG"],
                                       "endpoint_filter": {"role": "src",
                                                           "uids": ["u1"]}})
    edge_term, node_term = terms
    assert edge_term.rel_types == ("MSG",)
    assert node_term.rel_types is TOP
    # and the node arm is not narrowed to the cohort: a `label` dimension may
    # name the opposite endpoint from the one `endpoint_filter` restricts
    assert node_term.targets.nodes is TOP
    assert hits(terms, assert_node("someone-else", vt_s=10, vt_e=20))


def test_an_empty_cohort_widens_rather_than_going_vacuous():
    """An empty `endpoint_filter.uids` is a legal argument meaning an empty
    population. Its answer is constant-empty, so precision is worthless — and
    `[]` is the one spelling D13.5 warns reads as "no member matches"."""
    terms = scope("aggregate_events", {**AE, "endpoint_filter": {"role": "either",
                                                                 "uids": []}})
    assert terms[0].targets.edges is TOP
    assert hits(terms, assert_edge("u7", "u8", vt_s=10, vt_e=11))


# ---------------------------------------------------------------------------
# the phase's own boundaries
# ---------------------------------------------------------------------------

def test_only_three_operators_are_derived_and_the_rest_stay_top():
    """The coordinator's scope cut, stated as a test so widening it is a
    deliberate act."""
    assert set(LEAF_SCOPES) == {"entity_history", "neighborhood_evolution",
                                "aggregate_events"}
    for op, args in (("version_history", {"kind": "node", "window": W}),
                     ("snapshot_subgraph", {"seeds": ["u1"], "t_valid": 10}),
                     ("diff_snapshots", {"t1": 10, "t2": 20}),
                     ("resolve_entities", {"query": "u1"})):
        (term,) = scope(op, args)
        assert (term.kinds, term.targets, term.rel_types, term.vt, term.props) == \
            (TOP, TOP, TOP, TOP, TOP), op


def test_rolling_an_operator_back_to_top_is_one_line(monkeypatch):
    """Rollback is per-operator and is never a correctness event: `"*"` admits
    everything the derivation admitted and more (D13.1)."""
    derived = scope("neighborhood_evolution", NE)
    monkeypatch.delitem(LEAF_SCOPES, "neighborhood_evolution")
    rolled_back = scope("neighborhood_evolution", NE)
    assert rolled_back[0].kinds is TOP and rolled_back[0].vt is TOP
    # everything the derived scope caught, the coarse one still catches
    for _label, _args, op, must in NEIGHBORHOOD_MATRIX:
        if must:
            assert hits(rolled_back, op), op
    assert derived != rolled_back


def test_a_partial_argument_set_falls_back_to_top():
    """A failed step still contributes its scope (D13.14 prohibition 3), and it
    has no resolved arguments to derive one from."""
    assert terms_for("entity_history", {}, sigma_for("entity_history", {}))[0].kinds is TOP
    assert terms_for("aggregate_events", {}, sigma_for("aggregate_events", {}))[0].vt is TOP


def test_the_derivations_agree_with_the_live_envelope():
    """`ttq.dependency_of` and `scope_of` must not drift apart: one derivation,
    two callers."""
    from tgms.tgir.node import OpaqueLeaf
    from tgms.tgir.scope_of import ScopeBasis, leaf_scope
    from tgms.tgir.ttq import dependency_of

    ensure_all_registered()
    filled = validate_args("entity_history", {"uid": "u1", "include_edges": True})
    basis = ScopeBasis(store="s", tt_q=1)
    leaf = OpaqueLeaf.build("entity_history", filled, ("rows",))
    assert leaf_scope(leaf, basis).canonical() == \
        dependency_of("entity_history", basis, filled).canonical()


# ---------------------------------------------------------------------------
# the precision claims, checked against the real kernels
# ---------------------------------------------------------------------------

def _write(adapter, ops, tt):
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


def _node(uid, label="N", props=None, vt_s=0, vt_e=100):
    return {"op": "assert_node", "uid": uid, "label": label, "props": props or {},
            "vt_s": vt_s, "vt_e": vt_e, "source": "i", "provenance_ref": None}


def _edge(src, dst, rel_type="R", props=None, vt_s=0, vt_e=100, disc=""):
    return {"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel_type,
            "props": props or {}, "vt_s": vt_s, "vt_e": vt_e, "disc": disc,
            "source": "i", "provenance_ref": None}


def _correct_edge(src, dst, props, vt_s, vt_e, rel_type="R"):
    return {"op": "correct", "ref": {"kind": "edge", "src": src, "dst": dst,
                                     "rel_type": rel_type, "disc": ""},
            "props": props, "vt_s": vt_s, "vt_e": vt_e,
            "source": "i", "provenance_ref": None}


@pytest.fixture()
def store():
    """A store the precision claims can be *observed* on, not just argued."""
    from .conftest import fresh_adapter

    ensure_all_registered()
    a = fresh_adapter()
    yield a
    a.close()


def _digest(adapter, op, args):
    from tgms.temporal.algebra import call_operator

    return call_operator(adapter, op, dict(args))["result_digest"]


def test_neighborhood_evolution_really_is_insensitive_to_node_writes(store):
    """`K = ℰ` is a claim about the kernel, so it is checked against the
    kernel: the operator does not gate on node validity, so a write to the very
    identity it is centred on cannot move its answer."""
    _write(store, [_node("u1"), _node("u9")], 1)
    _write(store, [_edge("u1", "u9", vt_s=10, vt_e=100)], 2)
    args = {"uid": "u1", "t1": 10, "t2": 20}
    before = _digest(store, "neighborhood_evolution", args)
    _write(store, [_node("u1", label="RELABELLED", props={"x": 9})], 3)
    assert _digest(store, "neighborhood_evolution", args) == before
    assert not hits(scope("neighborhood_evolution", args), assert_node("u1"))


def test_a_recut_outside_the_window_really_does_not_reach_it(store):
    """L9.1, observed. An incident edge believed `[10,100)` is corrected over
    `[40,50)` — entirely outside `[t1, t2+1)`. The value arm misses it by
    valid time and the carve arm is excluded by `P`, so the scope says the
    result cannot have changed. It has not."""
    _write(store, [_node("u1"), _node("u9")], 1)
    _write(store, [_edge("u1", "u9", props={"w": 1}, vt_s=10, vt_e=100)], 2)
    args = {"uid": "u1", "t1": 10, "t2": 20}
    before = _digest(store, "neighborhood_evolution", args)
    _write(store, [_correct_edge("u1", "u9", {"w": 2}, 40, 50)], 3)
    assert _digest(store, "neighborhood_evolution", args) == before
    assert arms_that_hit(scope("neighborhood_evolution", args),
                         correct_edge("u1", "u9", vt_s=40, vt_e=50)) == []


def test_the_window_and_the_duration_exception_observed_together(store):
    """§9.7's own scenario, run rather than quoted.

    An edge believed `[0,100)`, `window = [0,20)`. A `correct` over `[50,60)`
    leaves the **count** untouched — which is why `Pᵥ` may exclude the carve arm
    and keep the window — while `max_duration` moves from `100` to `50`, which
    is why a `duration` aggregate must add `@recut` and give the window up.
    One store, one correction, two verdicts.
    """
    from tgms.temporal.algebra import call_operator

    _write(store, [_node("u1"), _node("u2")], 1)
    _write(store, [_edge("u1", "u2", props={"w": 1}, vt_s=0, vt_e=100)], 2)
    window = {"t_a": 0, "t_b": 20}
    counting = {"group_by": [], "aggregates": [{"agg": "count"}], "window": window}
    duration = {"group_by": [], "aggregates": [{"agg": "max", "of": "duration"}],
                "window": window}
    count_before = call_operator(store, "aggregate_events", dict(counting))["rows"]
    duration_before = call_operator(store, "aggregate_events", dict(duration))["rows"]

    _write(store, [_correct_edge("u1", "u2", {"w": 2}, 50, 60)], 3)

    count_after = call_operator(store, "aggregate_events", dict(counting))["rows"]
    duration_after = call_operator(store, "aggregate_events", dict(duration))["rows"]
    assert count_after == count_before == [{"count": 1}]
    assert duration_before == [{"max_duration": 100}]
    assert duration_after == [{"max_duration": 50}]

    op = correct_edge("u1", "u2", vt_s=50, vt_e=60)
    assert arms_that_hit(scope("aggregate_events", counting), op) == []
    assert arms_that_hit(scope("aggregate_events", duration), op) == ["carve"]


def test_the_label_dimension_really_does_read_node_state(store):
    """FF-3, observed: the same relabel that leaves an endpoint-grouped count
    alone splits a label-grouped one. The second term is a soundness
    requirement, not a precision nicety."""
    from tgms.temporal.algebra import call_operator

    _write(store, [_node("u1"), _node("u9"), _node("B")], 1)
    _write(store, [_edge("u1", "u9", "MSG", vt_s=10, vt_e=11, disc="a"),
                   _edge("B", "u9", "MSG", vt_s=10, vt_e=11, disc="b")], 2)
    window = {"t_a": 0, "t_b": 50}
    by_endpoint = {"group_by": [{"dim": "endpoint", "role": "src"}],
                   "aggregates": [{"agg": "count"}], "window": window}
    by_label = {"group_by": [{"dim": "label", "role": "src"}],
                "aggregates": [{"agg": "count"}], "window": window}
    endpoint_before = _digest(store, "aggregate_events", by_endpoint)
    label_before = call_operator(store, "aggregate_events", dict(by_label))["rows"]

    _write(store, [_node("B", label="Bot")], 3)

    assert _digest(store, "aggregate_events", by_endpoint) == endpoint_before
    label_after = call_operator(store, "aggregate_events", dict(by_label))["rows"]
    assert label_after != label_before
    relabel = assert_node("B", vt_s=0, vt_e=100)
    assert not hits(scope("aggregate_events", by_endpoint), relabel)
    assert hits(scope("aggregate_events", by_label), relabel)
