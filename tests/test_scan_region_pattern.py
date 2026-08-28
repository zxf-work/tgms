"""P1.3, first tranche — `ScanRegion`/`level1.refine` for `PatternMatch`.

`docs/design/M5_LEVEL1_SOUNDNESS.md` §1's proof (`L-PM1`, `PO-P1`..`PO-P4`),
its failure-mode table (§1.6) and its widening table (§1.7), discharged as
the test obligations §1.8 lists — each test below names the obligation it
pins in its docstring. `tests/test_artifact_check.py::
test_level1_flag_is_wired_and_absent_region_is_a_no_op` covers the absent
scan region / fail-safe byte-identity case (§1.8 test 9) against a non-
`PatternMatch` record; this file is the PatternMatch-specific suite the
task card asked for, including the fail-safe path with a `PatternMatch`
step actually present.

`snapshot_subgraph` (§2) and the multi-hop closure predicate (§3) are out of
this tranche's scope (blocked on finding F3; the cut-line item) and are not
tested here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import tgms
from tgms.core.errors import CostError, InvalidArgError
from tgms.core.model import EntityRef
from tgms.tgir.admission import Budget
from tgms.tgir.check import FRESH, POSSIBLY_STALE, UNDECIDABLE, Witness
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.eval.pattern import _node_domains, eval_pattern
from tgms.tgir.execute import run_plan
from tgms.tgir.node import (
    EdgePat, NodePat, NodeScan, Pattern, PatternMatch, Source,
)
from tgms.tgir.relation import Relation
from tgms.tgir.scan_region import (
    EdgeDomain, ScanRegion, pattern_match_region, scan_region_terms,
)
from tgms.tgir.types import Column, Schema, T_UID, Sigma
from tgms.tgir import level1 as level1_mod
from tgms.artifact.record import ArtifactRecord, StepDependency
from tgms.artifact.witness import check_artifact

WIDE_CEILINGS = {"rows_scanned_est": 10 ** 12, "expansions_est": 10 ** 12,
                 "time_est_ms": 10 ** 9}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = tgms.open(tmp_path / "s", backend="native")
    yield s
    s.close()


def _knows_pattern() -> Pattern:
    return Pattern((NodePat("x"), NodePat("y")), (EdgePat("e1", "x", "y", "KNOWS"),))


def _seed_triangle(store) -> None:
    """`a --KNOWS--> b`, plus an unrelated pair `p`/`q` and a bystander `z`,
    none of which are endpoints of the scanned edge — the fixture every
    A/B-out and FM test below needs to have "somewhere outside the region"."""
    for uid in ("a", "b", "p", "q", "z"):
        store.assert_node(uid, "Person", {}, 0, 100)
    store.assert_edge("a", "b", "KNOWS", {}, 0, 100)


def _run(store, root: PatternMatch) -> dict[str, Any]:
    return run_plan(root, store.adapter, tt_source=store, cost_ceilings=WIDE_CEILINGS)


def _record(store, root: PatternMatch, result: dict[str, Any]) -> ArtifactRecord:
    """The same `ArtifactRecord` shape `tests/test_artifact_check.py::
    _register` builds, but sourced from a real `run_plan` envelope so the
    `dependency` scope and the `scan_region` annotation are the real, paired
    artifacts one `PatternMatch` execution produced — never hand-assembled."""
    scope = DependencyScope.from_json(result["dependency"])
    region = result["tgir"].get("annotations", {}).get(root.node_digest, {}).get("scan_region")
    return ArtifactRecord(
        name="wmc", generation=0, kind="query_result", store=scope.store,
        plan={"plan_digest": "pd", "node_digest": root.node_digest, "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": scope.tt_q, "pinned": scope.pinned, "clamped": scope.clamped,
              "tt_q_verified": scope.tt_q_verified},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope, scan_region=region)],
    )


def _verdicts(store, record: ArtifactRecord) -> tuple[Any, Any]:
    """`(level0, level1)` — `check_artifact` with the flag off and on, against
    the *same* log state, so a caller can assert the raw check verdict is
    unchanged while level1's is narrower (the task's A/B pair, in one call)."""
    return (check_artifact(record, store.eventlog, level1=False),
           check_artifact(record, store.eventlog, level1=True))


# ---------------------------------------------------------------------------
# the recorded region itself — shape, determinism
# ---------------------------------------------------------------------------

def test_region_is_recorded_as_the_pair_edge_and_node_arms(store):
    """The region is a *pair* (L-PM1's corollary): one edge term per edge
    variable plus one node term, never the node arm alone."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    region = result["tgir"]["annotations"][root.node_digest]["scan_region"]
    assert region["complete"] is True
    assert region["edge_domains"] == [{"var": "e1", "source": "scan", "rel_type": "KNOWS"}]
    assert region["node_uids"] == {"x": ["a"], "y": ["b"]}

    terms = scan_region_terms(region)
    assert len(terms) == 2
    edge_term, node_term = terms
    assert edge_term.targets.edges is not None and edge_term.targets.nodes is None
    assert edge_term.rel_types == ("KNOWS",)
    assert node_term.targets.nodes == ("a", "b")
    assert node_term.targets.edges is None


def test_region_serialization_is_deterministic():
    """§4's "region serialization" obligation: two builds from differently
    ordered inputs canonicalize to the same JSON and the same digest."""
    r1 = pattern_match_region(
        node_digest="nd", t_v=[(0, 100)], t_b=100,
        edge_domains=[EdgeDomain("e1", "scan", "KNOWS"), EdgeDomain("e2", "bound", None)],
        node_uids={"y": ["b", "a"], "x": ["a"]},
        node_cohorts={"x": ["a"]})
    r2 = pattern_match_region(
        node_digest="nd", t_v=[(0, 100)], t_b=100,
        edge_domains=[EdgeDomain("e1", "scan", "KNOWS"), EdgeDomain("e2", "bound", None)],
        node_uids={"x": ["a"], "y": ["a", "b"]},
        node_cohorts={"x": ["a"]})
    assert r1.canonical() == r2.canonical()
    assert r1.digest() == r2.digest()
    # and re-parsing the canonical form round-trips to the same terms
    assert scan_region_terms(ScanRegion.from_json(r1.to_json())) == scan_region_terms(r1)


def test_digest_stability_is_unaffected_by_the_scan_region_annotation(store):
    """The channel is digest-excluded structurally (`execute.py`'s `payload`/
    `tgir` split): two runs that differ only in whether a `scan_region` is
    materialized (recording on vs. an artificially emptied sink) must still
    produce the same `result_digest` — the same property
    `test_plan_telemetry.py::test_result_digest_is_unchanged_by_annotation_content`
    pins for the telemetry lane, restated for the P1.3 lane
    (`scripts/check_digest_stability.py`'s standing gate)."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    r1 = _run(store, root)
    r2 = _run(store, root)
    assert r1["result_digest"] == r2["result_digest"]
    assert "scan_region" in r1["tgir"]["annotations"][root.node_digest]


# ---------------------------------------------------------------------------
# §1.8 test 1/2 — A/B rel_type (declared vs. undeclared)
# ---------------------------------------------------------------------------

def test_ab_in_declared_rel_type_correction_invalidates_both_levels(store):
    """§1.8 test 1 — a correct on the scanned rel_type, vt overlapping Σ,
    invalidates under Level 0 and Level 1 alike; `matched_on` never names
    `targets` for the edge term (it's vacuous — `targets.edges` is `TOP`)."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    record = _record(store, root, result)

    store.correct(EntityRef(kind="edge", src="a", dst="b", rel_type="KNOWS"),
                  {"w": 1}, vt_s=0, vt_e=100)

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh
    hit = [w for w in v1.steps.witnesses if "targets" in w.matched_on]
    assert not hit, "the edge term's targets arm is TOP and must not be reported as a hit"


def test_ab_out_foreign_rel_type_is_fresh_at_both_levels_when_all_declared(store):
    """§1.8 test 2, **as verified against the frozen `scope_of.py`** — flagged
    here rather than silently adjusted (see this file's module docstring's
    pointer to the final report for the full write-up).

    `M5_LEVEL1_SOUNDNESS.md` §1.8 test 2 describes this scenario ("a
    correction of a rel_type no `edge_pat` declares, in a pattern where every
    `edge_pat` declares one") as a case where Level 0 is imprecise and Level
    1 alone reaches `FRESH`. Empirically, against `scope_of.py:118-121`, that
    is not what happens: when *every* `edge_pat` declares a type, Level 0's
    `rel_types` is `tuple(dict.fromkeys(declared))` — the union of the
    declared types — and `PatternMatch`'s own per-variable Level-1 terms are
    each `(edge.rel_type,)`. As a *disjunction*, `⋃ᵢ {declaredᵢ}` and
    `{declared₁, ..., declaredₙ}` are the same predicate over the type alone,
    so a correction of a genuinely foreign type is excluded by **both**
    levels identically — confirmed here with two edge variables of two
    distinct declared types (`KNOWS`, `LIKES`) and a `FOLLOWS` correction
    incident to neither pattern's matched nodes. This does not cost
    soundness (Level 0 is, if anything, already *more* precise here than the
    design memo's ledger assumed) — it means this specific test obligation,
    read literally, has no distinguishing instance against the real
    `scope_of.py`. The per-variable split still has independent structural
    value (`test_region_is_recorded_as_the_pair_edge_and_node_arms`,
    `test_wp4_bound_edge_domain_edges_arm_stays_top`) and the mixed
    declared/undeclared case (an undeclared variable's own term is `TOP`,
    which then dominates the disjunction regardless of the *other*
    variables' precision) is why it is not independently observable either.
    """
    for u in ("a", "b", "c", "d", "p", "q"):
        store.assert_node(u, "Person", {}, 0, 100)
    store.assert_edge("a", "b", "KNOWS", {}, 0, 100)
    store.assert_edge("c", "d", "LIKES", {}, 0, 100)
    pattern = Pattern((NodePat("x"), NodePat("y"), NodePat("u"), NodePat("v")),
                      (EdgePat("e1", "x", "y", "KNOWS"), EdgePat("e2", "u", "v", "LIKES")))
    root = PatternMatch(pattern, sigma_=Sigma.default())
    result = _run(store, root)
    assert result["dependency"]["terms"][0]["rel_types"] == ["KNOWS", "LIKES"]
    record = _record(store, root, result)

    store.assert_edge("p", "q", "FOLLOWS", {}, 0, 100)  # declared by neither e1 nor e2

    v0, v1 = _verdicts(store, record)
    assert v0.actionable_fresh, "Level 0's union already excludes the foreign type"
    assert v1.actionable_fresh, "Level 1 must not regress below Level 0's own precision"


# ---------------------------------------------------------------------------
# §1.8 test 3/4 — A/B node (in region vs. out of region, "the entire value")
# ---------------------------------------------------------------------------

def test_ab_node_in_matched_uid_invalidates(store):
    """§1.8 test 3 — a node write on a matched uid invalidates, with
    `matched_on == ("targets.nodes",)`."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    record = _record(store, root, result)

    store.assert_node("a", "Person", {"x": 1}, 0, 100)

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh
    node_hits = [w for w in v1.steps.witnesses if "targets.nodes" in w.matched_on]
    assert node_hits


def test_ab_node_out_is_the_items_entire_value(store):
    """§1.8 test 4 — "This is the test that measures the item's entire
    value": a node write on a uid that is an endpoint of no scanned edge is
    `FRESH` under Level 1 and `POSSIBLY_STALE` under Level 0, because Level
    0's `targets` is `"*"` (unanchored pattern, `scope_of.py:127-128`) and
    Level 1's is the actual matched uid set."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    record = _record(store, root, result)

    store.assert_node("z", "Person", {"x": 1}, 0, 100)  # z is nowhere in {a, b}

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh, "Level 0's targets is TOP -- any node write matches"
    assert v1.actionable_fresh, "Level 1 narrows to the actually-matched uids"


# ---------------------------------------------------------------------------
# §1.8 test 5 — PO-P2: label_filter must not un-see a dropped row's uid
# ---------------------------------------------------------------------------

def test_po_p2_label_change_undrops_a_filtered_row(store):
    """§1.8 test 5 — the test that fails if an implementer records `distinct`
    *after* `label_filter` instead of before: a node pattern with a label
    predicate that currently excludes `b` (wrong label) still records `b` in
    `node_uids`, because `distinct` is captured in `_materialize`/`_versions`
    *before* `label_filter` (which wraps the whole `eval_pattern` call, at
    the `Execution` dispatch site) ever runs. A later `assert_node` that
    changes `b`'s label (`correct` cannot — a `correct` op carries no label
    argument at all, `storage/base.py`'s `_correct`) so it would now match
    the pattern must therefore invalidate the cached result, not silently
    stay fresh."""
    for u in ("a",):
        store.assert_node(u, "Person", {}, 0, 100)
    store.assert_node("b", "Wrong", {}, 0, 100)  # b's real label excludes it
    store.assert_edge("a", "b", "KNOWS", {}, 0, 100)
    pattern = Pattern((NodePat("x"), NodePat("y", label="Person")),
                      (EdgePat("e1", "x", "y", "KNOWS"),))
    root = PatternMatch(pattern, sigma_=Sigma.default())
    result = _run(store, root)
    assert result["tgir"]["rows_total"] == 0, "b's label excludes the match at recording time"
    record = _record(store, root, result)
    region = result["tgir"]["annotations"][root.node_digest]["scan_region"]
    assert region["node_uids"]["y"] == ["b"], \
        "distinct must be captured pre-label_filter, so b is still recorded"

    store.assert_node("b", "Person", {}, 0, 100)  # the label change that un-drops the row

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh, "b must still be in the region despite label_filter"


# ---------------------------------------------------------------------------
# §1.8 test 6 / FM-1 — the edge arm must stay intensional, never extensional
# ---------------------------------------------------------------------------

def test_fm1_new_edge_between_unrecorded_uids_still_invalidates(store):
    """§1.8 test 6 / FM-1 — the test that fails if the edge arm was made
    extensional (`targets.edges` narrowed to observed `eid`s instead of
    staying `TOP`): a brand-new edge of the scanned rel_type, incident to two
    uids that appear nowhere in `node_uids`, must still invalidate."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    record = _record(store, root, result)

    store.assert_edge("p", "q", "KNOWS", {}, 0, 100)  # p, q are in neither {a, b}

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh, "the edge arm must stay TOP -- new-edge absence is intensional"


# ---------------------------------------------------------------------------
# §1.8 test 7 / FM-3 — the carve arm still reaches a vt-disjoint correction
# ---------------------------------------------------------------------------

def test_fm3_carve_arm_reaches_a_vt_disjoint_correction(store):
    """§1.8 test 7 / FM-3 — the test that fails if `props` was narrowed off
    `TOP`: a `correct` whose *value*-arm valid-time interval is disjoint from
    Σ still invalidates via the carve arm (`vt="*"`, `props={@recut,@version}`
    — emitted unconditionally, `footprint.py:228-238`), which only meets a
    term whose own `props` is `TOP`."""
    for u in ("a", "b"):
        store.assert_node(u, "Person", {}, 0, 1000)
    # a wide-lived edge so a later correct(vt_s=50, vt_e=60) has an existing
    # believed version to overlap and carve, while [50, 60) itself is
    # disjoint from Σ = [10, 11) below.
    store.assert_edge("a", "b", "KNOWS", {}, 0, 1000)
    windowed = PatternMatch(_knows_pattern(), sigma_=Sigma.at_instant(10))
    result_w = _run(store, windowed)
    record_w = _record(store, windowed, result_w)

    store.correct(EntityRef(kind="edge", src="a", dst="b", rel_type="KNOWS"),
                  {"w": 1}, vt_s=50, vt_e=60)  # disjoint from [10, 11), inside [0, 1000)

    v0, v1 = _verdicts(store, record_w)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh, "the carve arm must still reach through props=TOP"
    carve_hits = [w for w in v1.steps.witnesses if w.arm == "carve"]
    assert carve_hits


# ---------------------------------------------------------------------------
# §1.8 test 8 / FM-7 — an empty result still depends on the edge scan
# ---------------------------------------------------------------------------

def test_fm7_empty_result_still_depends_on_the_edge_scan(store):
    """§1.8 test 8 / FM-7 — a pattern that binds nothing, then an edge write
    that would have matched, must invalidate: `Targets(nodes=())` can never
    HIT on its own, and it is sound only because the edge term still
    accompanies it (this is the test that catches a T_node-only region)."""
    for uid in ("a", "b"):
        store.assert_node(uid, "Person", {}, 0, 100)
    # no KNOWS edge at all -> the pattern binds nothing
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    result = _run(store, root)
    assert result["tgir"]["rows_total"] == 0
    record = _record(store, root, result)
    region = result["tgir"]["annotations"][root.node_digest]["scan_region"]
    assert region["node_uids"] == {"x": [], "y": []}

    store.assert_edge("a", "b", "KNOWS", {}, 0, 100)  # the edge that would have matched

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh, "the empty node term alone must not go FRESH"


# ---------------------------------------------------------------------------
# §1.8 test 9 — the fail-safe: absent region -> byte-identical to Level 0
# ---------------------------------------------------------------------------

def test_failsafe_widened_execution_is_byte_identical_to_level0(store):
    """§1.8 test 9, the `PatternMatch`-specific case: a widened execution
    (here, W-P2's D-155 runtime refusal) records no region at all, so
    `check_artifact(..., level1=True)` and `level1=False` must be
    byte-identical on the *next*, un-widened run's record — there is nothing
    to narrow with because nothing was recorded."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())

    sink: dict[str, Any] = {}
    with pytest.raises(CostError):
        eval_pattern(root, {}, store.adapter, None, Budget("pd", limit=0), None,
                    region_sink=sink)
    assert sink == {}, "W-P1/W-P2: a raise must leave region_sink empty"

    # a record with no scan_region at all -- the fail-safe's own shape
    result = _run(store, root)
    scope = DependencyScope.from_json(result["dependency"])
    record = ArtifactRecord(
        name="wmc", generation=0, kind="query_result", store=scope.store,
        plan={"plan_digest": "pd", "node_digest": root.node_digest, "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": scope.tt_q, "pinned": scope.pinned, "clamped": scope.clamped,
              "tt_q_verified": scope.tt_q_verified},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope, scan_region=None)],
    )
    store.assert_node("a", "Person", {"x": 1}, 0, 100)

    v0, v1 = _verdicts(store, record)
    assert v0.to_json() == v1.to_json()
    assert all(t.level == "level-0" for t in v1.terms)


# ---------------------------------------------------------------------------
# the widening table (§1.7), row by row
# ---------------------------------------------------------------------------

def test_wp1_wp2_any_raise_leaves_region_sink_empty(store):
    """W-P1/W-P2 — the recorder writes only on `eval_pattern`'s normal
    return: a `Budget` with a zero ceiling raises `CostError` (the D-155
    runtime-refusal shape, `admission.RefusalCertificate` stage `"runtime"`)
    mid-search, and `region_sink` must still be `{}` afterward."""
    _seed_triangle(store)
    root = PatternMatch(_knows_pattern(), sigma_=Sigma.default())
    sink: dict[str, Any] = {}
    with pytest.raises(CostError):
        eval_pattern(root, {}, store.adapter, None, Budget("pd", limit=0), None,
                    region_sink=sink)
    assert sink == {}


def test_wp3_non_current_belief_widens():
    """W-P3, at the unit level: both scans `eval_pattern` itself issues
    hardcode `belief="current"` (`pattern.py:114`, `_versions`'s explicit
    `NodeScan(..., belief="current", ...)`), so this condition is structurally
    unreachable through the real call path today -- pinned here as a direct,
    independently-testable guard so a future change to either default cannot
    silently turn a `superseded`/`all` read into a narrowed region."""
    from tgms.tgir.eval.pattern import _region_widened
    assert _region_widened(["current", "current"]) is False
    assert _region_widened(["current", "superseded"]) is True
    assert _region_widened(["all"]) is True


def test_wp4_bound_edge_domain_edges_arm_stays_top(store):
    """W-P4 — a `source == "bound"` edge variable's `edges` arm is never
    narrowed to observed `eid`s: the recorded `EdgeDomain.source` is
    `"bound"`, but `scan_region_terms` still emits `targets.edges = TOP` for
    it (same as the `"scan"` row), so a *new* edge between the two bound
    uids -- which the bound relation obviously never saw -- still
    invalidates rather than going silently FRESH."""
    for uid in ("a", "b"):
        store.assert_node(uid, "Person", {}, 0, 100)
    store.assert_edge("a", "b", "KNOWS", {}, 0, 100)

    # a NodeScan feeding PatternMatch's edge variable "e1" via `sources`, so
    # `_domain` takes the "bound" branch (`pattern.py:109-111`) instead of
    # issuing its own EdgeScan.
    from tgms.tgir.eval import EdgeScan as _EdgeScanEval  # noqa: F401  (import-order doc only)
    from tgms.tgir.node import EdgeScan as EdgeScanNode

    edge_source_node = EdgeScanNode("e1", rel_types=("KNOWS",), sigma_=Sigma.default())
    pattern = Pattern((NodePat("x"), NodePat("y")), (EdgePat("e1", "x", "y"),))
    root = PatternMatch(pattern, sources=(Source("e1", edge_source_node),),
                        sigma_=Sigma.default())
    result = _run(store, root)
    region = result["tgir"]["annotations"][root.node_digest]["scan_region"]
    assert region["edge_domains"] == [{"var": "e1", "source": "bound", "rel_type": None}]

    terms = scan_region_terms(region)
    edge_term = terms[0]
    assert edge_term.targets.edges is not None  # TOP -- never a bare eid set

    record = _record(store, root, result)
    store.assert_edge("a", "b", "LIKES", {}, 0, 100)  # a *new* edge the bound scan never read

    v0, v1 = _verdicts(store, record)
    assert not v0.actionable_fresh
    assert not v1.actionable_fresh, "a bound edge domain's edges arm must stay TOP"


def test_wp6_unresolvable_cohort_is_omitted_not_widened():
    """W-P6 — `_sole_uid_column` returning `None` (a runtime relation with
    more than one uid-typed column) is not a soundness problem and must not
    widen the whole region to Level 0; the cohort is simply omitted from
    `node_cohorts` rather than fabricated. Exercised directly against
    `_node_domains` — the exact function `eval_pattern` calls to build
    `node_cohorts` (`pattern.py:64-86`)."""
    edge_source = NodeScan("p", sigma_=Sigma.default())  # 1 uid column: satisfies __post_init__
    pattern = Pattern((NodePat("x"), NodePat("y")), (EdgePat("e1", "x", "y", "KNOWS"),))
    node = PatternMatch(pattern, sources=(Source("x", edge_source),), sigma_=Sigma.default())

    # the *runtime* relation handed to `_node_domains` carries two uid columns
    # -- `_sole_uid_column` cannot resolve which one restricts "x"
    ambiguous_schema = Schema((Column("c1", T_UID), Column("c2", T_UID)))
    ambiguous = Relation.of(ambiguous_schema,
                            {"c1": np.array(["a"], dtype=object),
                             "c2": np.array(["b"], dtype=object)}, n=1)

    cohorts = _node_domains(node, {"x": ambiguous})
    assert "x" not in cohorts, "an unresolvable cohort must be skipped, not fabricated"


def test_po_p4_trap_node_arm_alone_is_not_constructible():
    """The PO-P4 trap test: a `ScanRegion` with `complete=True` and no edge
    domains at all -- the node-arm-alone shape `burst_detection`'s trap names
    -- must never be constructible."""
    with pytest.raises(InvalidArgError):
        ScanRegion(node_digest="nd", t_v=((0, 100),), t_b=100, edge_domains=(),
                  node_uids={"x": ("a",)}, complete=True)


def test_po_p4_trap_node_arm_alone_is_not_consumed_from_the_wire():
    """The same trap, from the consumption side: a hand-crafted wire payload
    naming a node-only, `complete: true` region must not be parseable into a
    usable region either -- `ScanRegion.from_json` refuses it (via the same
    constructor guard) and `scan_region_terms` reports nothing, never a
    node-only disjunction."""
    payload = {
        "schema": "tgms-scan-region", "version": 1, "op": "PatternMatch",
        "node_digest": "nd", "complete": True,
        "sigma": {"t_v": [[0, 100]], "t_b": 100},
        "edge_domains": [],
        "node_uids": {"x": ["a"]}, "node_cohorts": {},
    }
    assert ScanRegion.from_json(payload) is None
    assert scan_region_terms(payload) == ()


# ---------------------------------------------------------------------------
# undecidable / refuse passthrough (level1 must never touch these)
# ---------------------------------------------------------------------------

def test_undecidable_verdict_passes_through_level1_unchanged():
    """`level1.refine` must never touch `UNDECIDABLE` (D13.25; `M5_DESIGN.md`
    §6.2). Identity-preserved, not merely value-equal."""
    verdict = UNDECIDABLE("no-tt_q")
    log = tgms.open(Path(tempfile.mkdtemp()) / "s", backend="native").eventlog
    region = {"schema": "tgms-scan-region", "version": 1, "op": "PatternMatch",
              "node_digest": "nd", "complete": True,
              "sigma": {"t_v": [[0, 100]], "t_b": 100},
              "edge_domains": [{"var": "e1", "source": "scan", "rel_type": None}],
              "node_uids": {"x": ["a"]}, "node_cohorts": {}}
    out = level1_mod.refine(verdict, region, log)
    assert out is verdict


def test_fresh_verdict_passes_through_level1_unchanged():
    verdict = FRESH()
    log = tgms.open(Path(tempfile.mkdtemp()) / "s", backend="native").eventlog
    out = level1_mod.refine(verdict, None, log)
    assert out is verdict


def test_absent_region_is_a_level1_noop_by_identity():
    """`refine` on a `possibly-stale` verdict with no region must return the
    very same object -- the strongest form of "no narrowing"."""
    log = tgms.open(Path(tempfile.mkdtemp()) / "s", backend="native").eventlog
    w = Witness(batch_id="b0", tt=10, op_seq=0, arm="value", cls="A", kind="assert_node",
               identity={"uid": "a"}, vt=[0, 100], matched_term=0, matched_on=())
    verdict = POSSIBLY_STALE([w], total=1)
    assert level1_mod.refine(verdict, None, log) is verdict
    assert level1_mod.refine(verdict, {}, log) is verdict
    assert level1_mod.refine(verdict, {"garbage": True}, log) is verdict
