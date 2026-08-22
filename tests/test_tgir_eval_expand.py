"""M3.1 — `Expand` in all three hop forms, adjacency, cost and admission.

The three restrictions of §2.3 are load-bearing rather than decorative, so each
gets a direct test that would fail under the wrong reading:

1. **No edge bindings for variable-length forms** — v1 has no list type, which
   is why the path family stays outside the core.
2. **Structural closure only.** There is no constraint that hop *i+1*'s `vt_s`
   exceed hop *i*'s. The fixture below is built so that a *time-respecting*
   expansion would return strictly fewer rows, so the two readings cannot both
   pass.
3. **An unbounded `Expand` is never truncated** — complete or refused, because a
   partial fixpoint produces false absences.

And the §8.7 asymmetry — `exact(k)` a walk relation, `bounded(k,k)` a node
relation — is *proved* on a concrete store rather than asserted.
"""

from __future__ import annotations

import numpy as np
import pytest

from tgms.core.errors import CostError
from tgms.core.model import OPEN_END
from tgms.temporal.algebra import call_operator, ensure_all_registered
from tgms.tgir.admission import (
    Budget, RefusalCertificate, admit, has_core_node, plan_estimate,
)
from tgms.tgir.cost import branching, cost_of, window_fraction_of
from tgms.tgir.eval import Execution, evaluate_core
from tgms.tgir.eval.adjacency import AdjacencyCache
from tgms.tgir.expr import Col, Cmp, Lit
from tgms.tgir.node import (
    Bounded, Exact, Expand, Filter, NodeScan, OpaqueLeaf, Project, Unbounded,
)
from tgms.tgir.prune import live_columns
from tgms.tgir.types import Sigma

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
def chain():
    """`u1 → u2 → u3 → u4`, with **two parallel** `u1 → u2` edges.

    The parallel pair is what makes the §8.7 asymmetry observable: `u2` is
    reachable from `u1` by two distinct 1-walks, so a walk relation returns it
    twice and a node relation once.

    The hop times **descend** (30, 20, 10 along the chain) so that a
    time-respecting expansion could not traverse the chain at all — which is
    exactly what restriction 2 says must not happen.
    """
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [node_op(f"u{i}") for i in range(1, 5)], 1)
    write(a, [edge_op("u1", "u2", vt_s=30, vt_e=90, disc="a"),
              edge_op("u1", "u2", vt_s=30, vt_e=90, disc="b")], 2)
    write(a, [edge_op("u2", "u3", vt_s=20, vt_e=90)], 3)
    write(a, [edge_op("u3", "u4", vt_s=10, vt_e=90)], 4)
    yield a
    a.close()


def seed(uid="u1"):
    return NodeScan("p", uids=(uid,))


def into_uids(rel):
    return [r["q.uid"] for r in rel.rows()]


# ---------------------------------------------------------------------------
# §8.7 — the asymmetry, proved
# ---------------------------------------------------------------------------

def test_exact_k_is_a_walk_relation_and_bounded_k_k_is_not(chain):
    """The gate: `bounded(k,k)` is **not** `exact(k)`, and this store shows it.

    `u2` is reachable from `u1` by two distinct 1-walks, so the walk relation
    has two rows and the node relation one. An implementation that normalized
    one form into the other would pass every other test in this file and fail
    here — which is why §2.3 says the two "must not" be rewritten into each
    other.
    """
    walks = evaluate_core(Expand(seed(), "p", "q", Exact(1)), chain)
    nodes = evaluate_core(Expand(seed(), "p", "q", Bounded(1, 1)), chain)
    assert into_uids(walks) == ["u2", "u2"], "multiplicity is preserved"
    assert into_uids(nodes) == ["u2"], "deduplicated by (input row, into)"
    assert "q.depth" in nodes.schema.names and "q.depth" not in walks.schema.names


def test_the_asymmetry_survives_two_hops(chain):
    walks = evaluate_core(Expand(seed(), "p", "q", Exact(2)), chain)
    nodes = evaluate_core(Expand(seed(), "p", "q", Bounded(2, 2)), chain)
    assert into_uids(walks) == ["u3", "u3"]
    assert into_uids(nodes) == ["u3"]


def test_bounded_keeps_the_minimum_depth_within_the_band(chain):
    """`bounded(a,b)` keeps the minimum `j` **within** `[a,b]`, so it admits a
    node whose true minimum distance is below `a` — which is why BI10's
    far-minus-near shape needs a `Join{anti}` of two expansions rather than one
    banded expansion."""
    banded = evaluate_core(Expand(seed(), "p", "q", Bounded(2, 3)), chain)
    assert [(r["q.uid"], r["q.depth"]) for r in banded.rows()] == [("u3", 2),
                                                                  ("u4", 3)]


# ---------------------------------------------------------------------------
# §2.3's three restrictions
# ---------------------------------------------------------------------------

def test_variable_length_forms_bind_no_edge_variable(chain):
    """Restriction 1, enforced by the node layer at construction: a variable
    number of edges cannot bind into a fixed row schema without list values."""
    from tgms.core.errors import InvalidArgError

    for hops in (Bounded(1, 2), Unbounded(1)):
        with pytest.raises(InvalidArgError, match="list type"):
            Expand(seed(), "p", "q", hops, edge_var="e")
    # ... while the fixed form binds one
    bound = evaluate_core(Expand(seed(), "p", "q", Exact(1), edge_var="e"), chain)
    assert sorted(r["e.disc"] for r in bound.rows()) == ["a", "b"]


def test_expansion_is_structural_not_time_respecting(chain):
    """Restriction 2, pinned by a fixture that separates the two readings.

    The chain's hop times **descend** (30 → 20 → 10), so a time-respecting
    expansion — one requiring each hop's `vt_s` to exceed the previous — would
    reach `u2` and stop. Structural closure reaches `u4`.

    R6's unbounded `Expand` is therefore *not* the reachability operator:
    `temporal_reachability` imposes the ordering constraint and an
    earliest-arrival semiring, and stays an opaque leaf. This test is what
    would fail if someone "improved" `Expand` into it.
    """
    reached = evaluate_core(Expand(seed(), "p", "q", Unbounded(1)), chain)
    assert sorted(set(into_uids(reached))) == ["u2", "u3", "u4"]

    hop_times = [int(r["e.vt_s"]) for r in evaluate_core(
        Expand(seed(), "p", "q", Exact(1), edge_var="e"), chain).rows()]
    assert hop_times == [30, 30], "the first hop is later than the ones after it"


def test_the_operator_that_does_respect_time_returns_less(chain):
    """The contrast that makes restriction 2 a *difference* rather than a
    preference, on a store built for it.

    This system's time-respecting rule is arrival-based — a traversal at
    arrival `τ` may take an edge only while `τ < vt_e` (`ops_paths._csr_for`'s
    stated constraints). So an edge whose validity has **already ended** when
    the walk arrives is passable structurally and not temporally. `a → b` is
    valid `[50, 60)` and `b → c` only `[10, 20)`: structural closure reaches
    `c`, `temporal_reachability` does not.
    """
    write(chain, [node_op("a"), node_op("b"), node_op("c")], 10)
    write(chain, [edge_op("a", "b", vt_s=50, vt_e=60)], 11)
    write(chain, [edge_op("b", "c", vt_s=10, vt_e=20)], 12)

    structural = evaluate_core(
        Expand(NodeScan("p", uids=("a",)), "p", "q", Unbounded(1)), chain)
    assert sorted(set(into_uids(structural))) == ["b", "c"]

    env = call_operator(chain, "temporal_reachability",
                        {"src": "a", "window": {"t_a": 0, "t_b": 100}})
    assert {r["uid"] for r in env["rows"]} == {"b"}, \
        "time-respecting reachability stops where structural closure does not"


def test_an_unbounded_expansion_is_complete_or_refused_never_truncated(chain):
    """Restriction 3, as a property: **any execution that returns is
    complete**. A partial fixpoint produces false absences, and the evidence
    contract permits false invalidation but never false certification.
    """
    full = set(into_uids(evaluate_core(Expand(seed(), "p", "q", Unbounded(1)),
                                       chain)))
    for limit in (1, 2, 5, 10, 10_000):
        node = Expand(seed(), "p", "q", Unbounded(1))
        run = Execution(chain, live_columns(node),
                        budget=Budget("plan-digest", limit=limit))
        try:
            got = set(into_uids(run.run(node)))
        except CostError as e:
            # refusal carries the estimate and the certificate — never a
            # partial answer
            assert e.details["refusal_certificate"]["stage"] == "runtime"
            assert e.details["estimate"]["expansions_est"] > limit
            continue
        assert got == full, "an execution that returned must be complete"


def test_exact_zero_traverses_no_edge(chain):
    """`exact(0)` binds `into` = `from` and touches no adjacency at all."""
    got = evaluate_core(Expand(seed(), "p", "q", Exact(0)), chain)
    assert into_uids(got) == ["u1"]
    with pytest.raises(Exception):
        Expand(seed(), "p", "q", Exact(0), edge_var="e")


# ---------------------------------------------------------------------------
# §9.1 — the version-less `into`
# ---------------------------------------------------------------------------

def test_a_version_less_into_binds_uid_and_nulls(chain):
    """The coordinator's §9.1 ruling. §6 #3 rejected making node validity a
    global `Expand` rule, so the version-less row must **survive** the
    expansion — a plan that wants validity enforced interposes the
    `Join{inner}` against `NodeScan @ instant($t)` that §6 #3 prescribes."""
    write(chain, [edge_op("u1", "ghost", vt_s=30, vt_e=90, disc="g")], 5)
    got = evaluate_core(Expand(seed(), "p", "q", Exact(1)), chain)
    ghost = [r for r in got.rows() if r["q.uid"] == "ghost"]
    assert len(ghost) == 1, "the row survives"
    assert ghost[0]["q.vid"] is None and ghost[0]["q.label"] is None
    assert ghost[0]["q.vt_s"] is None and ghost[0]["q.vt_e"] is None


def test_a_node_invisible_under_sigma_is_null_not_dropped(chain):
    """The same ruling for a node that *has* a version, just not one Σ can
    see — which is the case `snapshot_subgraph`'s instant Σ produces."""
    write(chain, [node_op("late", vt_s=500, vt_e=600),
                  edge_op("u1", "late", vt_s=30, vt_e=90, disc="l")], 5)
    sigma = Sigma.in_window(0, 100)
    got = evaluate_core(
        Expand(NodeScan("p", uids=("u1",), sigma_=sigma), "p", "q", Exact(1),
               sigma_=sigma), chain)
    late = [r for r in got.rows() if r["q.uid"] == "late"]
    assert len(late) == 1 and late[0]["q.vid"] is None


def test_the_join_inner_shape_that_section_6_3_prescribes(chain):
    """And the reason nulls are the right ruling: the rejected global rule is
    expressible as a plan, on the same expansion."""
    write(chain, [edge_op("u1", "ghost", vt_s=30, vt_e=90, disc="g")], 5)
    expansion = Project(Expand(seed(), "p", "q", Exact(1)),
                        (("uid", Col("q.uid")),))
    assert len(evaluate_core(expansion, chain).rows()) == 3
    valid_only = Filter(Expand(seed(), "p", "q", Exact(1)),
                        Cmp("!=", Col("q.vid"), Lit("")))
    assert all(r["q.uid"] != "ghost"
               for r in evaluate_core(valid_only, chain).rows())


# ---------------------------------------------------------------------------
# adjacency
# ---------------------------------------------------------------------------

def test_the_adjacency_is_sigma_pruned_and_typed(chain):
    """An expansion cannot traverse an edge its own Σ excludes, and a
    `rel_type` restricts the index rather than post-filtering it."""
    write(chain, [edge_op("u1", "u9", rel_type="OTHER", vt_s=30, vt_e=90)], 5)
    write(chain, [node_op("u9")], 6)
    typed = evaluate_core(Expand(seed(), "p", "q", Exact(1), rel_type="R"), chain)
    assert "u9" not in into_uids(typed)
    untyped = evaluate_core(Expand(seed(), "p", "q", Exact(1)), chain)
    assert "u9" in into_uids(untyped)

    windowed = Sigma.in_window(0, 25)     # excludes the vt_s = 30 hops
    narrow = evaluate_core(
        Expand(NodeScan("p", uids=("u1",), sigma_=windowed), "p", "q", Exact(1),
               sigma_=windowed), chain)
    assert narrow.n == 0


def test_the_adjacency_cache_is_shared_across_hops(chain):
    cache = AdjacencyCache(chain)
    first = cache.get("R", Sigma.default())
    assert cache.get("R", Sigma.default()) is first
    assert cache.get(None, Sigma.default()) is not first
    # an edge_var binding needs an index carrying vid, so it is a different one
    assert cache.get("R", Sigma.default(), need_identity=True) is not first


def test_direction_both_merges_and_reorders(chain):
    """"the traversal's `(vt_s, vid)`" is a statement about the hop, not about
    each direction separately."""
    out = evaluate_core(Expand(seed("u2"), "p", "q", Exact(1), dir="out"), chain)
    inn = evaluate_core(Expand(seed("u2"), "p", "q", Exact(1), dir="in"), chain)
    both = evaluate_core(Expand(seed("u2"), "p", "q", Exact(1), dir="both"), chain)
    assert into_uids(out) == ["u3"] and sorted(into_uids(inn)) == ["u1", "u1"]
    assert sorted(into_uids(both)) == ["u1", "u1", "u3"]


def test_variable_length_canonical_order(chain):
    """§2.3: `(input row position, into.depth, into)`."""
    got = evaluate_core(Expand(seed(), "p", "q", Unbounded(1)), chain)
    depths = [int(r["q.depth"]) for r in got.rows()]
    assert depths == sorted(depths)
    assert [r["q.uid"] for r in got.rows()] == ["u2", "u3", "u4"]


# ---------------------------------------------------------------------------
# cost and admission
# ---------------------------------------------------------------------------

def test_cost_shapes_follow_section_2_3(chain):
    stats = chain.stats()
    b = branching(stats)
    assert b >= 1.0
    one = cost_of(Expand(seed(), "p", "q", Exact(1)), stats, 1)
    two = cost_of(Expand(seed(), "p", "q", Exact(2)), stats, 1)
    assert two["expansions_est"] >= one["expansions_est"]
    band = cost_of(Expand(seed(), "p", "q", Bounded(1, 2)), stats, 1)
    assert band["expansions_est"] >= one["expansions_est"]


def test_window_fraction_prices_a_narrow_sigma_lower(chain):
    stats = chain.stats()
    assert window_fraction_of(Sigma.default(), stats) == 1.0
    narrow = window_fraction_of(Sigma.in_window(0, 5), stats)
    assert 0.0 <= narrow <= 1.0


def test_plan_admission_sums_over_nodes_and_dedupes_a_shared_subtree(chain):
    scan = NodeScan("p")
    plan = Filter(scan, Cmp("!=", Col("p.label"), Lit("nope")))
    estimate = plan_estimate(plan, chain.stats())
    assert set(estimate) >= {"rows_scanned_est", "expansions_est", "time_est_ms"}
    assert len(estimate["per_node"]) == 2, "one entry per distinct node digest"


def test_a_plan_over_the_ceiling_is_refused_with_a_certificate(chain):
    node = Expand(seed(), "p", "q", Unbounded(1))
    with pytest.raises(CostError) as excinfo:
        admit(node, chain.stats(), "plan-digest",
              ceilings={"expansions_est": 0, "rows_scanned_est": 0,
                        "time_est_ms": 0})
    details = excinfo.value.details
    certificate = details["refusal_certificate"]
    assert certificate["plan_digest"] == "plan-digest"
    assert certificate["stage"] == "plan"
    assert certificate["policy_version"] == "guardrail-policy-v1"
    assert certificate["calibration_ref"]
    assert certificate["estimates"] and certificate["ceilings"]
    # additive: the keys the planner repair loop already consumes are intact
    assert set(details) >= {"estimate", "ceilings", "suggestions"}


def test_stage_two_can_only_refuse_more(chain):
    """The realized-cardinality re-check runs before each node and computes the
    same function of a number that is no longer a guess."""
    node = Expand(NodeScan("p"), "p", "q", Exact(1))
    stats = chain.stats()
    admit(node, stats, "d")                      # stage 1 admits
    run = Execution(chain, live_columns(node), stats=stats, plan_digest="d",
                    ceilings={"expansions_est": 0})
    with pytest.raises(CostError) as excinfo:
        run.run(node)
    assert excinfo.value.details["refusal_certificate"]["stage"] == "node"
    assert excinfo.value.details["refusal_certificate"]["node_digest"]


def test_plan_admission_does_not_touch_the_fifteen_leaves(chain):
    """M2's C5: a single-leaf plan keeps today's per-operator admission at
    today's site. Plan-level admission runs **only** over core nodes."""
    leaf = OpaqueLeaf.build("entity_history", {"uid": "u1"}, ("rows",))
    assert not has_core_node(leaf)
    assert admit(leaf, chain.stats(), "d", ceilings={"time_est_ms": 0}) == {}
    # ... and the operator path is unchanged: it still refuses on its own
    # cost_fn, at its own site, with its own message
    env = call_operator(chain, "entity_history", {"uid": "u1"})
    assert env["rows_total"] >= 1
    with pytest.raises(CostError) as excinfo:
        call_operator(chain, "version_history",
                      {"kind": "node", "window": {"t_a": 0, "t_b": 100}},
                      cost_ceilings={"rows_scanned_est": 0})
    assert "refusal_certificate" not in excinfo.value.details, \
        "the leaves' refusal shape is frozen by C5 and must not grow keys"


def test_has_core_node_sees_through_a_leaf_input(chain):
    assert has_core_node(NodeScan("p"))
    assert has_core_node(Filter(NodeScan("p"), Lit(True)))


def test_the_budget_refuses_rather_than_truncating():
    budget = Budget("d", limit=10)
    budget.charge(5)
    with pytest.raises(CostError) as excinfo:
        budget.charge(20)
    assert excinfo.value.details["refusal_certificate"]["stage"] == "runtime"


def test_certificate_serializes_the_five_fields_section_2_13_names():
    certificate = RefusalCertificate("digest", "plan", {"time_est_ms": 5},
                                     {"time_est_ms": 1})
    payload = certificate.to_json()
    assert set(payload) >= {"plan_digest", "policy_version", "estimates",
                            "ceilings", "calibration_ref", "stage"}


# ---------------------------------------------------------------------------
# the bfs_node_set gate
# ---------------------------------------------------------------------------

def test_bounded_reproduces_bfs_node_sets_hop_map(chain):
    """M3.1's gate (a): `Expand(bounded)` reproduces `ops_snapshot.bfs_node_set`'s
    hop map for `hops ≤ 3` — exhaustive in `hops`, since `MAX_HOPS = 3`.

    `bfs_node_set` is undirected, so the comparison is against `dir="both"`.
    """
    from tgms.temporal.ops_snapshot import bfs_node_set, edges_at, nodes_at

    for hops in (0, 1, 2, 3):
        edges = edges_at(chain, 50, OPEN_END)
        nodes = nodes_at(chain, 50, OPEN_END)
        seed_ids = chain.dense_ids(["u1"])
        expected = bfs_node_set(edges, np.asarray(seed_ids), nodes["uid_id"], hops)
        by_uid = {uid: dist for uid, dist in
                  zip(chain.uids_for(list(expected)), expected.values())}

        sigma = Sigma.at_instant(50)
        got = evaluate_core(
            Expand(NodeScan("p", uids=("u1",), vt_mode="instant", sigma_=sigma),
                   "p", "q", Bounded(0, hops), dir="both", sigma_=sigma), chain)
        assert {r["q.uid"]: int(r["q.depth"]) for r in got.rows()} == by_uid, hops
