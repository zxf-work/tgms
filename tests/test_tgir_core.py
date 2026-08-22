"""M2.0 — the TGIR core module: node/Σ/schema types, the completeness lattice,
and the `DependencyScope` wire format.

These tests cover `tgms/tgir/` only. Nothing in that package is wired into the
live call path, so nothing here touches a store, an adapter or an operator
kernel — which is the phase's whole point: the four existing suites do not
import the code under test.

References are to `docs/design/TGIR_SPEC.md` (FROZEN) and
`docs/design/FRESHNESS_SEMANTICS.md` §13.
"""

from __future__ import annotations

import json

import pytest

from tgms.core.errors import InvalidArgError, StateError
from tgms.core.model import OPEN_END, Interval, canonical_json
from tgms.tgir import (
    Agg, Aggregate, Bounded, Checkpoint, Cmp, Col, Column, Completeness,
    DependencyScope, EdgePat, EdgeScan, Endpoints, Exact, Exactness, Expand,
    Filter, Incident, Join, Limit, Lit, MIDDLE, NodePat, NodeScan, NullAdapter,
    OpaqueLeaf, Order, Pattern, PatternMatch, Project, PropertyPredicate,
    Provenance, ResultMeta, Schema, ScopeBasis, ScopeTerm, Sigma, SortKey,
    Source, TOP, TOP_TERM, Targets, Tau, TupleExpr, TypeConstraint, Unbounded,
    adapter_for, anchor_of_var, comparable, compute_node_digest, le, leaf_scope,
    meet, meet_all, meet_exactness, union_all, vt_closed, vt_from,
)
# `tgms.tgir.scope_of` is the *module*; the function of the same name is not
# re-exported at package level, because binding it there would shadow the
# module and make `tgms.tgir.scope_of.leaf_scope` an AttributeError.
from tgms.tgir.scope_of import scope_of
from tgms.tgir.depscope import FULL_SCAN_CHECKPOINTS, UNANCHORED

BASIS = ScopeBasis(store="store-a", tt_q=1_000, checkpoints=(Checkpoint(500, "aa" * 8),))


# ---------------------------------------------------------------------------
# §4.1 / §4.2 — types and schemas
# ---------------------------------------------------------------------------

def test_tau_rejects_unknown_base_and_renders_canonically():
    with pytest.raises(InvalidArgError):
        Tau("list")
    assert Tau("int").optional().to_json() == "int?"
    assert Tau.tuple_of(Tau("uid"), Tau("int")).to_json() == "tuple(uid,int)"
    with pytest.raises(InvalidArgError):
        Tau("tuple")  # a tuple needs components


def test_schema_collision_is_a_static_plan_error():
    a = Schema.of(Column("p.uid", Tau("uid")))
    with pytest.raises(InvalidArgError):
        a.concat(Schema.of(Column("p.uid", Tau("uid"))))
    with pytest.raises(InvalidArgError):
        Schema.of(Column("x", Tau("int")), Column("x", Tau("int")))


def test_sigma_default_and_narrowing():
    s = Sigma.default()
    assert s.t_v == (Interval(0, OPEN_END),) and s.t_b == OPEN_END
    narrowed = s.narrow(Interval(10, 20))
    assert narrowed.t_v == (Interval(10, 20),)
    # §3.5: a node may narrow T_v and may never widen it
    with pytest.raises(InvalidArgError):
        narrowed.narrow(Interval(5, 30))
    assert Sigma.at_instant(7).t_v == (Interval(7, 8),)
    # the diff_snapshots shape — the one leaf whose Σ is a pair of instants
    pair = Sigma.at_instants(3, 9)
    assert pair.vt_json() == [[3, 4], [9, 10]]
    assert pair.is_instant


def test_input_sigma_may_not_widen_its_consumers():
    inner = NodeScan("p", sigma_=Sigma.default())
    with pytest.raises(InvalidArgError):
        Filter(inner, Lit(True), sigma_=Sigma.in_window(0, 10))


# ---------------------------------------------------------------------------
# §2 — construction and validation of every node type
# ---------------------------------------------------------------------------

def test_node_scan_binds_its_variable_and_emits_node_columns():
    scan = NodeScan("p", labels=("Person",), uids=("u1",))
    assert scan.out_schema.names == (
        "p.uid", "p.vid", "p.label", "p.vt_s", "p.vt_e", "p.tt_s", "p.tt_e", "p.props")
    assert scan.reads_store
    assert scan.canonical_order == "(vt_s, vid)"
    assert NodeScan("p", belief="all").canonical_order == "(tt_s, vid)"
    with pytest.raises(InvalidArgError):
        NodeScan("")
    with pytest.raises(InvalidArgError):
        NodeScan("p", belief="maybe")
    with pytest.raises(InvalidArgError):
        NodeScan("p", labels=())  # [] matches nothing; omit it instead


def test_edge_scan_endpoints_carry_the_four_role_enum():
    scan = EdgeScan("e", rel_types=("TRUST",), endpoints=Endpoints("both", ("u1", "u2")))
    assert scan.out_schema.names[:4] == ("e.eid", "e.vid", "e.src", "e.dst")
    with pytest.raises(InvalidArgError):
        Endpoints("either_or", ("u1",))
    with pytest.raises(InvalidArgError):
        Endpoints("src", ())


def test_expand_hop_forms_and_their_schema_consequences():
    seed = NodeScan("p", uids=("u1",))
    one = Expand(seed, "p", "q", Exact(1), edge_var="e")
    assert "q.uid" in one.out_schema and "e.eid" in one.out_schema
    assert "q.depth" not in one.out_schema

    var = Expand(seed, "p", "q", Bounded(1, 3))
    assert "q.depth" in var.out_schema
    assert var.out_schema.tau_of("q.depth") == Tau("int")

    # no edge bindings for variable-length forms — v1 has no list type
    with pytest.raises(InvalidArgError):
        Expand(seed, "p", "q", Unbounded(1), edge_var="e")
    # exact(0) traverses no edge
    with pytest.raises(InvalidArgError):
        Expand(seed, "p", "q", Exact(0), edge_var="e")
    with pytest.raises(InvalidArgError):
        Expand(seed, "nope", "q", Exact(1))
    with pytest.raises(InvalidArgError):
        Expand(seed, "p", "q", Exact(1), dir="sideways")
    with pytest.raises(InvalidArgError):
        Exact(-1)
    with pytest.raises(InvalidArgError):
        Bounded(3, 1)
    with pytest.raises(InvalidArgError):
        Unbounded(2)


def test_bounded_k_k_is_not_normalized_to_exact_k():
    """§2.3 / §8.7: `exact(k)` is a walk relation, `bounded` a node relation.
    They differ whenever a target is reachable by several k-walks."""
    seed = NodeScan("p", uids=("u1",))
    assert Expand(seed, "p", "q", Bounded(2, 2)).out_schema != \
        Expand(seed, "p", "q", Exact(2)).out_schema


def test_filter_type_checks_its_predicate():
    scan = NodeScan("p")
    Filter(scan, Cmp("<", Col("p.vt_s"), Lit(5)))
    with pytest.raises(InvalidArgError):
        Filter(scan, Col("p.uid"))  # not boolean
    with pytest.raises(InvalidArgError):
        Filter(scan, Cmp("<", Col("p.nope"), Lit(5)))  # unbound column


def test_property_predicate_and_type_constraint_validate_their_variable():
    scan = NodeScan("p")
    PropertyPredicate(scan, "p", "firstName", "=", "Ada")
    TypeConstraint(scan, "p", labels=("Post", "Comment"))
    with pytest.raises(InvalidArgError):
        PropertyPredicate(scan, "q", "firstName", "=", "Ada")
    with pytest.raises(InvalidArgError):
        PropertyPredicate(scan, "p", "firstName", "~", "Ada")
    with pytest.raises(InvalidArgError):
        TypeConstraint(scan, "p")  # neither labels nor rel_type
    with pytest.raises(InvalidArgError):
        TypeConstraint(scan, "p", labels=("Post",), rel_type="KNOWS")


def test_project_keep_modes_and_the_one_aggregate_column_rule():
    scan = NodeScan("p")
    listed = Project(scan, (("who", Col("p.uid")),))
    assert listed.out_schema.names == ("who",)
    kept = Project(scan, (("who", Col("p.uid")),), keep="all")
    assert kept.out_schema.names[-1] == "who" and "p.vid" in kept.out_schema

    agg = Aggregate(scan, (("k", Col("p.label")),),
                    (Agg("count", "n"), Agg("sum", "s", Col("p.vt_s"))))
    Project(agg, (("scaled", Cmp(">", Col("n"), Lit(2))),))
    with pytest.raises(InvalidArgError):
        # arithmetic over two aggregate output columns — beyond v1 (R2's boundary)
        Project(agg, (("ratio", Cmp(">", Col("n"), Col("s"))),))


def test_join_types_and_key_compatibility():
    left = Project(NodeScan("p"), (("k", Col("p.uid")),))
    right = Project(NodeScan("q"), (("k2", Col("q.uid")),))
    inner = Join(left, right, (("k", "k2"),))
    assert inner.out_schema.names == ("k", "k2")
    outer = Join(left, right, (("k", "k2"),), join_type="left_outer")
    assert outer.out_schema.tau_of("k2").nullable
    anti = Join(left, right, (("k", "k2"),), join_type="anti")
    assert anti.out_schema.names == ("k",)  # the probe contributes no columns

    bad = Project(NodeScan("r"), (("k3", Col("r.vt_s")),))
    with pytest.raises(InvalidArgError):
        Join(left, bad, (("k", "k3"),))
    with pytest.raises(InvalidArgError):
        Join(left, right, (("k", "k2"),), join_type="full_outer")
    with pytest.raises(InvalidArgError):
        Join(left, right, ())


def test_pattern_match_validation_and_schema():
    pattern = Pattern((NodePat("a", "Person"), NodePat("b")),
                      (EdgePat("e1", "a", "b", "KNOWS"),))
    pm = PatternMatch(pattern)
    assert pm.out_schema.names[0] == "a.uid"
    assert "a.tt_s" not in pm.out_schema  # node variables expose no tt pair
    assert "e1.vt_s" in pm.out_schema     # edge times are visible to Filter (R1)

    with pytest.raises(InvalidArgError):
        Pattern((NodePat("a"),), (EdgePat("e", "a", "zz"),))
    with pytest.raises(InvalidArgError):
        Pattern((NodePat("a"), NodePat("a")), (EdgePat("e", "a", "a"),))
    with pytest.raises(InvalidArgError):
        PatternMatch(pattern, (Source("nope", NodeScan("s")),))


def test_pattern_match_source_must_name_its_column_when_ambiguous():
    pattern = Pattern((NodePat("a"), NodePat("b")), (EdgePat("e1", "a", "b"),))
    ambiguous = EdgeScan("x")  # carries both src and dst as uid columns
    with pytest.raises(InvalidArgError):
        PatternMatch(pattern, (Source("a", ambiguous),))
    PatternMatch(pattern, (Source("a", ambiguous, "x.src"),))


def test_aggregate_arity_distinct_and_the_page_cut_precondition():
    scan = NodeScan("p")
    distinct = Aggregate(scan, (("k", Col("p.uid")),))  # aggregates = [] is DISTINCT
    assert distinct.out_schema.names == ("k",)
    everything = Aggregate(scan, (), (Agg("count", "n"),))  # group_by = [] is one row
    assert everything.out_schema.names == ("n",)
    means = Aggregate(scan, (), (Agg("mean", "m", Col("p.vt_s")),))
    assert means.out_schema.tau_of("m") == Tau("float")

    with pytest.raises(InvalidArgError):
        Agg("count", "n", Col("p.uid"))  # count takes no `of`
    with pytest.raises(InvalidArgError):
        Agg("median", "m", Col("p.vt_s"))
    with pytest.raises(InvalidArgError):
        Aggregate(scan, (), (Agg("count", "n"), Agg("count_distinct", "n", Col("p.uid"))))
    # an Aggregate consumes relations, never pages
    with pytest.raises(InvalidArgError):
        Aggregate(Limit(scan, 10), (), (Agg("count", "n"),))
    # ... but a top-k Limit narrows the *domain*, which is legal
    Aggregate(Limit(Order(scan, (SortKey(Col("p.vt_s"), "desc"),)), 10), (),
              (Agg("count", "n"),))


def test_order_and_limit():
    scan = NodeScan("p")
    ordered = Order(scan, (SortKey(Col("p.vt_s"), "desc", "nulls_first"),))
    assert ordered.out_schema == scan.out_schema
    assert Limit(ordered, 10).is_top_k
    assert not Limit(scan, 10).is_top_k
    with pytest.raises(InvalidArgError):
        Order(scan, ())
    with pytest.raises(InvalidArgError):
        SortKey(Col("p.uid"), "sideways")
    with pytest.raises(InvalidArgError):
        Limit(scan, 0)
    with pytest.raises(InvalidArgError):
        Limit(scan, 10, offset=0, cursor="5")


def test_all_twelve_core_types_are_covered_by_the_module():
    from tgms.tgir import CORE_NODE_TYPES, STORE_READING_CORE
    assert len(CORE_NODE_TYPES) == 12
    names = {t.__name__ for t in CORE_NODE_TYPES}
    assert names == {
        "NodeScan", "EdgeScan", "Expand", "Filter", "PropertyPredicate",
        "TypeConstraint", "Project", "Join", "PatternMatch", "Aggregate",
        "Order", "Limit"}
    # §2.0's ∅ classification: four store-reading, eight pure
    assert len(STORE_READING_CORE) == 4
    assert all(t.reads_store for t in STORE_READING_CORE)
    assert sum(1 for t in CORE_NODE_TYPES if not t.reads_store) == 8


# ---------------------------------------------------------------------------
# §4.2 — schema propagation on three small plans
# ---------------------------------------------------------------------------

def test_plan_one_anchored_neighbours_projected():
    """`NodeScan(uids) → Expand{exact(1)} → Filter → Project` — the shape
    `entity_history`'s edge list compiles to."""
    seed = NodeScan("p", uids=("u1",))
    hop = Expand(seed, "p", "q", Exact(1), rel_type="MSG", edge_var="e")
    filtered = Filter(hop, Cmp("<", Col("e.vt_s"), Lit(100)))
    projected = Project(filtered, (("uid", Col("q.uid")), ("at", Col("e.vt_s"))))
    assert filtered.out_schema == hop.out_schema
    assert projected.out_schema.names == ("uid", "at")
    assert projected.out_schema.tau_of("uid") == Tau("uid")
    assert projected.out_schema.tau_of("at") == Tau("ts")


def test_plan_two_aggregate_over_an_edge_scan():
    """`EdgeScan → Project(tuple key) → Aggregate → Order → Limit` — the shape
    an `aggregate_events` core fragment takes."""
    scan = EdgeScan("e", rel_types=("MSG",), vt_mode="event",
                    sigma_=Sigma.in_window(0, 1_000))
    keyed = Project(scan, (("pair", TupleExpr((Col("e.src"), Col("e.dst")))),), keep="all")
    agg = Aggregate(keyed, (("pair", Col("pair")),), (Agg("count", "n"),))
    top = Limit(Order(agg, (SortKey(Col("n"), "desc"),)), 10)
    assert keyed.out_schema.tau_of("pair") == Tau.tuple_of(Tau("uid"), Tau("uid"))
    assert agg.out_schema.names == ("pair", "n")
    assert agg.out_schema.tau_of("n") == Tau("int")
    assert top.out_schema == agg.out_schema and top.is_top_k


def test_plan_three_anti_join_over_a_pattern():
    """`PatternMatch` ⨝anti `NodeScan` — set difference on identity (R3)."""
    pattern = Pattern((NodePat("a"), NodePat("b")), (EdgePat("e1", "a", "b", "KNOWS"),))
    pm = PatternMatch(pattern)
    probe = Project(NodeScan("x", labels=("Bot",)), (("bot", Col("x.uid")),))
    anti = Join(pm, probe, (("a.uid", "bot"),), join_type="anti")
    assert anti.out_schema == pm.out_schema
    left_outer = Join(pm, probe, (("a.uid", "bot"),), join_type="left_outer")
    assert left_outer.out_schema.tau_of("bot") == Tau("uid").optional()


# ---------------------------------------------------------------------------
# §5.2.1 — the completeness lattice
# ---------------------------------------------------------------------------

ALL_COMPLETENESS = tuple(Completeness)


def test_meet_is_idempotent_commutative_and_associative():
    for a in ALL_COMPLETENESS:
        assert meet(a, a) is a
    for a in ALL_COMPLETENESS:
        for b in ALL_COMPLETENESS:
            assert meet(a, b) is meet(b, a)
            for c in ALL_COMPLETENESS:
                assert meet(meet(a, b), c) is meet(a, meet(b, c))


def test_refused_is_absorbing_bottom_and_complete_is_the_identity():
    for a in ALL_COMPLETENESS:
        assert meet(a, Completeness.REFUSED) is Completeness.REFUSED
        assert meet(a, Completeness.COMPLETE) is a
        assert le(Completeness.REFUSED, a)
        assert le(a, Completeness.COMPLETE)


def test_unknown_sits_above_refused_and_below_every_positive_claim():
    assert le(Completeness.REFUSED, Completeness.UNKNOWN)
    assert not le(Completeness.UNKNOWN, Completeness.REFUSED)
    for m in MIDDLE:
        assert le(Completeness.UNKNOWN, m)
        assert not le(m, Completeness.UNKNOWN)
        assert meet(Completeness.UNKNOWN, m) is Completeness.UNKNOWN


def test_the_four_middle_values_are_pairwise_incomparable_and_meet_at_unknown():
    for a in MIDDLE:
        for b in MIDDLE:
            if a is b:
                continue
            assert not comparable(a, b)
            assert meet(a, b) is Completeness.UNKNOWN


def test_meet_all_over_an_empty_input_is_the_lattice_top():
    """A leaf has no inputs and no meet to take (§5.3's two scan rows)."""
    assert meet_all(()) is Completeness.COMPLETE
    assert meet_all([Completeness.PAGINATED, Completeness.COMPLETE]) is Completeness.PAGINATED
    assert meet_all([Completeness.PAGINATED, Completeness.TOP_K]) is Completeness.UNKNOWN


def test_certification_layer_surjection():
    from tgms.tgir import CERTIFICATION_LAYER
    assert CERTIFICATION_LAYER[Completeness.COMPLETE] == "certified-complete"
    assert {CERTIFICATION_LAYER[m] for m in MIDDLE} == {"uncertified"}
    assert CERTIFICATION_LAYER[Completeness.UNKNOWN] == "uncertified"
    assert CERTIFICATION_LAYER[Completeness.REFUSED] == "none"


def test_exactness_meet_is_exact_only_when_every_input_is():
    assert meet_exactness(Exactness.EXACT, Exactness.EXACT) is Exactness.EXACT
    assert meet_exactness(Exactness.EXACT, Exactness.SAMPLED) is Exactness.SAMPLED
    with pytest.raises(InvalidArgError):
        # under-determined by §5.2.1 and unreachable in v1 — raised, not guessed
        meet_exactness(Exactness.SAMPLED, Exactness.BOUNDED)


def test_result_meta_projects_the_envelope_fields():
    scan = NodeScan("p")
    meta = ResultMeta(
        sigma=Sigma.in_window(0, 10),
        completeness=Completeness.PAGINATED,
        provenance=Provenance(scan.node_digest, "NodeScan"),
        dependency=DependencyScope.top("store-a", 7),
        schema=scan.out_schema,
    )
    out = meta.to_json()
    assert out["t_v"] == [[0, 10]] and out["t_b"] == OPEN_END
    assert out["completeness"] == "paginated" and out["exactness"] == "exact"
    assert meta.certification == "uncertified"
    assert out["dependency"]["schema"] == "tgms-depscope"


def test_node_digest_is_content_addressed_over_data_only():
    a = NodeScan("p", uids=("u1",))
    b = NodeScan("p", uids=("u1",))
    c = NodeScan("p", uids=("u2",))
    assert a.node_digest == b.node_digest != c.node_digest
    # Σ participates: two identical ops under different scopes are different nodes
    assert NodeScan("p", sigma_=Sigma.in_window(0, 5)).node_digest != a.node_digest
    # and it is exactly §5.4's tuple
    assert a.node_digest == compute_node_digest("NodeScan", a.canonical_args(), a.sigma)
    # input digests participate — a Merkle digest of the plan subtree
    assert Filter(a, Lit(True)).node_digest != Filter(c, Lit(True)).node_digest


# ---------------------------------------------------------------------------
# R7 — the opaque leaf and the ∅-kernel guard
# ---------------------------------------------------------------------------

def test_opaque_leaf_carries_args_sigma_schema_and_the_withholding_decision():
    leaf = OpaqueLeaf.build("entity_history", {"uid": "u1", "include_edges": True},
                            ("rows", "rows_total", "truncated", "cursor", "edges"),
                            sigma=Sigma.default())
    assert leaf.op == "entity_history"
    assert leaf.args == {"uid": "u1", "include_edges": True}
    assert leaf.out_schema.names == ("rows", "rows_total", "truncated", "cursor", "edges")
    assert leaf.out_schema.tau_of("truncated") == Tau("bool")
    assert leaf.reads_store and leaf.withhold_adapter is False


def test_compute_is_the_one_empty_scope_leaf_and_cannot_be_un_withheld():
    leaf = OpaqueLeaf.build("compute", {"expr": "1+1"}, ("value",))
    assert leaf.withhold_adapter is True and not leaf.reads_store
    with pytest.raises(InvalidArgError):
        OpaqueLeaf("compute", (), ("value",), "overlap", False)
    # widening the other way is permitted
    assert OpaqueLeaf("entity_history", (), ("rows",), "overlap", True).withhold_adapter


def test_null_adapter_fails_loudly_at_the_first_read():
    leaf = OpaqueLeaf.build("compute", {}, ("value",))
    adapter = adapter_for(leaf, object())
    assert isinstance(adapter, NullAdapter)
    with pytest.raises(StateError):
        adapter.nodes_columnar()
    # a store-reading node gets the real adapter
    sentinel = object()
    assert adapter_for(NodeScan("p"), sentinel) is sentinel
    assert isinstance(adapter_for(Filter(NodeScan("p"), Lit(True)), sentinel), NullAdapter)


def test_opaque_leaf_validation():
    with pytest.raises(InvalidArgError):
        OpaqueLeaf.build("", {}, ("rows",))
    with pytest.raises(InvalidArgError):
        OpaqueLeaf.build("entity_history", {}, ())
    with pytest.raises(InvalidArgError):
        OpaqueLeaf("entity_history", (("uid", "a"), ("uid", "b")), ("rows",))


# ---------------------------------------------------------------------------
# FRESHNESS_SEMANTICS D13.2–D13.9 — the wire format
# ---------------------------------------------------------------------------

def test_scope_round_trips_through_canonical_json():
    scope = DependencyScope(
        store="store-a", tt_q=1_755_780_000_123_456, pinned=True, clamped=False,
        checkpoints=(Checkpoint(918_273, "a1b2c3d4e5f60718"),),
        terms=(
            ScopeTerm(kinds=("assert_node", "correct"),
                      targets=Targets(nodes=("u1", "u2"),
                                      incident=Incident("either", ("u1",))),
                      rel_types=("MSG",), vt=((0, 100),), vt_mode="event",
                      props=("weight", "@label", "@identity")),
            TOP_TERM,
        ))
    text = scope.canonical()
    assert DependencyScope.from_json(json.loads(text)) == scope
    assert DependencyScope.from_json(json.loads(text)).canonical() == text
    obj = json.loads(text)
    assert obj["schema"] == "tgms-depscope" and obj["version"] == 1
    assert obj["checkpoints"] == [[918_273, "a1b2c3d4e5f60718"]]
    assert obj["terms"][1] == {"kinds": "*", "targets": "*", "rel_types": "*",
                               "vt": "*", "vt_mode": "overlap", "props": "*"}


def test_top_is_the_string_star_at_every_level_and_is_not_an_empty_list():
    """D13.5: ⊤ is `"*"`, one spelling, and is deliberately distinct from `[]`,
    which means *nothing matches* and makes the term vacuous."""
    top = ScopeTerm(kinds=TOP, targets=TOP, rel_types=TOP, vt=TOP, props=TOP)
    vacuous = ScopeTerm(kinds=(), targets=Targets(nodes=()), rel_types=(), vt=(),
                        props=())
    assert top.to_json()["kinds"] == "*"
    assert vacuous.to_json()["kinds"] == []
    assert vacuous.to_json()["targets"] == {"nodes": []}
    assert top != vacuous
    assert ScopeTerm.from_json(top.to_json()) == top
    assert ScopeTerm.from_json(vacuous.to_json()) == vacuous
    # an *absent* arm is ∅ for that entity kind — distinct from both
    assert Targets(nodes=TOP).to_json() == {"nodes": "*"}
    assert Targets().to_json() == {}
    assert Targets(nodes=()).to_json() == {"nodes": []}


def test_empty_terms_is_the_empty_scope_and_is_not_top():
    empty = DependencyScope.empty("store-a", 5)
    assert empty.is_empty and empty.to_json()["terms"] == []
    assert not DependencyScope.top("store-a", 5).is_empty
    assert DependencyScope.from_json(json.loads(empty.canonical())) == empty


def test_scope_term_rejects_unknown_enum_values():
    with pytest.raises(InvalidArgError):
        ScopeTerm(kinds=("assert_node", "delete_everything"))
    with pytest.raises(InvalidArgError):
        ScopeTerm(vt_mode="whenever")
    with pytest.raises(InvalidArgError):
        ScopeTerm(props=("@unknown_pseudo",))
    with pytest.raises(InvalidArgError):
        Incident("neither", ("u1",))
    with pytest.raises(InvalidArgError):
        ScopeTerm(vt=((10, 10),))


def test_carve_reachability_is_decided_by_the_property_vocabulary():
    """D13.7a: a scope that does not name `@recut` or `@version` is untouched by
    the carve arm and keeps its window."""
    assert ScopeTerm(props=("@event_key", "@extent")).carve_reachable is False
    assert ScopeTerm(props=("@recut",)).carve_reachable is True
    assert ScopeTerm(props=("@version",)).carve_reachable is True
    assert TOP_TERM.carve_reachable is True  # "*" names everything


def test_footprint_vt_constructors_are_the_only_site_of_the_plus_one():
    assert vt_closed(10, 20) == ((10, 21),)
    assert vt_closed(10, OPEN_END) == ((10, OPEN_END),)  # saturating, exactly
    assert vt_from(10) == ((10, OPEN_END),)
    with pytest.raises(InvalidArgError):
        vt_closed(20, 10)
    with pytest.raises(InvalidArgError):
        vt_from(OPEN_END)


def test_scope_requires_a_store_and_mandatory_checkpoints():
    with pytest.raises(InvalidArgError):
        DependencyScope(store="", tt_q=1)
    with pytest.raises(InvalidArgError):
        DependencyScope(store="s", tt_q=1, checkpoints=())
    assert DependencyScope(store="s", tt_q=1).checkpoints == FULL_SCAN_CHECKPOINTS


def test_store_identity_is_the_header_and_first_batch_digest():
    """Coordinator ruling (M2.1): the header alone is a constant and
    discriminates nothing, so the identity is `digest(header ‖ first batch)` —
    distinct between stores, identical across replays of one history."""
    from tgms.storage.eventlog import HEADER
    from tgms.tgir import store_identity

    b1 = {"batch_id": "aaaa", "tt": 10, "ops": [{"op": "assert_node", "uid": "a"}]}
    b2 = {"batch_id": "bbbb", "tt": 11, "ops": [{"op": "assert_node", "uid": "b"}]}
    ident = store_identity(HEADER, b1)
    assert ident == store_identity(canonical_json(HEADER), canonical_json(b1))
    assert ident == store_identity(canonical_json(HEADER).encode(),
                                   canonical_json(b1).encode())
    assert ident != store_identity(HEADER, b2)      # two stores, two identities
    # a log with no batches has no identity to state — until its first write
    assert store_identity(HEADER, None) == UNANCHORED == "unanchored"


def test_a_kinds_set_naming_every_kind_canonicalizes_to_top():
    """D13.5's one-spelling rule, applied at construction so `==` and the
    round-trip agree."""
    from tgms.tgir.depscope import KINDS

    assert ScopeTerm(kinds=KINDS).kinds is TOP
    assert ScopeTerm(kinds=tuple(reversed(KINDS))) == ScopeTerm(kinds=TOP)
    assert ScopeTerm(kinds=KINDS).to_json()["kinds"] == "*"
    partial = ScopeTerm(kinds=KINDS[:-1])
    assert partial.kinds is not TOP and len(partial.kinds) == 4


# ---------------------------------------------------------------------------
# D13.8 / D13.8a / D13.8b — ⊎
# ---------------------------------------------------------------------------

def _scope(store="store-a", tt_q=100, offset=500, chain="c1", pinned=False,
           clamped=False, terms=(TOP_TERM,)):
    return DependencyScope(store, tt_q, tuple(terms), (Checkpoint(offset, chain),),
                           pinned, clamped)


def test_union_concatenates_terms_and_checkpoints():
    a = _scope(offset=500, chain="c500", terms=(ScopeTerm(kinds=("correct",)),))
    b = _scope(offset=900, chain="c900", terms=(ScopeTerm(kinds=("retract",)),))
    u = a.union(b)
    assert u.terms == a.terms + b.terms
    assert [c.offset for c in u.checkpoints] == [500, 900]
    # every operand's tamper-evidence is kept, not shrunk to the earliest prefix
    assert {c.chain for c in u.checkpoints} == {"c500", "c900"}
    assert u.min_offset == 500


def test_union_moves_the_cursor_triple_as_a_unit_never_component_wise():
    """D13.8a: the triple comes from whichever operand has the smaller minimum
    offset. Taking `min` of the `tt_q`s while keeping the other operand's offset
    would leave batches in the tt-suffix unscanned — a false `FRESH`."""
    early = _scope(offset=500, chain="c500", tt_q=900, pinned=True, clamped=False)
    late = _scope(offset=900, chain="c900", tt_q=100, pinned=False, clamped=True)
    for u in (early.union(late), late.union(early)):
        assert u.tt_q == 900 and u.pinned is True and u.clamped is False
        assert u.min_offset == 500


def test_union_tie_break_on_equal_offsets_takes_the_smaller_tt_q():
    a = _scope(offset=500, tt_q=900, pinned=True)
    b = _scope(offset=500, tt_q=100, pinned=False, clamped=True)
    for u in (a.union(b), b.union(a)):
        assert (u.tt_q, u.pinned, u.clamped) == (100, False, True)


def test_union_refuses_at_construction_on_a_store_mismatch():
    with pytest.raises(InvalidArgError):
        _scope(store="store-a").union(_scope(store="store-b"))
    with pytest.raises(InvalidArgError):
        union_all([_scope(store="store-a"), _scope(store="store-b")])


def test_union_refuses_across_schema_versions():
    a = _scope()
    b = DependencyScope("store-a", 100, (TOP_TERM,), (Checkpoint(500, "c1"),),
                        version=2)
    with pytest.raises(InvalidArgError):
        a.union(b)


def test_union_is_associative_over_terms_and_needs_a_nonempty_sequence():
    a = _scope(offset=100, terms=(ScopeTerm(kinds=("correct",)),))
    b = _scope(offset=200, terms=(ScopeTerm(kinds=("retract",)),))
    c = _scope(offset=300, terms=())
    assert a.union(b).union(c).terms == a.union(b.union(c)).terms
    assert union_all([a, b, c]).terms == a.union(b).union(c).terms
    with pytest.raises(InvalidArgError):
        union_all([])


def test_union_with_the_empty_scope_changes_nothing_but_the_checkpoints():
    a = _scope(terms=(ScopeTerm(kinds=("correct",)),))
    e = DependencyScope.empty("store-a", 100, checkpoints=(Checkpoint(500, "c1"),))
    assert a.union(e).terms == a.terms


# ---------------------------------------------------------------------------
# D13.10 / D13.15 / L13.1 — scope_of and the anchor table
# ---------------------------------------------------------------------------

def test_node_scan_scope_carries_both_arms_when_uids_are_bound():
    """L13.3: wherever `kinds` includes `𝒟`, `targets` must carry an `incident`
    arm over the same uids, or `𝒟`'s presence is inert."""
    scope = leaf_scope(NodeScan("p", uids=("u1", "u2")), BASIS)
    term = scope.terms[0]
    # `𝒩 ∪ 𝒟` names every kind, and a kinds set equal to all five *is* ⊤, which
    # has exactly one spelling (D13.5) — so the derivation's `𝒟` reach shows up
    # as `"*"` rather than as an enumeration that means the same thing
    assert term.kinds is TOP
    assert term.targets.nodes == ("u1", "u2")
    assert term.targets.incident == Incident("either", ("u1", "u2"))
    # an unrestricted scan is ⊤-targeted
    assert leaf_scope(NodeScan("p"), BASIS).terms[0].targets is TOP


def test_edge_scan_scope_uses_the_incident_arm_or_the_edges_arm():
    bound = leaf_scope(EdgeScan("e", rel_types=("MSG",),
                                endpoints=Endpoints("dst", ("u1",))), BASIS).terms[0]
    assert bound.targets.incident == Incident("dst", ("u1",))
    assert bound.rel_types == ("MSG",)
    free = leaf_scope(EdgeScan("e"), BASIS).terms[0]
    assert free.targets.edges is TOP and free.targets.incident is None
    assert free.rel_types is TOP


def test_scope_vt_is_copied_from_sigma_with_no_adjustment():
    scan = NodeScan("p", sigma_=Sigma.in_window(10, 20), vt_mode="instant")
    term = leaf_scope(scan, BASIS).terms[0]
    assert term.vt == ((10, 20),) and term.vt_mode == "instant"


def test_expand_narrows_only_its_edge_arm_at_exact_one():
    seed = NodeScan("p", uids=("u1",))
    one = leaf_scope(Expand(seed, "p", "q", Exact(1), dir="out"), BASIS).terms[0]
    assert one.targets.incident == Incident("src", ("u1",))
    assert one.targets.nodes is TOP  # binding node columns forbids narrowing it

    zero = leaf_scope(Expand(seed, "p", "q", Exact(0)), BASIS).terms[0]
    assert zero.targets.nodes == ("u1",) and zero.targets.incident is None

    for hops in (Exact(2), Bounded(1, 3), Unbounded(1)):
        multi = leaf_scope(Expand(seed, "p", "q", hops), BASIS).terms[0]
        assert multi.targets is TOP, hops


def test_expand_falls_back_to_top_when_the_anchor_is_top():
    """The trap L13.1 states explicitly: a narrow upstream *scope* does not make
    a narrow anchor. `EdgeScan(endpoints={dst})`'s `src` column ranges over
    every account that has written to the seed."""
    scan = EdgeScan("e", endpoints=Endpoints("dst", ("u1",)))
    assert anchor_of_var(scan, "e.src") is TOP
    hop = Expand(scan, "e.src", "q", Exact(1))
    assert leaf_scope(hop, BASIS).terms[0].targets is TOP


def test_anchor_table_roles():
    assert anchor_of_var(NodeScan("p", uids=("u1",)), "p") == frozenset({"u1"})
    assert anchor_of_var(NodeScan("p", labels=("Person",)), "p") is TOP
    both = EdgeScan("e", endpoints=Endpoints("both", ("u1", "u2")))
    from tgms.tgir import anchors_of
    assert anchors_of(both) == {"e.src": frozenset({"u1", "u2"}),
                                "e.dst": frozenset({"u1", "u2"})}
    either = EdgeScan("e", endpoints=Endpoints("either", ("u1",)))
    assert anchors_of(either) == {}  # neither column is contained in U
    src_only = EdgeScan("e", endpoints=Endpoints("src", ("u1",)))
    assert anchors_of(src_only) == {"e.src": frozenset({"u1"})}
    # selections pass through; Expand's `into` is ⊤
    seed = NodeScan("p", uids=("u1",))
    assert anchor_of_var(Filter(seed, Lit(True)), "p") == frozenset({"u1"})
    assert anchor_of_var(Expand(seed, "p", "q", Exact(1)), "q") is TOP
    assert anchor_of_var(Expand(seed, "p", "q", Exact(1)), "p") == frozenset({"u1"})


def test_pattern_match_scope_keeps_nodes_top_beside_its_incident_arm():
    """D13.15 + L13.2a: `"*"` unless **every** node variable is anchored to a
    bound relation, and then the `incident` arm comes *in addition to*
    `nodes: "*"` — its node variables expose props and label, and a correction
    to either changes an output row."""
    pattern = Pattern((NodePat("a"), NodePat("b")), (EdgePat("e1", "a", "b", "KNOWS"),))
    cohort_a = Project(NodeScan("x", uids=("u1",)), (("k", Col("x.uid")),))
    cohort_b = Project(NodeScan("y", uids=("u2",)), (("k2", Col("y.uid")),))

    partly = PatternMatch(pattern, (Source("a", cohort_a, "k"),))
    assert leaf_scope(partly, BASIS).terms[0].targets is TOP

    fully = PatternMatch(pattern, (Source("a", cohort_a, "k"), Source("b", cohort_b, "k2")))
    term = leaf_scope(fully, BASIS).terms[0]
    assert term.targets.nodes is TOP
    assert term.targets.incident == Incident("either", ("u1", "u2"))
    assert term.rel_types == ("KNOWS",)


def test_pure_nodes_have_the_empty_leaf_scope_and_a_filter_never_narrows():
    """D13.11/D13.12: every selection operator is `∅`-scoped, and scopes only
    ever union — so no code path exists in which a predicate could narrow one."""
    scan = NodeScan("p", uids=("u1",))
    filtered = Filter(scan, Cmp("<", Col("p.vt_s"), Lit(5)))
    assert leaf_scope(filtered, BASIS).is_empty
    assert scope_of(filtered, BASIS).terms == leaf_scope(scan, BASIS).terms


def test_scope_of_unions_every_input_including_seeds_only_ones():
    seed = NodeScan("p", uids=("u1",))
    hop = Expand(seed, "p", "q", Exact(1))
    scope = scope_of(hop, BASIS)
    assert len(scope.terms) == 2  # the hop's own term ⊎ the seed-supplying scan's
    probe = NodeScan("x", labels=("Bot",))
    joined = Join(Project(hop, (("k", Col("q.uid")),)),
                  Project(probe, (("b", Col("x.uid")),)),
                  (("k", "b"),), join_type="anti")
    assert len(scope_of(joined, BASIS).terms) == 3


def test_opaque_leaves_get_the_coarse_top_term_and_compute_gets_empty():
    """The coarse default, for the twelve operators M2.3 left on `"*"`. The
    three it derived are `tests/test_tgir_scopes.py`'s subject; `compute` is ∅
    from day one."""
    leaf = OpaqueLeaf.build("version_history",
                            {"kind": "node", "window": {"t_a": 0, "t_b": 10}},
                            ("rows",))
    assert scope_of(leaf, BASIS).terms == (TOP_TERM,)
    assert scope_of(OpaqueLeaf.build("compute", {}, ("value",)), BASIS).is_empty


def test_scope_of_matches_the_documented_wire_shape():
    leaf = OpaqueLeaf.build("version_history", {"uid": "u1"}, ("rows",))
    obj = scope_of(leaf, ScopeBasis(store="store-a", tt_q=42)).to_json()
    assert obj == {
        "schema": "tgms-depscope", "version": 1, "store": "store-a", "tt_q": 42,
        "pinned": False, "clamped": False,
        "checkpoints": [[0, FULL_SCAN_CHECKPOINTS[0].chain]],
        "terms": [{"kinds": "*", "targets": "*", "rel_types": "*", "vt": "*",
                   "vt_mode": "overlap", "props": "*"}],
    }
