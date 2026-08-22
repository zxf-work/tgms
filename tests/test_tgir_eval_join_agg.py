"""M3.2 — `Join{left_outer, anti}` and `Aggregate`, with their preconditions.

Both absence-deriving joins and `Aggregate` carry a **runtime** precondition:
they refuse unless the input whose absence or completeness they rest on is
execution-complete. "An anti-join against a truncated probe reports false
absences — not merely uncertified rows, but wrong ones." So the tests here come
in pairs: the refusal fires where it must, and — the half that is easy to get
wrong — does **not** fire on a `paginated` input, which is delivery-incomplete
and execution-complete.
"""

from __future__ import annotations

import pytest

from tgms.core.errors import LimitError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.domain import Domain
from tgms.tgir.eval import Execution, evaluate_core
from tgms.tgir.expr import Col, PropRef
from tgms.tgir.metadata import Completeness, IncompletenessRefusal
from tgms.tgir.node import (
    Agg, Aggregate, Join, Limit, NodeScan, Order, Project, SortKey,
)
from tgms.tgir.prune import live_columns

from .conftest import fresh_adapter


def write(adapter, ops, tt):
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


def node_op(uid, label="N", props=None, vt_s=0, vt_e=100):
    return {"op": "assert_node", "uid": uid, "label": label, "props": props or {},
            "vt_s": vt_s, "vt_e": vt_e, "source": "i", "provenance_ref": None}


@pytest.fixture()
def store():
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [node_op(f"u{i}", "N" if i % 2 else "M", {"w": i})
              for i in range(1, 6)], 1)
    yield a
    a.close()


def keys(uids=()):
    scan = NodeScan("p", uids=tuple(uids)) if uids else NodeScan("p")
    return Project(scan, (("k", Col("p.uid")), ("w", Col("p.vt_e"))))


def probe(uids=()):
    scan = NodeScan("q", uids=tuple(uids)) if uids else NodeScan("q")
    return Project(scan, (("k2", Col("q.uid")), ("lab", Col("q.label"))))


# ---------------------------------------------------------------------------
# Join{left_outer}
# ---------------------------------------------------------------------------

def test_left_outer_fills_unmatched_rows_with_nulls(store):
    """`inner ∪ { l ⧺ null_R | ¬∃ r }` — and every right column becomes
    nullable (§4.2), which is the `nullable_copy` construction §9.1's
    version-less `into` also uses."""
    got = evaluate_core(Join(keys(), probe(("u1", "u2")), (("k", "k2"),),
                             join_type="left_outer"), store)
    by_key = {r["k"]: r for r in got.rows()}
    assert by_key["u1"]["k2"] == "u1" and by_key["u1"]["lab"] == "N"
    assert by_key["u3"]["k2"] is None and by_key["u3"]["lab"] is None
    assert got.n == 5, "every left row survives"
    assert got.schema.tau_of("lab").nullable


def test_left_outer_preserves_left_row_order(store):
    """§2.8's canonical order is left row position, and the fill rows are not a
    separate tail — they sit where their left rows sat."""
    left = keys()
    order = [r["k"] for r in evaluate_core(left, store).rows()]
    got = evaluate_core(Join(left, probe(("u1",)), (("k", "k2"),),
                             join_type="left_outer"), store)
    assert [r["k"] for r in got.rows()] == order


def test_left_outer_multiplies_against_duplicate_probe_keys(store):
    """§8.4 CLOSED: `left_outer` accepts duplicate probe keys **with
    multiplication**. The ruling is load-bearing for bo31 and BI18 on any
    corrected store, where several believed versions per identity is normal."""
    write(store, [node_op("u1", "N", {"w": 11}, vt_s=100, vt_e=200)], 2)
    got = evaluate_core(Join(keys(("u1",)), probe(("u1",)), (("k", "k2"),),
                             join_type="left_outer"), store)
    assert got.n == 4, "two left versions × two probe versions"


# ---------------------------------------------------------------------------
# Join{anti}
# ---------------------------------------------------------------------------

def test_anti_emits_left_rows_only(store):
    got = evaluate_core(Join(keys(), probe(("u1", "u2")), (("k", "k2"),),
                             join_type="anti"), store)
    assert sorted(r["k"] for r in got.rows()) == ["u3", "u4", "u5"]
    assert got.schema.names == ("k", "w"), "the probe contributes no columns"


def test_anti_accepts_duplicate_probe_keys(store):
    """"duplicates cannot change an absence test, so the result is
    unaffected" — the adjudicated ruling, tested rather than assumed."""
    write(store, [node_op("u1", "N", {"w": 11}, vt_s=100, vt_e=200)], 2)
    got = evaluate_core(Join(keys(("u2",)), probe(("u1",)), (("k", "k2"),),
                             join_type="anti"), store)
    assert [r["k"] for r in got.rows()] == ["u2"]


# ---------------------------------------------------------------------------
# the execution-completeness precondition
# ---------------------------------------------------------------------------

def truncated_probe(store):
    """A probe that is *not* execution-complete. No backend reports an engine
    cutoff today, so the state is produced through the metadata layer — which
    is where the precondition reads it from."""
    node = probe()
    run = Execution(store, live_columns(node))
    run.run(node)
    from tgms.tgir.metadata import ResultMeta

    meta = run.meta[node.node_digest]
    run.meta[node.node_digest] = ResultMeta(
        meta.sigma, Completeness.TIMEOUT_TRUNCATED, meta.exactness,
        meta.provenance, domain=meta.domain)
    return node, run


@pytest.mark.parametrize("join_type", ["left_outer", "anti"])
def test_the_absence_preconditions_fire_on_a_truncated_probe(store, join_type):
    node, run = truncated_probe(store)
    joined = Join(keys(), node, (("k", "k2"),), join_type=join_type)
    with pytest.raises(LimitError) as excinfo:
        run.run(joined)
    refusal = excinfo.value.details["incompleteness_refusal"]
    assert refusal["node_digest"] == joined.node_digest
    assert refusal["offending_input"] == "right"
    assert refusal["offending_completeness"] == "timeout-truncated"
    assert refusal["offending_sigma"]["t_b"]
    assert excinfo.value.details["error_class"] == "E_INCOMPLETE"


@pytest.mark.parametrize("join_type", ["left_outer", "anti"])
def test_the_absence_preconditions_do_not_fire_on_a_paginated_probe(store, join_type):
    """The half that is easy to get wrong. A page cut narrows **delivery**;
    execution is complete, and `rows_total` still certifies a cardinality. So
    the precondition must pass — refusing here would make every paged probe
    unusable for an absence test that is perfectly sound."""
    paged = Limit(probe(), 2)
    got = evaluate_core(Join(keys(), paged, (("k", "k2"),), join_type=join_type),
                        store)
    assert got.n >= 1


def test_inner_join_needs_no_precondition(store):
    """"`inner` may proceed against an incomplete input; its rows remain genuine
    witnesses and only the output's completeness degrades." """
    node, run = truncated_probe(store)
    joined = Join(keys(), node, (("k", "k2"),))
    out = run.run(joined)
    assert out.n == 5
    assert run.meta[joined.node_digest].completeness is Completeness.UNKNOWN


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def test_group_by_arity_is_unrestricted_and_empty_means_one_row(store):
    """"Today's two-slot cap is an operator-boundary artifact, not a semantic
    gap"; `group_by = []` yields exactly one row."""
    three = Aggregate(NodeScan("p"),
                      (("a", Col("p.label")), ("b", Col("p.vt_s")),
                       ("c", Col("p.vt_e"))),
                      (Agg("count", "n"),))
    assert evaluate_core(three, store).schema.names == ("a", "b", "c", "n")
    one = evaluate_core(Aggregate(NodeScan("p"), (), (Agg("count", "n"),)), store)
    assert one.rows() == [{"n": 5}]


def test_no_aggregates_is_distinct_over_the_key(store):
    got = evaluate_core(Aggregate(NodeScan("p"), (("lab", Col("p.label")),), ()),
                        store)
    assert [r["lab"] for r in got.rows()] == ["M", "N"]


def test_an_empty_input_emits_no_row_at_all(store):
    """Non-empty groups only: there is no densified group axis in v1, which is
    why `mean` never yields NaN and never raises a division error."""
    empty = Aggregate(NodeScan("p", uids=("nope",)), (),
                      (Agg("mean", "m", Col("p.vt_s")),))
    assert evaluate_core(empty, store).n == 0


def test_mean_is_the_one_blessed_mean(store):
    """`ops_aggregate._mean`, not a second implementation: "a mean over
    epoch-microsecond sums hashes identically everywhere, which plain float
    accumulation does not survive"."""
    from tgms.temporal.ops_aggregate import _mean

    got = evaluate_core(
        Aggregate(NodeScan("p"), (), (Agg("mean", "m", PropRef("p.props", "w")),)),
        store)
    assert got.rows() == [{"m": _mean(1 + 2 + 3 + 4 + 5, 5)}]


def test_aggregate_canonical_order_is_by_group_key_nulls_first(store):
    write(store, [node_op("z", "Z")], 2)
    got = evaluate_core(
        Aggregate(NodeScan("p"), (("w", PropRef("p.props", "w")),),
                  (Agg("count", "n"),)), store)
    values = [r["w"] for r in got.rows()]
    assert values[0] is None, "nulls first"
    assert values[1:] == sorted(v for v in values if v is not None)


def test_aggregate_refuses_over_a_non_execution_complete_input(store):
    node, run = truncated_probe(store)
    agg = Aggregate(node, (), (Agg("count", "n"),))
    with pytest.raises(LimitError) as excinfo:
        run.run(agg)
    assert excinfo.value.details["incompleteness_refusal"]["offending_input"] == "input"


def test_aggregate_over_a_page_cut_is_still_a_static_error(store):
    """M2.0's static rejection stands: "`Aggregate` consumes relations, never
    pages", and a page cut belongs at the plan's output boundary."""
    from tgms.core.errors import InvalidArgError

    with pytest.raises(InvalidArgError, match="never pages"):
        Aggregate(Limit(NodeScan("p"), 2), (), (Agg("count", "n"),))


def test_aggregate_over_a_top_k_limit_is_allowed_and_falls_the_domain(store):
    """The domain-fall: the enum value rises only because the domain fell."""
    top = Limit(Order(NodeScan("p"), (SortKey(Col("p.uid")),)), 2)
    agg = Aggregate(top, (), (Agg("count", "n"),))
    run = Execution(store, live_columns(agg))
    assert run.run(agg).rows() == [{"n": 2}]
    meta = run.meta[agg.node_digest]
    assert meta.completeness is Completeness.COMPLETE
    assert isinstance(meta.domain, Domain) and meta.domain.is_narrowed
    assert meta.domain == run.meta[top.node_digest].domain, \
        "complete **over that narrowed domain**, and the domains must be equal"


def test_count_distinct_and_the_scalar_aggregates(store):
    got = evaluate_core(
        Aggregate(NodeScan("p"), (),
                  (Agg("count_distinct", "d", Col("p.label")),
                   Agg("min", "lo", PropRef("p.props", "w")),
                   Agg("max", "hi", PropRef("p.props", "w")),
                   Agg("sum", "s", PropRef("p.props", "w")))), store)
    assert got.rows() == [{"d": 2, "lo": 1, "hi": 5, "s": 15}]


def test_the_incompleteness_refusal_shape(store):
    """§5.2's CO-2 resolution: a **different** shape from `RefusalCertificate`,
    because every field of that one except the digest is inapplicable — no
    ceiling, no estimate, no calibration reference."""
    refusal = IncompletenessRefusal("d", "Aggregate", "why", "input", "unknown",
                                    {"t_b": 1})
    payload = refusal.to_json()
    assert set(payload) == {"node_digest", "op", "reason", "offending_input",
                            "offending_completeness", "offending_sigma"}
    assert "ceilings" not in payload and "estimates" not in payload
