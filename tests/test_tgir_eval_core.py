"""M3.0 — the runtime: `Relation`, the expression evaluator, the scans, the
selections, `Project`/`Order`/`Limit` and `Join{inner}`.

What is worth testing here is what a receipt cannot see: the null story, the
error semantics, the canonical orders §2's tables declare, and the properties
that make column pruning *not* a plan rewrite. The cross-checks against
existing operators live in `scripts/check_core_equivalence.py`, which runs on
real stores and both backends.
"""

from __future__ import annotations

import numpy as np
import pytest

from tgms.core.errors import InvalidArgError, StateError
from tgms.core.model import OPEN_END
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.eval import Execution, evaluate_core
from tgms.tgir.eval.expr_eval import eval_expr, eval_predicate
from tgms.tgir.expr import (
    Arith, BoolOp, Cast, Cmp, Coalesce, Col, If, IsNull, Lit, MathFn, Not, PropRef,
    TupleExpr,
)
from tgms.tgir.node import (
    Aggregate, Agg, EdgeScan, Endpoints, Exact, Expand, Filter, Join, Limit,
    NodePat, NodeScan, Order, Pattern, PatternMatch, Project, PropertyPredicate,
    SortKey, TypeConstraint,
)
from tgms.tgir.prune import live_columns
from tgms.tgir.relation import Relation, array_for, combine_nulls
from tgms.tgir.types import Column, Schema, Sigma, T_INT, T_STR, Tau

from .conftest import fresh_adapter


def write(adapter, ops, tt):
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


def node_op(uid, label="N", props=None, vt_s=0, vt_e=100):
    return {"op": "assert_node", "uid": uid, "label": label, "props": props or {},
            "vt_s": vt_s, "vt_e": vt_e, "source": "i", "provenance_ref": None}


def edge_op(src, dst, rel_type="R", props=None, vt_s=0, vt_e=100, disc=""):
    return {"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel_type,
            "props": props or {}, "vt_s": vt_s, "vt_e": vt_e, "disc": disc,
            "source": "i", "provenance_ref": None}


@pytest.fixture()
def store():
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [node_op(f"u{i}", "N" if i % 2 else "M", {"w": i, "name": f"n{i}"})
              for i in range(1, 6)], 1)
    write(a, [edge_op(f"u{i}", f"u{i % 5 + 1}", "R" if i % 2 else "S", {"k": i},
                      vt_s=10 * i, vt_e=10 * i + 25) for i in range(1, 5)], 2)
    yield a
    a.close()


def rel_of(**cols) -> Relation:
    """A hand-built relation, for the expression tests."""
    schema = Schema(tuple(
        Column(name, T_INT if isinstance(values[0], int) and not isinstance(
            values[0], bool) else (T_STR if isinstance(values[0], str)
                                   else Tau("json")))
        for name, values in cols.items()))
    return Relation(schema,
                    {k: array_for(schema.tau_of(k), v) for k, v in cols.items()},
                    len(next(iter(cols.values()))))


# ---------------------------------------------------------------------------
# the relation
# ---------------------------------------------------------------------------

def test_a_relation_validates_its_own_shape():
    schema = Schema.of(Column("a", T_INT))
    Relation(schema, {"a": np.array([1, 2])}, 2)
    with pytest.raises(Exception):
        Relation(schema, {}, 0)                       # missing column
    with pytest.raises(Exception):
        Relation(schema, {"a": np.array([1, 2])}, 3)  # row count disagrees


def test_null_masks_ride_along_through_every_row_operation():
    """The property the whole design turns on: `take` is the primitive under
    masking, sorting and joining, so a mask cannot be lost by one of them."""
    schema = Schema.of(Column("a", T_INT.optional()))
    rel = Relation(schema, {"a": np.array([1, 0, 3])}, 3,
                   {"a": np.array([False, True, False])})
    assert rel.has_nulls("a")
    taken = rel.take(np.array([2, 1]))
    assert list(taken.is_null("a")) == [False, True]
    assert taken.rows() == [{"a": 3}, {"a": None}]
    filtered = rel.filter(np.array([True, True, False]))
    assert filtered.rows() == [{"a": 1}, {"a": None}]


def test_an_absent_mask_means_no_nulls_and_allocates_nothing():
    rel = rel_of(a=[1, 2, 3])
    assert rel.null_mask("a") is None
    assert not rel.has_nulls("a")
    assert combine_nulls((None, None), 3) is None


def test_nullable_copy_and_concat_preserve_the_null_story():
    """`Join{left_outer}`'s right side is every column nullable (§4.2), and its
    fill rows are all-null — the shape M3.2 will build on, designed now."""
    rel = rel_of(a=[1, 2])
    nullable = rel.nullable_copy()
    assert nullable.schema.tau_of("a").nullable
    fill = Relation(nullable.schema, {"a": np.array([0])}, 1,
                    {"a": np.array([True])})
    both = nullable.concat_rows(fill)
    assert both.n == 3 and both.rows() == [{"a": 1}, {"a": 2}, {"a": None}]


# ---------------------------------------------------------------------------
# the expression evaluator
# ---------------------------------------------------------------------------

def test_arithmetic_and_the_blessed_quotient():
    rel = rel_of(a=[10, 7], b=[4, 2])
    assert list(eval_expr(Arith("+", Col("a"), Col("b")), rel)[0]) == [14, 9]
    assert list(eval_expr(Arith("floor_div", Col("a"), Col("b")), rel)[0]) == [2, 3]
    values, _ = eval_expr(Arith("/", Col("a"), Col("b")), rel)
    assert list(values) == [2.5, 3.5]


def test_division_by_zero_is_an_error_never_a_null():
    """§8.15, and the reason it matters: a null would silently change which
    rows the plan returns."""
    rel = rel_of(a=[1, 2], b=[1, 0])
    with pytest.raises(InvalidArgError, match="division by zero"):
        eval_expr(Arith("/", Col("a"), Col("b")), rel)
    with pytest.raises(InvalidArgError, match="division by zero"):
        eval_expr(Arith("floor_div", Col("a"), Col("b")), rel)


def test_if_short_circuits_per_row():
    """§9.2's ruling. A guarded quotient must not raise on the rows its guard
    excludes — which is what "pure, row-local, **total**" asks for once
    division by zero is an error."""
    rel = rel_of(a=[10, 20, 30], b=[2, 0, 5])
    guarded = If(Cmp("!=", Col("b"), Lit(0)), Arith("/", Col("a"), Col("b")), Lit(0))
    values, nulls = eval_expr(guarded, rel)
    assert [round(float(v), 6) for v in values] == [5.0, 0.0, 6.0]
    assert nulls is None
    # ... and without the guard the same rows do raise, which is the contrast
    with pytest.raises(InvalidArgError):
        eval_expr(Arith("/", Col("a"), Col("b")), rel)


def test_a_null_operand_yields_a_null_not_an_error():
    schema = Schema.of(Column("a", T_INT.optional()), Column("b", T_INT))
    rel = Relation(schema, {"a": np.array([1, 0]), "b": np.array([2, 2])}, 2,
                   {"a": np.array([False, True])})
    values, nulls = eval_expr(Arith("+", Col("a"), Col("b")), rel)
    assert list(nulls) == [False, True]
    assert values[0] == 3
    assert list(eval_expr(IsNull(Col("a")), rel)[0]) == [False, True]
    assert list(eval_expr(Coalesce(Col("a"), Lit(99)), rel)[0]) == [1, 99]


def test_a_null_predicate_is_not_true():
    """§2.4: "a `null` result is not `true`" — the rows drop."""
    schema = Schema.of(Column("a", T_INT.optional()))
    rel = Relation(schema, {"a": np.array([5, 0])}, 2, {"a": np.array([False, True])})
    assert list(eval_predicate(Cmp(">", Col("a"), Lit(1)), rel)) == [True, False]


def test_cast_of_a_non_canonical_identity_is_an_error():
    """§2.7: `cast(uid, int)` is well-typed **iff** the identity string is a
    canonical decimal integer. Row-determining — LDBC's `toInteger(id)`
    tie-break sorts under a `Limit` on two of the gate-tested rows."""
    assert list(eval_expr(Cast(Col("a"), T_INT), rel_of(a=["10", "9"]))[0]) == [10, 9]
    for bad in (["01"], ["1x"], [""], ["-0"]):
        with pytest.raises(InvalidArgError, match="canonical decimal"):
            eval_expr(Cast(Col("a"), T_INT), rel_of(a=bad))


def test_string_comparison_is_by_code_point():
    """`COLLATE "C"`: "10" < "9" is what decides two LDBC rows' tie-breaks."""
    rel = rel_of(a=["10"], b=["9"])
    assert list(eval_expr(Cmp("<", Col("a"), Col("b")), rel)[0]) == [True]


def test_math_functions_and_the_explicit_rounding_rule():
    rel = rel_of(a=[-3, 7])
    assert list(eval_expr(MathFn("abs", Col("a")), rel)[0]) == [3, 7]
    assert list(eval_expr(MathFn("floor", Arith("/", Col("a"), Lit(2))), rel)[0]) \
        == [-2, 3]
    assert list(eval_expr(MathFn("ceil", Arith("/", Col("a"), Lit(2))), rel)[0]) \
        == [-1, 4]


def test_boolean_forms_and_tuple_keys():
    rel = rel_of(a=[1, 5], b=[1, 2])
    both = BoolOp("and", Cmp(">", Col("a"), Lit(0)), Cmp(">", Col("b"), Lit(1)))
    assert list(eval_expr(both, rel)[0]) == [False, True]
    assert list(eval_expr(Not(Cmp(">", Col("a"), Lit(0))), rel)[0]) == [False, False]
    keys, _ = eval_expr(TupleExpr((Col("a"), Col("b"))), rel)
    assert list(keys) == [(1, 1), (5, 2)]


def test_prop_ref_reads_the_parsed_bag(store):
    rel = evaluate_core(NodeScan("p"), store)
    values, nulls = eval_expr(PropRef("p.props", "w"), rel)
    assert sorted(int(v) for v in values) == [1, 2, 3, 4, 5]
    assert nulls is None
    _missing, missing_nulls = eval_expr(PropRef("p.props", "absent"), rel)
    assert missing_nulls is not None and missing_nulls.all()


# ---------------------------------------------------------------------------
# the scans, Σ, and the fallback
# ---------------------------------------------------------------------------

def test_a_scan_emits_versions_not_entities(store):
    write(store, [node_op("u1", "N", {"w": 11}, vt_s=100, vt_e=200)], 3)
    rel = evaluate_core(NodeScan("p", uids=("u1",)), store)
    assert rel.n == 2, "a window scope returns every version overlapping it"
    instant = evaluate_core(
        NodeScan("p", uids=("u1",), vt_mode="instant",
                 sigma_=Sigma.at_instant(150)), store)
    assert instant.n == 1, "an instant scope returns at most one row per uid"


def test_sigma_keying_modes(store):
    """§3.2's three predicates, over the same store."""
    window = Sigma.in_window(20, 40)
    overlap = evaluate_core(EdgeScan("e", vt_mode="overlap", sigma_=window), store)
    event = evaluate_core(EdgeScan("e", vt_mode="event", sigma_=window), store)
    instant = evaluate_core(EdgeScan("e", vt_mode="instant",
                                     sigma_=Sigma.at_instant(30)), store)
    starts = lambda rel: sorted(int(v) for v in rel.column("e.vt_s"))  # noqa: E731
    assert starts(overlap) == [10, 20, 30]   # vt_s < 40 and 20 < vt_e
    assert starts(event) == [20, 30]         # 20 <= vt_s < 40
    assert starts(instant) == [10, 20, 30]   # vt_s <= 30 < vt_e


def test_labels_and_uids_are_post_filters_with_identical_semantics(store):
    """§9.3: no backend has a label predicate, so the filter lands in numpy —
    and the point of the ruling is that this is unobservable."""
    scanned = evaluate_core(NodeScan("p", labels=("N",)), store)
    manual = evaluate_core(
        Filter(NodeScan("p"), Cmp("=", Col("p.label"), Lit("N"))), store)
    assert scanned.rows() == manual.rows()


def test_a_scan_never_raises_on_an_unknown_uid(store):
    """A core scan has **no** `E_NOT_FOUND` — FRESHNESS_SEMANTICS D13.15's
    RG-10 exemption is granted on exactly that ground, so the scan must not
    raise where the leaf does."""
    assert evaluate_core(NodeScan("p", uids=("nope",)), store).n == 0
    assert evaluate_core(
        EdgeScan("e", endpoints=Endpoints("either", ("nope",))), store).n == 0


def test_endpoint_roles(store):
    """All four roles, on `u2` — which the fixture makes both a source and a
    target, so `either` and `both` are not degenerate."""
    src = evaluate_core(EdgeScan("e", endpoints=Endpoints("src", ("u2",))), store)
    dst = evaluate_core(EdgeScan("e", endpoints=Endpoints("dst", ("u2",))), store)
    either = evaluate_core(EdgeScan("e", endpoints=Endpoints("either", ("u2",))),
                           store)
    assert set(src.column("e.src")) == {"u2"} and src.n == 1
    assert set(dst.column("e.dst")) == {"u2"} and dst.n == 1
    assert either.n == src.n + dst.n == 2
    both = evaluate_core(
        EdgeScan("e", endpoints=Endpoints("both", ("u1", "u2"))), store)
    assert both.n == 1 and both.rows()[0]["e.src"] == "u1"


def test_the_belief_fallback_and_the_censoring_rule(store):
    """`belief ≠ current` has no columnar route at all, so it takes the
    `versions_columnar` path — and wherever `tt_e` is materialized, §3.4's
    censoring rule applies: a belief that ended after `T_b` had not ended yet.
    """
    write(store, [node_op("u1", "RELABEL", {"w": 1})], 3)   # supersedes u1@tt1
    now = evaluate_core(NodeScan("p", uids=("u1",)), store)
    assert [r["p.label"] for r in now.rows()] == ["RELABEL"]

    superseded = evaluate_core(NodeScan("p", uids=("u1",), belief="superseded"),
                               store)
    assert [r["p.label"] for r in superseded.rows()] == ["N"]

    every = evaluate_core(NodeScan("p", uids=("u1",), belief="all"), store)
    assert sorted(r["p.label"] for r in every.rows()) == ["N", "RELABEL"]

    pinned = evaluate_core(
        NodeScan("p", uids=("u1",), belief="all",
                 sigma_=Sigma((Sigma.default().t_v[0],), 2)), store)
    assert all(r["p.tt_e"] == OPEN_END for r in pinned.rows()), \
        "a belief that ended after T_b must report tt_e = OPEN_END"


def test_node_props_are_fetched_by_vid_after_the_other_predicates(store):
    """Node `props` have no columnar route at all, so they are resolved on the
    rows that survived — which is why the props column is correct but is not
    what the scan filtered on."""
    rel = evaluate_core(
        Project(Filter(NodeScan("p"), Cmp("=", Col("p.label"), Lit("N"))),
                (("w", PropRef("p.props", "w")),)), store)
    assert sorted(int(v) for v in rel.column("w")) == [1, 3, 5]


# ---------------------------------------------------------------------------
# §3.4 — canonical output order
# ---------------------------------------------------------------------------

def test_scan_canonical_order_is_vt_s_then_vid(store):
    """§2.1/§2.2. The ABC's docstring says only "Sorted by vt_s", so the
    tie-break is re-asserted here rather than assumed: a backend that stopped
    ordering by `vid` would silently reorder every plan's output."""
    for rel, prefix in ((evaluate_core(NodeScan("p"), store), "p"),
                        (evaluate_core(EdgeScan("e"), store), "e")):
        keys = list(zip((int(v) for v in rel.column(f"{prefix}.vt_s")),
                        rel.column(f"{prefix}.vid")))
        assert keys == sorted(keys)


def test_a_non_current_belief_scan_orders_by_tt_s_then_vid(store):
    """"the belief log's own order — the difference between a version scan and
    a snapshot"."""
    write(store, [node_op("u1", "V2", {"w": 1})], 3)
    write(store, [node_op("u2", "V2", {"w": 2})], 4)
    rel = evaluate_core(NodeScan("p", belief="all"), store)
    keys = list(zip((int(v) for v in rel.column("p.tt_s")), rel.column("p.vid")))
    assert keys == sorted(keys)


def test_selections_and_projections_preserve_input_order(store):
    """Eleven of the twelve operators never sort; mask indexing is why."""
    scan = evaluate_core(NodeScan("p"), store)
    filtered = evaluate_core(Filter(NodeScan("p"),
                                    Cmp("!=", Col("p.label"), Lit("nope"))), store)
    assert list(filtered.column("p.vid")) == list(scan.column("p.vid"))


def test_order_is_total_and_ties_keep_the_input_order(store):
    """§2.11: the declared keys are extended with the input's own canonical
    order as a final tiebreak, so the output order is total."""
    scan = evaluate_core(NodeScan("p"), store)
    tied = evaluate_core(Order(NodeScan("p"), (SortKey(Col("p.vt_s")),)), store)
    assert list(tied.column("p.vid")) == list(scan.column("p.vid")), \
        "every row shares vt_s, so the input order must survive intact"

    desc = evaluate_core(Order(NodeScan("p"), (SortKey(Col("p.vt_s"), "desc"),)),
                         store)
    assert list(desc.column("p.vid")) == list(scan.column("p.vid")), \
        "a descending sort on an all-equal key must not reverse the tiebreak"


def test_order_directions_and_null_placement():
    schema = Schema.of(Column("a", T_INT.optional()), Column("b", T_STR))
    rel = Relation(schema, {"a": np.array([3, 0, 1]),
                            "b": np.array(["x", "y", "z"], dtype=object)}, 3,
                   {"a": np.array([False, True, False])})
    node = Order(_Stub(schema), (SortKey(Col("a"), "asc", "nulls_first"),))
    assert [r["b"] for r in _order(node, rel).rows()] == ["y", "z", "x"]
    node = Order(_Stub(schema), (SortKey(Col("a"), "asc", "nulls_last"),))
    assert [r["b"] for r in _order(node, rel).rows()] == ["z", "x", "y"]
    node = Order(_Stub(schema), (SortKey(Col("a"), "desc", "nulls_last"),))
    assert [r["b"] for r in _order(node, rel).rows()] == ["x", "z", "y"]


def _order(node, rel):
    from tgms.tgir.eval.order import eval_order
    return eval_order(node, rel)


class _Stub:
    """A node-shaped stand-in, so an `Order` can be built over a hand-made
    relation without a store behind it."""

    reads_store = False

    def __init__(self, schema):
        self.out_schema = schema
        self.sigma = Sigma.default()
        self.inputs = ()
        self.node_digest = "stub"
        self.op = "Stub"


# ---------------------------------------------------------------------------
# Limit, PropertyPredicate, TypeConstraint, Join
# ---------------------------------------------------------------------------

def test_limit_cuts_at_the_output_boundary(store):
    ordered = Order(NodeScan("p"), (SortKey(Col("p.uid")),))
    top = evaluate_core(Limit(ordered, 2), store)
    assert [r["p.uid"] for r in top.rows()] == ["u1", "u2"]
    page = evaluate_core(Limit(ordered, 2, offset=2), store)
    assert [r["p.uid"] for r in page.rows()] == ["u3", "u4"]


def test_property_predicate_uses_the_shared_type_fit_rule(store):
    """D-052 lives in `tgms/temporal/props.py` and is shared by the kernel, the
    oracle and the SQL twins. Text is never parsed into a number."""
    rel = evaluate_core(PropertyPredicate(NodeScan("p"), "p", "w", ">", 3), store)
    assert sorted(r["p.uid"] for r in rel.rows()) == ["u4", "u5"]
    mismatched = evaluate_core(
        PropertyPredicate(NodeScan("p"), "p", "w", ">", "3"), store)
    assert mismatched.n == 0, '"3" > 3 is not a comparison that returned false'


def test_property_predicate_reports_its_coercion_denominator(store):
    """§2.5's reason for existing separately from `Filter`: an answer must not
    rest on a shrunken denominator without saying so."""
    node = PropertyPredicate(NodeScan("p"), "p", "w", ">", "3")
    run = Execution(store, live_columns(node))
    run.run(node)
    counts = run.coercion[node.node_digest]
    assert counts["considered"] == 5 and counts["skipped"] == 5
    assert counts["matched"] == 0


def test_type_constraint_is_a_union_list_not_a_hierarchy(store):
    """§8.17: LDBC's `Message` compiles as `labels: ["Post", "Comment"]` at
    bind time; the IR carries no subtyping."""
    rel = evaluate_core(TypeConstraint(NodeScan("p"), "p", labels=("N", "M")), store)
    assert rel.n == 5
    one = evaluate_core(TypeConstraint(NodeScan("p"), "p", labels=("M",)), store)
    assert sorted(r["p.uid"] for r in one.rows()) == ["u2", "u4"]


def test_join_inner_multiplies_multiplicities(store):
    """Bag semantics (§2.8, §8.4 CLOSED): duplicate keys on either side
    multiply, and that ruling is load-bearing for bo31 and BI18 on any
    corrected store."""
    write(store, [node_op("u1", "N", {"w": 11}, vt_s=100, vt_e=200)], 3)
    left = Project(NodeScan("p", uids=("u1",)), (("k", Col("p.uid")),))
    right = Project(NodeScan("q", uids=("u1",)), (("k2", Col("q.uid")),))
    assert evaluate_core(Join(left, right, (("k", "k2"),)), store).n == 4


def test_join_emits_left_order_then_right_order(store):
    left = Project(Order(NodeScan("p"), (SortKey(Col("p.uid"), "desc"),)),
                   (("k", Col("p.uid")),))
    right = Project(NodeScan("q"), (("k2", Col("q.uid")),))
    got = evaluate_core(Join(left, right, (("k", "k2"),)), store)
    assert [r["k"] for r in got.rows()] == ["u5", "u4", "u3", "u2", "u1"]


def test_a_null_join_key_is_an_error_not_a_non_match():
    """§2.8, checked before the build so the failure names the key."""
    from tgms.tgir.eval.select import check_join_keys

    schema = Schema.of(Column("k", T_INT.optional()))
    rel = Relation(schema, {"k": np.array([1, 0])}, 2, {"k": np.array([False, True])})
    with pytest.raises(InvalidArgError, match="null join key"):
        check_join_keys(rel, ("k",), "left")


# ---------------------------------------------------------------------------
# column pruning, the ∅ guard, and the remaining seams
# ---------------------------------------------------------------------------

def test_pruning_changes_which_arrays_are_built_and_nothing_else(store):
    """§3.7: pruning is not a plan rewrite. It changes no node's arguments, so
    no `node_digest` and no dependency scope moves — and it changes no row."""
    plan = Project(NodeScan("p"), (("uid", Col("p.uid")),))
    live = live_columns(plan)
    assert live[plan.input.node_digest] == frozenset({"p.uid"})
    pruned = evaluate_core(plan, store)
    unpruned = Execution(store, None).run(plan)
    assert pruned.rows() == unpruned.rows()
    assert plan.node_digest == Project(
        NodeScan("p"), (("uid", Col("p.uid")),)).node_digest


def test_pruning_keeps_what_a_predicate_reads_even_when_the_root_drops_it(store):
    plan = Project(Filter(NodeScan("p"), Cmp("=", Col("p.label"), Lit("N"))),
                   (("uid", Col("p.uid")),))
    live = live_columns(plan)
    scan = plan.input.input
    assert live[scan.node_digest] == frozenset({"p.uid", "p.label"})


def test_a_shared_subtree_is_evaluated_once(store):
    scan = NodeScan("p")
    left = Project(scan, (("k", Col("p.uid")),))
    right = Project(scan, (("k2", Col("p.uid")),))
    run = Execution(store, live_columns(Join(left, right, (("k", "k2"),))))
    run.run(Join(left, right, (("k", "k2"),)))
    assert scan.node_digest in run.memo


def test_the_empty_scope_guard_is_live_for_the_core(store):
    """§3.3: only the four `reads_store` node kinds ever see the adapter. A
    pure evaluator that started reading store state would raise by name."""
    from tgms.tgir.guard import NullAdapter, adapter_for

    assert isinstance(adapter_for(Filter(NodeScan("p"), Lit(True)), store),
                      NullAdapter)
    assert adapter_for(NodeScan("p"), store) is store
    with pytest.raises(StateError, match="nodes_columnar"):
        NullAdapter().nodes_columnar()


def test_the_not_yet_built_nodes_name_their_phase(store):
    """`evaluate.py`'s `NotImplementedError` shrank to exactly the node kinds
    M3.0 does not build."""
    seed = NodeScan("p", uids=("u1",))
    # `Expand` landed in M3.1, so it no longer raises — the seam shrank again
    assert evaluate_core(Expand(seed, "p", "q", Exact(1)), store) is not None
    with pytest.raises(NotImplementedError, match="M3.2"):
        evaluate_core(Aggregate(seed, (), (Agg("count", "n"),)), store)
    with pytest.raises(NotImplementedError, match="M3.2"):
        evaluate_core(PatternMatch(Pattern((NodePat("a"), NodePat("b")),
                                           (_edge_pat(),))), store)
    left = Project(seed, (("k", Col("p.uid")),))
    right = Project(NodeScan("q", uids=("u1",)), (("k2", Col("q.uid")),))
    with pytest.raises(NotImplementedError, match="E_INCOMPLETE"):
        evaluate_core(Join(left, right, (("k", "k2"),), join_type="anti"), store)


def _edge_pat():
    from tgms.tgir.node import EdgePat
    return EdgePat("e1", "a", "b")
