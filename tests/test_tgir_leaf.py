"""M2.2 — the opaque-leaf path: every operator call is a single-node TGIR plan.

The phase's contract is that this changes **nothing** about an answer. So most
of what is worth testing is a negative: the leaf delegates to the exact kernel,
the payload and its digest are identical with the wrapping on and off, and the
new metadata never enters `result_digest`. What is positive is the metadata
itself — Σ per §5.2, completeness per §11.11's ruling, provenance at §5.4's
descriptor granularity — and the `∅`-kernel guard, which turns §2.0's
classification from a comment into a check.

Companion receipts, which cover what a suite cannot:
`scripts/check_tgir_leaf_totality.py` (every operator wrapped, no bypass) and
`scripts/check_digest_stability.py` (19 frozen digests × 2 backends).
"""

from __future__ import annotations

import pytest

import tgms
from tgms.core.errors import NotFoundError, StateError, TgmsError
from tgms.core.model import OPEN_END, Interval, canonical_json
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.temporal import algebra
from tgms.temporal.algebra import (
    REGISTRY, call_operator, ensure_all_registered, validate_args,
)
from tgms.tgir.evaluate import evaluate, evaluate_leaf, meta_json
from tgms.tgir.leaf import LEAF_VT_MODE, build_leaf, sigma_for
from tgms.tgir.metadata import Completeness, Exactness
from tgms.tgir.node import EMPTY_SCOPE_OPS, NodeScan, OpaqueLeaf
from tgms.tgir.plan import Plan
from tgms.tgir.rollout import PLAN_PATH_ENV, plan_path_enabled

from .conftest import fresh_adapter

W = {"t_a": 0, "t_b": 100}


def write(adapter, ops, tt):
    """One write batch, bracketed the way `Store._write` brackets one.

    The brackets are not optional on every backend: the native engine stages a
    batch until `commit`, and a windowed columnar scan does not see staged rows
    even though `all_edge_versions` and `store_digest` do. Writing without them
    produces a store that looks identical and answers differently.
    """
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


@pytest.fixture()
def adapter():
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [
        {"op": "assert_node", "uid": "u1", "label": "N", "props": {"name": "one"},
         "vt_s": 0, "vt_e": 100, "source": "i", "provenance_ref": None},
        {"op": "assert_node", "uid": "u2", "label": "N", "props": {"name": "two"},
         "vt_s": 0, "vt_e": 100, "source": "i", "provenance_ref": None},
    ], 1)
    write(a, [
        {"op": "ingest_events", "offset": 0, "node_label": "N", "events": [
            {"src": "u1", "dst": "u2", "rel_type": "R", "vt_s": t} for t in range(10, 40)
        ]},
    ], 2)
    yield a
    a.close()


# ---------------------------------------------------------------------------
# §5.2 — Σ is derived per call, which is why the leaf is a node and not an
# annotation
# ---------------------------------------------------------------------------

def test_sigma_of_a_windowed_operator_is_its_window():
    sigma = sigma_for("version_history", {"kind": "node", "window": W,
                                          "as_of_tt": OPEN_END})
    assert sigma.t_v == (Interval(0, 100),) and sigma.t_b == OPEN_END


def test_sigma_of_an_unwindowed_operator_is_the_whole_extent():
    for op in ("entity_history", "resolve_entities", "co_active"):
        assert sigma_for(op, {"as_of_tt": OPEN_END}).t_v == (Interval(0, OPEN_END),)


def test_sigma_of_the_two_instant_operators():
    """`snapshot_subgraph` and `diff_snapshots` are the first operators in the
    system whose Σ is an instant — and therefore the first to meet §5.5.5's
    carve arm, where a narrow `vt` is what there is to lose."""
    assert sigma_for("snapshot_subgraph", {"t_valid": 42}).t_v == (Interval(42, 43),)
    pair = sigma_for("diff_snapshots", {"t1": 10, "t2": 20})
    assert pair.t_v == (Interval(10, 11), Interval(20, 21))
    assert pair.is_instant
    # neighborhood_evolution spans the two instants rather than pairing them
    assert sigma_for("neighborhood_evolution",
                     {"uid": "u1", "t1": 10, "t2": 20}).t_v == (Interval(10, 21),)


def test_as_of_tt_becomes_t_b_and_the_default_is_open_end():
    assert sigma_for("entity_history", {"uid": "u1", "as_of_tt": 7}).t_b == 7
    assert sigma_for("entity_history", {"uid": "u1"}).t_b == OPEN_END
    # `compute` has no as_of_tt at all: its Σ is inherited (§6 #15)
    assert sigma_for("compute", {"fn": "count"}).t_b == OPEN_END


def test_vt_mode_is_a_per_operator_scan_parameter():
    """§3.2's three keying modes, mapped per §5.2's table."""
    assert LEAF_VT_MODE["version_history"] == "overlap"
    assert LEAF_VT_MODE["aggregate_events"] == "event"
    assert LEAF_VT_MODE["snapshot_subgraph"] == "instant"
    assert set(LEAF_VT_MODE) >= set(REGISTRY)


def test_the_leaf_carries_the_call_not_the_operator():
    """Two calls of one operator are two different nodes; the same call twice
    is one node. `node_digest` is over data only — never a kernel callable."""
    a = build_leaf("entity_history", {"uid": "u1", "as_of_tt": OPEN_END}, ("rows",))
    b = build_leaf("entity_history", {"uid": "u1", "as_of_tt": OPEN_END}, ("rows",))
    c = build_leaf("entity_history", {"uid": "u2", "as_of_tt": OPEN_END}, ("rows",))
    d = build_leaf("entity_history", {"uid": "u1", "as_of_tt": 5}, ("rows",))
    assert a.node_digest == b.node_digest
    assert a.node_digest != c.node_digest      # bound args participate
    assert a.node_digest != d.node_digest      # and so does Σ


# ---------------------------------------------------------------------------
# no semantic change: the leaf delegates to the exact kernel
# ---------------------------------------------------------------------------

CASES = [
    ("entity_history", {"uid": "u1"}),
    ("version_history", {"kind": "node", "window": W}),
    ("snapshot_subgraph", {"seeds": ["u1"], "t_valid": 20}),
    ("diff_snapshots", {"t1": 10, "t2": 20}),
    ("neighborhood_evolution", {"uid": "u1", "t1": 10, "t2": 30}),
    ("resolve_entities", {"query": "u1"}),
    ("aggregate_events", {"group_by": [], "aggregates": [{"agg": "count"}], "window": W}),
    ("graph_metric_timeseries",
     {"metric": "edge_event_count", "window": W, "stride": 10}),
    ("burst_detection",
     {"target": {"kind": "edge_event_rate"}, "window": W, "stride": 10}),
    ("count_temporal_motifs", {"motif": "M_2node_pingpong", "delta": 5, "window": W}),
    ("find_temporal_motif_instances",
     {"motif": "M_2node_pingpong", "delta": 5, "window": W}),
    ("temporal_reachability", {"src": "u1", "window": W}),
    ("temporal_paths", {"src": "u1", "dst": "u2", "window": W}),
    ("co_active", {"a_spec": {"src": "u1"}, "b_spec": {"src": "u2"},
                   "allen_relation": {"relation": "overlaps"}}),
    ("compute", {"fn": "count", "input": [{"x": 1}, {"x": 2}]}),
]


@pytest.mark.parametrize("op,args", CASES, ids=[c[0] for c in CASES])
def test_the_plan_path_and_the_direct_path_agree_byte_for_byte(op, args, adapter,
                                                               monkeypatch):
    """The M2.2 gate in miniature: the same kernel, the same arguments, the
    same payload — so the same `result_digest`, whichever path ran it."""
    wrapped = call_operator(adapter, op, dict(args))
    monkeypatch.setenv(PLAN_PATH_ENV, "off")
    assert not plan_path_enabled()
    direct = call_operator(adapter, op, dict(args))
    assert wrapped["result_digest"] == direct["result_digest"]
    strip = ("tt_q", "pinned", "clamped", "dependency", "tgir")
    assert canonical_json({k: v for k, v in wrapped.items() if k not in strip}) == \
        canonical_json({k: v for k, v in direct.items() if k not in strip})


def test_every_registry_operator_has_a_case():
    """If an operator is added, this file must grow a case for it — the same
    totality `scripts/check_tgir_leaf_totality.py` enforces on the call path."""
    assert {c[0] for c in CASES} == set(REGISTRY)


def test_the_escape_hatch_is_off_by_default_and_takes_a_deliberate_value(monkeypatch):
    monkeypatch.delenv(PLAN_PATH_ENV, raising=False)
    assert plan_path_enabled()
    for value in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv(PLAN_PATH_ENV, value)
        assert not plan_path_enabled(), value
    for value in ("on", "1", "", "yes", "anything-else"):
        monkeypatch.setenv(PLAN_PATH_ENV, value)
        assert plan_path_enabled(), value


# ---------------------------------------------------------------------------
# §2.0 obligation 6 — the ∅-kernel guard, live
# ---------------------------------------------------------------------------

class ExplodingAdapter:
    """Every attribute access is a failure. Standing in for a live adapter,
    it turns "the kernel must not read the store" into an observation."""

    def __getattr__(self, name):
        raise AssertionError(f"the kernel touched the adapter: {name}")


def test_compute_is_evaluated_without_a_live_adapter():
    """`compute` is the one `∅` leaf, so its kernel never sees the adapter —
    it gets a `NullAdapter`, and a misclassification would fail loudly at the
    first read instead of rotting into silent unsoundness."""
    ensure_all_registered()
    filled = validate_args("compute", {"fn": "count", "input": [{"x": 1}, {"x": 2}]})
    leaf = build_leaf("compute", filled, REGISTRY["compute"].output_fields)
    assert leaf.withhold_adapter and not leaf.reads_store
    # the adapter handed in is never touched, because it is never passed on
    result = evaluate_leaf(leaf, ExplodingAdapter())
    assert result.payload["value"] == 2


def test_a_store_reading_leaf_does_receive_the_adapter():
    """The guard is a classification, not a blanket: the other fourteen get
    the real adapter, and an `∅` misclassification would show up here."""
    ensure_all_registered()
    filled = validate_args("entity_history", {"uid": "u1"})
    leaf = build_leaf("entity_history", filled,
                      REGISTRY["entity_history"].output_fields)
    assert leaf.reads_store and not leaf.withhold_adapter
    with pytest.raises(AssertionError, match="touched the adapter"):
        evaluate_leaf(leaf, ExplodingAdapter())


def test_the_null_adapter_names_the_attribute_it_refused():
    """A misclassified kernel must fail *by name*, so the diagnosis is the
    error rather than a puzzle."""
    from tgms.tgir.guard import NullAdapter

    with pytest.raises(StateError, match="nodes_columnar"):
        NullAdapter().nodes_columnar()


def test_only_compute_is_empty_scope_classified():
    assert EMPTY_SCOPE_OPS == {"compute"}
    assert EMPTY_SCOPE_OPS <= set(REGISTRY)


def test_the_leaf_evaluator_routes_core_nodes_elsewhere():
    """`evaluate` is the **opaque leaf's** entry point: it returns a payload
    envelope, while a core node returns a `Relation`. M3.0 built the core
    evaluators, so this no longer says "not implemented" — it says where to go,
    and the not-yet-built node kinds name their own phase (M3 plan §4.1)."""
    with pytest.raises(NotImplementedError, match="evaluate_core"):
        evaluate(NodeScan("p"), None)
    from tgms.tgir.node import Agg, Aggregate

    # the seam shrinks phase by phase: M3.0 built the scans and selections,
    # M3.1 `Expand`, and what is left names the phase that owns it
    with pytest.raises(NotImplementedError, match="M3.2"):
        evaluate(Aggregate(NodeScan("p"), (), (Agg("count", "n"),)), None)


# ---------------------------------------------------------------------------
# §5 — the result metadata the leaf now carries
# ---------------------------------------------------------------------------

def test_completeness_is_unknown_unless_the_envelope_proves_better(adapter):
    """§11.11's ruling. Mapping `truncated = False` to `complete` would
    manufacture a certification no leaf supports: none of the fifteen has been
    audited for execution completeness."""
    env = call_operator(adapter, "version_history", {"kind": "edge", "window": W})
    meta = _meta(adapter, "version_history", env)
    assert not env["truncated"]
    assert meta["completeness"] == Completeness.UNKNOWN.value
    assert meta["exactness"] == Exactness.EXACT.value


def test_a_truncated_page_with_a_cursor_is_paginated(adapter):
    """The one upgrade the envelope proves: delivery incomplete, execution
    complete, domain as declared — which is what `paginated` says."""
    env = call_operator(adapter, "version_history",
                        {"kind": "edge", "window": W, "limit": 2})
    assert env["truncated"] and env["cursor"] is not None
    assert _meta(adapter, "version_history", env)["completeness"] == \
        Completeness.PAGINATED.value


def test_truncation_without_a_cursor_stays_unknown(adapter):
    """`diff_snapshots`, `neighborhood_evolution` and `snapshot_subgraph`'s
    node list truncate with **no cursor to recover from it**, so `paginated`
    would claim a recoverability they do not have.

    Two shapes, because the absence takes two forms: a `cursor` field that is
    `None` even though the result is truncated (`snapshot_subgraph`, whose node
    list is capped but not paged), and an operator with no `cursor` field at
    all (`diff_snapshots`).
    """
    env = call_operator(adapter, "snapshot_subgraph",
                        {"seeds": ["u1"], "t_valid": 20, "limit": 1})
    assert env["truncated"] and env["nodes_truncated"] and env["cursor"] is None
    assert _meta(adapter, "snapshot_subgraph", env)["completeness"] == \
        Completeness.UNKNOWN.value

    write(adapter, [{"op": "assert_edge", "src": "u1", "dst": uid,
                     "rel_type": "S", "props": {}, "vt_s": 20, "vt_e": 100,
                     "disc": uid, "source": "i", "provenance_ref": None}
                    for uid in ("u2", "u3", "u4")], 100)
    env = call_operator(adapter, "diff_snapshots", {"t1": 10, "t2": 30, "limit": 1})
    assert env["truncated"] and "cursor" not in env
    assert _meta(adapter, "diff_snapshots", env)["completeness"] == \
        Completeness.UNKNOWN.value


def test_provenance_is_descriptor_level_and_content_addressed(adapter):
    env = call_operator(adapter, "entity_history", {"uid": "u1"})
    prov = _meta(adapter, "entity_history", env)["provenance"]
    assert prov["op"] == "entity_history"
    assert prov["node_digest"] and prov["input_digests"] == []
    assert prov["semantic_identity"].startswith("tgms/")
    # a scan descriptor, not a vid set: a whole-store scan's vid set is
    # unbounded, and a scope must cover regions holding no rows at all
    (descriptor,) = prov["source_versions"]
    assert descriptor["kind"] == "opaque" and descriptor["belief"] == "current"
    assert descriptor["endpoints"] == {"uid": "u1"}
    assert descriptor["sigma"]["t_b"] == OPEN_END


def test_an_empty_scope_leaf_reads_no_versions(adapter):
    """`compute`'s provenance has no scan descriptor at all — it read nothing,
    and saying "one opaque scan" would be a claim about a kernel that never
    touched the store."""
    env = call_operator(adapter, "compute", {"fn": "count", "input": [{"x": 1}]})
    prov = _meta(adapter, "compute", env)["provenance"]
    assert prov["source_versions"] == []


def test_the_metadata_carries_the_schema_and_the_scope(adapter):
    env = call_operator(adapter, "snapshot_subgraph", {"seeds": ["u1"], "t_valid": 20})
    meta = _meta(adapter, "snapshot_subgraph", env)
    assert meta["t_v"] == [[20, 21]] and meta["t_b"] == OPEN_END
    names = [c[0] for c in meta["schema"]]
    assert names == list(REGISTRY["snapshot_subgraph"].output_fields)


def _meta(adapter, op, env):
    from tgms.tgir.evaluate import meta_for

    return meta_for(op, env["args_echo"], env, REGISTRY[op].output_fields)


# ---------------------------------------------------------------------------
# digest exclusion, with the envelope switch on
# ---------------------------------------------------------------------------

def test_publishing_the_metadata_does_not_move_a_digest(adapter, monkeypatch):
    """The metadata is envelope-only, so `result_digest` — which covers the
    kernel's payload — cannot see it. Flipping the publication switch is the
    strongest form of that check."""
    monkeypatch.delenv(PLAN_PATH_ENV, raising=False)   # the metadata is the
    # plan path's; this test is about publication, not about the hatch
    # Pin both states explicitly: the module default flipped to True when
    # publication went live, and this test must not depend on the default.
    monkeypatch.setattr(algebra, "EMIT_TGIR_META", False)
    before = call_operator(adapter, "entity_history", {"uid": "u1"})
    monkeypatch.setattr(algebra, "EMIT_TGIR_META", True)
    after = call_operator(adapter, "entity_history", {"uid": "u1"})
    assert "tgir" not in before and "tgir" in after
    assert before["result_digest"] == after["result_digest"]
    assert after["tgir"]["plan_digest"] == after["tgir"]["node_digest"]
    assert after["tgir"]["completeness"] == Completeness.UNKNOWN.value


# ---------------------------------------------------------------------------
# the single-leaf plan
# ---------------------------------------------------------------------------

def test_a_single_leaf_plan_is_its_leaf():
    leaf = build_leaf("entity_history", {"uid": "u1", "as_of_tt": OPEN_END}, ("rows",))
    plan = Plan.of(leaf, "p1")
    assert plan.nodes() == (leaf,)
    assert plan.plan_digest != leaf.node_digest  # a plan wraps, it does not alias
    record = plan.to_json()
    assert record["plan_id"] == "p1"
    assert record["nodes"] == [{"op": "entity_history",
                                "node_digest": leaf.node_digest,
                                "sigma": leaf.sigma.to_json(), "inputs": []}]


def test_plan_nodes_are_deduplicated_by_digest():
    """A DAG that reaches one subtree twice reports it once."""
    from tgms.tgir.expr import Cmp, Col, Lit
    from tgms.tgir.node import Filter, Join, Project

    scan = NodeScan("p", uids=("u1",))
    left = Project(Filter(scan, Cmp(">", Col("p.vt_s"), Lit(0))), (("k", Col("p.uid")),))
    right = Project(scan, (("k2", Col("p.uid")),))
    plan = Plan(Join(left, right, (("k", "k2"),)))
    ops = [n.op for n in plan.nodes()]
    assert ops.count("NodeScan") == 1        # shared, counted once
    assert ops[-1] == "Join" and ops[0] == "NodeScan"   # inputs before consumers


# ---------------------------------------------------------------------------
# the executor's step record is a TGIR plan record
# ---------------------------------------------------------------------------

def test_trace_steps_carry_the_plan_record(adapter):
    from tgms.agent.executor import Executor
    from tgms.agent.ir import Plan as AgentPlan
    from tgms.tools.server import ToolRouter

    plan = AgentPlan.from_json({
        "plan_id": "m22",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "u1"}},
            {"id": "s2", "op": "entity_history", "args": {"uid": "nope"}},
        ],
        "answer_spec": {"kind": "count", "from": "s1.rows_total"}})
    trace = Executor(ToolRouter(adapter)).run(plan)
    steps = {s["step_id"]: s for s in trace.to_json()["steps"]}
    assert steps["s1"]["status"] == "ok"
    tgir = steps["s1"]["tgir"]
    assert tgir["node_digest"] == tgir["plan_digest"]
    assert tgir["provenance"]["op"] == "entity_history"
    assert tgir["completeness"] == Completeness.UNKNOWN.value
    # a failed step has no result to describe, but still carries its scope
    assert steps["s2"]["status"] == "failed" and "tgir" not in steps["s2"]
    assert isinstance(steps["s2"]["dependency"], dict)


def test_the_answer_projection_strips_the_metadata(adapter):
    """C8: the agent's answer must not grow envelope keys, or every agent
    answer changes shape."""
    from tgms.agent.executor import Executor
    from tgms.agent.ir import Plan as AgentPlan
    from tgms.tools.server import ToolRouter

    plan = AgentPlan.from_json({
        "plan_id": "m22b",
        "steps": [{"id": "s1", "op": "entity_history", "args": {"uid": "u1"}}],
        "answer_spec": {"kind": "entity_set", "from": "s1"}})
    trace = Executor(ToolRouter(adapter)).run(plan)
    assert set(trace.answer) & {"op", "args_echo", "dataset_extent", "tt_q",
                                "pinned", "clamped", "dependency", "tgir"} == set()
    assert "rows" in trace.answer


# ---------------------------------------------------------------------------
# §11.9 — the resolve_entities preservation obligation
# ---------------------------------------------------------------------------

RESOLVE_VERSIONS = (
    # an entity whose newest believed version does **not** itself match the
    # query: found by its old name, and §11.9's exact distinguishing shape
    ("A", {"name": "alpha"}, 0, 50),
    ("B", {"name": "zeta"}, 50, 100),
)


def _resolve_store(adapter):
    for i, (label, props, vt_s, vt_e) in enumerate(RESOLVE_VERSIONS, start=1):
        write(adapter, [{"op": "assert_node", "uid": "u-ren", "label": label,
                         "props": props, "vt_s": vt_s, "vt_e": vt_e,
                         "source": "i", "provenance_ref": None}], i)
    return adapter


def _both_adapters():
    """The two implementations by name, not by whatever the environment
    defaults to: `resolve_entities` has an engine kernel *and* a portable
    row-loop fallback, and the point is to pin each."""
    out = {"portable": DuckDBAdapter(":memory:")}
    try:
        import tempfile

        from tgms.storage.native import NativeAdapter
        tmp = tempfile.TemporaryDirectory(prefix="tgms-resolve-")
        native = NativeAdapter(tmp.name)
        native._test_tmpdir = tmp
        out["kernel"] = native
    except Exception:  # pragma: no cover - engine not built
        pass
    return out


@pytest.mark.parametrize("path", ["portable", "kernel"])
def test_resolve_entities_canonical_version_is_pinned_per_path(path):
    """§11.9's fixture, and its outcome.

    `TGIR_SPEC.md` §6 #14 makes *preserving* a known `resolve_entities`
    divergence an M2 obligation, and §11.9 records that no suite could detect a
    repair. Both rest on `docs/eval_semantics.md` §7, which is **stale**: D-031
    (2026-07-30) already closed the divergence in favour of the oracle's rule —
    canonical `label`/`name` come from the latest believed version by
    `(vt_s, vid)`, *whether or not that version matched* — and
    `crates/tgms-engine-core/src/read.rs::resolve_entities` says so in its own
    contract. There is therefore nothing left to preserve, and preserving it
    would mean reintroducing a fixed bug.

    So this pins the **current behaviour of each path separately** on the exact
    data §11.9 asks for ("a uid whose newest version does not itself match").
    Written per path rather than as a cross-check, so a future drift in either
    implementation fails here by name — including a drift that made them
    disagree again, which the previous shape of this obligation could not see.
    """
    ensure_all_registered()
    adapters = _both_adapters()
    if path not in adapters:
        pytest.skip("native engine not built")
    adapter = _resolve_store(adapters[path])
    try:
        rows = call_operator(adapter, "resolve_entities", {"query": "alpha"})["rows"]
        assert rows == [{"uid": "u-ren", "label": "B", "name": "zeta", "match": 2}], \
            f"{path} path changed its canonical-version rule"
        # the label filter reads the *canonical* version's label, not the
        # matching one — the same rule, observed through a second argument
        assert call_operator(adapter, "resolve_entities",
                             {"query": "alpha", "label": "A"})["rows"] == []
        assert call_operator(adapter, "resolve_entities",
                             {"query": "alpha", "label": "B"})["rows"] == rows
    finally:
        adapter.close()


def test_the_vt_s_tiebreak_half_of_the_divergence_is_unreachable():
    """The other half of `eval_semantics.md` §7's claim — that the two paths
    break `vt_s` ties in opposite directions — cannot be exercised at all, and
    the reason is structural rather than a gap in the data.

    Two *believed* versions of one uid have disjoint valid intervals (the
    store's own invariant), so two of them cannot share a `vt_s`. D-031 records
    the same conclusion and keeps the `vid` tiebreak anyway, so every
    implementation is order-independent by construction rather than by luck.
    This test is the recorded impossibility argument, and it fails the moment
    the invariant it rests on stops holding.
    """
    a = DuckDBAdapter(":memory:")
    try:
        write(a, [{"op": "assert_node", "uid": "u1", "label": "A", "props": {},
                   "vt_s": 0, "vt_e": 100, "source": "i",
                   "provenance_ref": None}], 1)
        # a second version at the same vt_s supersedes rather than coexisting
        write(a, [{"op": "assert_node", "uid": "u1", "label": "B", "props": {},
                   "vt_s": 0, "vt_e": 100, "source": "i",
                   "provenance_ref": None}], 2)
        believed = [v for v in a.all_node_versions() if v.believed_at(OPEN_END)]
        assert len(believed) == 1, "two believed versions of one uid at one vt_s"
        starts = [v.vt_s for v in believed]
        assert len(set(starts)) == len(starts)
    finally:
        a.close()


def test_resolve_entities_stays_opaque():
    """§6 #14: the operator is OPAQUE, so no compiled path exists to diverge
    from. M2's obligation is discharged by not touching the kernel (rule 1.3),
    which the leaf wrapping preserves by construction — it delegates."""
    leaf = build_leaf("resolve_entities", {"query": "x", "as_of_tt": OPEN_END},
                      REGISTRY["resolve_entities"].output_fields)
    assert isinstance(leaf, OpaqueLeaf) and leaf.reads_store


def test_unknown_operator_in_a_leaf_is_an_internal_error():
    from tgms.core.errors import InternalError

    leaf = OpaqueLeaf.build("not_an_operator", {}, ("rows",))
    with pytest.raises(InternalError):
        evaluate_leaf(leaf, None)


def test_meta_json_drops_the_dependency_it_does_not_own():
    """`dependency` and `tt_q` are carried flat beside the payload (D13.16's
    placement); one copy is enough."""
    leaf = build_leaf("compute", validate_args("compute", {"fn": "count", "input": []}),
                      ("value",))
    meta = meta_json(leaf, {"value": 0, "truncated": False})
    assert "dependency" not in meta and "completeness" in meta


def test_a_leaf_for_a_store_backed_call_matches_the_registry_schema(adapter):
    """C4: the envelope's field names are the contract, and the leaf's schema
    is derived from exactly them."""
    for op, args in CASES:
        try:
            env = call_operator(adapter, op, dict(args))
        except (TgmsError, NotFoundError):
            continue
        leaf = build_leaf(op, env["args_echo"], REGISTRY[op].output_fields)
        assert [c.name for c in leaf.out_schema] == list(REGISTRY[op].output_fields)


def test_tgms_open_still_answers_through_the_leaf(tmp_path):
    """End to end, through the public surface: `tgms.open` → router → leaf."""
    from tgms.tools.server import ToolRouter

    store = tgms.open(tmp_path / "s")
    try:
        store.assert_node("u1", "N", {"name": "one"}, 0, 100)
        res = ToolRouter(store.adapter, tt_source=store).call(
            "entity_history", {"uid": "u1"})
        assert res["rows_total"] == 1 and "result_digest" in res
    finally:
        store.close()
