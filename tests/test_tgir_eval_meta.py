"""M3.2 — §5.3's metadata propagation and the `Domain` record.

§7.5 says this is "easy to get subtly wrong", and names the four places. These
are property tests rather than examples wherever the property is what matters:
the lattice rules hold over *every* plan shape built here, not over the three a
worked example happens to use.

The one that carries the most weight is the `Aggregate` domain-fall. It is
§5.3 rule 3's **single stated exception** to "no operator's completeness is ever
stronger than its inputs'", and its guard is domain **equality** — certifying
"these are exactly the 10 greatest under ⟨key⟩" is sound, certifying "these are
exactly the person's messages" is not, and only the domain distinguishes them.
The gate review's RG-2 verification is the abuse case, and it must fail.
"""

from __future__ import annotations

import pytest

from tgms.core.errors import LimitError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.domain import Domain
from tgms.tgir.eval import Execution
from tgms.tgir.expr import Col, Cmp, Lit
from tgms.tgir.metadata import (
    Completeness, Exactness, ResultMeta, comparable, le, meet,
)
from tgms.tgir.node import (
    Agg, Aggregate, EdgeScan, Exact, Expand, Filter, Join, Limit, NodeScan, Order,
    Project, PropertyPredicate, SortKey, TypeConstraint, Unbounded,
)
from tgms.tgir.propagate import EXECUTION_COMPLETE, is_execution_complete, meta_for
from tgms.tgir.prune import live_columns
from tgms.tgir.types import Sigma

from .conftest import fresh_adapter


def write(adapter, ops, tt):
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


@pytest.fixture()
def store():
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [{"op": "assert_node", "uid": f"u{i}", "label": "N",
               "props": {"w": i}, "vt_s": 0, "vt_e": 100, "source": "i",
               "provenance_ref": None} for i in range(1, 6)], 1)
    write(a, [{"op": "assert_edge", "src": "u1", "dst": f"u{i}", "rel_type": "R",
               "props": {}, "vt_s": 10, "vt_e": 90, "disc": str(i),
               "source": "i", "provenance_ref": None} for i in (2, 3)], 2)
    yield a
    a.close()


def run(node, store):
    execution = Execution(store, live_columns(node))
    execution.run(node)
    return execution


def meta(node, store):
    return run(node, store).meta[node.node_digest]


# ---------------------------------------------------------------------------
# 1-3: the lattice rules, as properties over every plan shape here
# ---------------------------------------------------------------------------

def plans():
    """A spread of shapes: each is `(label, node)`."""
    scan = NodeScan("p")
    filtered = Filter(scan, Cmp("!=", Col("p.label"), Lit("nope")))
    projected = Project(filtered, (("k", Col("p.uid")),))
    ordered = Order(scan, (SortKey(Col("p.uid")),))
    return [
        ("scan", scan),
        ("filter", filtered),
        ("project", projected),
        ("type-constraint", TypeConstraint(scan, "p", labels=("N",))),
        ("property", PropertyPredicate(scan, "p", "w", ">", 1)),
        ("order", ordered),
        ("top-k", Limit(ordered, 2)),
        ("page", Limit(scan, 2)),
        ("expand", Expand(NodeScan("p", uids=("u1",)), "p", "q", Exact(1))),
        ("unbounded", Expand(NodeScan("p", uids=("u1",)), "p", "q", Unbounded(1))),
        ("join", Join(Project(scan, (("k", Col("p.uid")),)),
                      Project(NodeScan("q"), (("k2", Col("q.uid")),)),
                      (("k", "k2"),))),
        ("aggregate", Aggregate(scan, (), (Agg("count", "n"),))),
    ]


@pytest.mark.parametrize("label,node", plans(), ids=[p[0] for p in plans()])
def test_no_nodes_completeness_exceeds_the_meet_of_its_inputs(label, node, store):
    """§5.3 rule 3, as a property.

    "An operator may lower it (a `Limit` making `top-k` or `paginated`) but
    never raise it." The single exception is the `Aggregate` domain-fall, and it
    is not a raise: the enum value rises only because the domain fell, which the
    next test checks separately.
    """
    execution = run(node, store)
    got = execution.meta[node.node_digest]
    inputs = [execution.meta[i.node_digest] for i in node.inputs]
    if not inputs:
        assert got.completeness is Completeness.COMPLETE
        return
    bound = inputs[0].completeness
    for other in inputs[1:]:
        bound = meet(bound, other.completeness)
    if isinstance(node, Aggregate):
        assert got.domain == inputs[0].domain, "the exception is a domain fall"
        return
    assert le(got.completeness, bound) or got.completeness is Completeness.UNKNOWN


def test_refused_is_absorbing_through_the_propagation(store):
    """⊥ absorbs: any operator with a refused input is itself refused, since
    there is nothing to compute over."""
    scan = NodeScan("p")
    node = Filter(scan, Lit(True))
    refused = ResultMeta(Sigma.default(), Completeness.REFUSED,
                         domain=Domain.of(Sigma.default()))
    got = meta_for(node, (refused,))
    assert got.completeness is Completeness.REFUSED
    assert meet(Completeness.REFUSED, Completeness.COMPLETE) is Completeness.REFUSED


def test_join_inner_over_mixed_inputs_drops_to_unknown(store):
    """A **deliberate** drop below the meet: no ranking key and no cursor
    survives a join, so the output is not a `top-k` or a page of anything."""
    left = Project(Limit(Order(NodeScan("p"), (SortKey(Col("p.uid")),)), 2),
                   (("k", Col("p.uid")),))
    right = Project(NodeScan("q"), (("k2", Col("q.uid")),))
    node = Join(left, right, (("k", "k2"),))
    execution = run(node, store)
    assert execution.meta[left.node_digest].completeness is Completeness.TOP_K
    assert execution.meta[right.node_digest].completeness is Completeness.COMPLETE
    assert execution.meta[node.node_digest].completeness is Completeness.UNKNOWN
    # ... and below the meet, which would have been `top-k`
    assert meet(Completeness.TOP_K, Completeness.COMPLETE) is Completeness.TOP_K


# ---------------------------------------------------------------------------
# 4-5: the Aggregate exception and its abuse
# ---------------------------------------------------------------------------

def test_the_aggregate_domain_fall_is_guarded_by_domain_equality(store):
    """The exception, stated as the spec states it: `complete` **over that
    narrowed domain**."""
    top = Limit(Order(NodeScan("p"), (SortKey(Col("p.uid")),)), 2)
    node = Aggregate(top, (), (Agg("count", "n"),))
    execution = run(node, store)
    got = execution.meta[node.node_digest]
    assert got.completeness is Completeness.COMPLETE
    assert got.domain == execution.meta[top.node_digest].domain
    assert got.domain.is_narrowed, "the domain fell; that is why the enum rose"


def test_the_wide_domain_abuse_must_fail(store):
    """RG-2's verification. Certifying an aggregate over a `top-k` input as
    complete **over the un-narrowed domain** is the false certification the
    lattice exists to foreclose: counting ten rows and presenting them as the
    exact population.

    So the guard is equality, not implication — the aggregate's domain must
    *be* its input's, and a domain that dropped the `top-k` narrowing is a
    different domain.
    """
    scan = NodeScan("p")
    top = Limit(Order(scan, (SortKey(Col("p.uid")),)), 2)
    node = Aggregate(top, (), (Agg("count", "n"),))
    execution = run(node, store)
    aggregate_domain = execution.meta[node.node_digest].domain
    wide_domain = execution.meta[scan.node_digest].domain

    assert aggregate_domain != wide_domain
    assert not le(Completeness.COMPLETE, Completeness.TOP_K), \
        "complete over the wide domain would be a genuine raise"
    # the abuse, spelled out: the same count presented over the wide domain
    forged = ResultMeta(node.sigma, Completeness.COMPLETE, domain=wide_domain)
    assert forged.domain != aggregate_domain, \
        "an implementation that reset the domain would be certifying a lie"


# ---------------------------------------------------------------------------
# 6-8: the narrative cases §7.3 and §5.3 name
# ---------------------------------------------------------------------------

def test_is2s_top_k_inheritance(store):
    """§5.3's worked case. `s5`'s `Limit(10)` makes the relation `top-k`, `s6`
    (an unbounded `Expand`) **inherits** it, and the plan's result is
    `top-k, exact`.

    Had `s6` output `complete`, an `Aggregate` above it would license
    `@ExactCardinality` over ten rows presented as the exact population.
    """
    s5 = Limit(Order(NodeScan("p", uids=("u1",)), (SortKey(Col("p.uid")),)), 10)
    s6 = Expand(s5, "p", "q", Unbounded(1))
    execution = run(s6, store)
    assert execution.meta[s5.node_digest].completeness is Completeness.TOP_K
    assert execution.meta[s6.node_digest].completeness is Completeness.TOP_K
    assert execution.meta[s6.node_digest].exactness is Exactness.EXACT


def test_a_page_cut_is_paginated_and_a_top_k_is_top_k(store):
    """§2.12's two uses over identical rows: what differs is the metadata, and
    therefore what a caller may certify."""
    ordered = Order(NodeScan("p"), (SortKey(Col("p.uid")),))
    assert meta(Limit(ordered, 2), store).completeness is Completeness.TOP_K
    assert meta(Limit(NodeScan("p"), 2), store).completeness is Completeness.PAGINATED


def test_a_limit_over_a_non_complete_input_takes_the_meet(store):
    """"a **non-complete** input gives the meet of its value with `top-k` —
    `unknown` whenever the two are incomparable"."""
    paged = Limit(NodeScan("p"), 3)
    top = Limit(Order(paged, (SortKey(Col("p.uid")),)), 2)
    execution = run(top, store)
    assert execution.meta[paged.node_digest].completeness is Completeness.PAGINATED
    assert execution.meta[top.node_digest].completeness is Completeness.UNKNOWN
    assert not comparable(Completeness.PAGINATED, Completeness.TOP_K)


def test_the_selections_preserve_completeness_and_narrow_the_domain(store):
    """"preserved; domain narrows by the predicate" — for all three."""
    scan = NodeScan("p")
    for node in (Filter(scan, Cmp("!=", Col("p.label"), Lit("x"))),
                 TypeConstraint(scan, "p", labels=("N",)),
                 PropertyPredicate(scan, "p", "w", ">", 1)):
        execution = run(node, store)
        got = execution.meta[node.node_digest]
        assert got.completeness is Completeness.COMPLETE
        assert got.domain.is_narrowed
        assert len(got.domain.narrowings) == 1
        assert got.domain.sigma == execution.meta[scan.node_digest].domain.sigma


# ---------------------------------------------------------------------------
# 9-10: execution-completeness, and what a scan starts from
# ---------------------------------------------------------------------------

def test_execution_completeness_is_the_precondition_predicate(store):
    """`paginated` is execution-complete — delivery is incomplete, execution is
    not — and that is exactly what the two absence preconditions turn on.
    `unknown` is not: it asserts the absence of certification."""
    assert EXECUTION_COMPLETE == {Completeness.COMPLETE, Completeness.PAGINATED,
                                  Completeness.TOP_K}
    for value, expected in ((Completeness.COMPLETE, True),
                            (Completeness.PAGINATED, True),
                            (Completeness.TOP_K, True),
                            (Completeness.SAMPLED, False),
                            (Completeness.TIMEOUT_TRUNCATED, False),
                            (Completeness.UNKNOWN, False),
                            (Completeness.REFUSED, False)):
        assert is_execution_complete(
            ResultMeta(Sigma.default(), value)) is expected


def test_a_scan_is_complete_over_sigma_and_its_domain_starts_there(store):
    """§5.3's two scan rows: leaves with no inputs and no meet to take."""
    sigma = Sigma.in_window(0, 50)
    for node in (NodeScan("p", sigma_=sigma), EdgeScan("e", sigma_=sigma)):
        got = meta(node, store)
        assert got.completeness is Completeness.COMPLETE
        assert got.exactness is Exactness.EXACT
        assert got.domain == Domain.of(sigma)
        assert not got.domain.is_narrowed


def test_the_domain_is_append_only_along_a_plan(store):
    """Every narrowing is recorded, and none is ever removed — a domain never
    widens, because no operator widens what an answer is about."""
    scan = NodeScan("p")
    one = Filter(scan, Cmp("!=", Col("p.label"), Lit("a")))
    two = Filter(one, Cmp("!=", Col("p.label"), Lit("b")))
    three = Limit(Order(two, (SortKey(Col("p.uid")),)), 2)
    execution = run(three, store)
    lengths = [len(execution.meta[n.node_digest].domain.narrowings)
               for n in (scan, one, two, three)]
    assert lengths == [0, 1, 2, 3]
    kinds = [n.kind for n in execution.meta[three.node_digest].domain.narrowings]
    assert kinds == ["predicate", "predicate", "top-k"]


def test_metadata_exists_at_every_node_not_only_the_root(store):
    """§5: "from day one, at **every** node — not only at the plan's root"."""
    scan = NodeScan("p")
    node = Project(Filter(scan, Lit(True)), (("k", Col("p.uid")),))
    execution = run(node, store)
    assert set(execution.meta) == {n.node_digest for n in
                                   (scan, node.input, node)}
    for got in execution.meta.values():
        assert got.completeness is not None and got.domain is not None


def test_incompleteness_refusal_is_raised_before_the_work(store):
    """A precondition refusal must leave no partial relation behind — so the
    metadata is computed before the evaluator runs, not after."""
    left = Project(NodeScan("p"), (("k", Col("p.uid")),))
    right = Project(NodeScan("q"), (("k2", Col("q.uid")),))
    node = Join(left, right, (("k", "k2"),), join_type="anti")
    execution = Execution(store, live_columns(node))
    execution.run(right)
    execution.meta[right.node_digest] = ResultMeta(
        Sigma.default(), Completeness.TIMEOUT_TRUNCATED,
        domain=Domain.of(Sigma.default()))
    with pytest.raises(LimitError):
        execution.run(node)
    assert node.node_digest not in execution.memo
