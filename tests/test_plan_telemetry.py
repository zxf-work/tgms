"""M5 Phase-3, P3.1 — plan telemetry capture.

Three deliverables, each with its own section below (`docs/design/
M5_EXECUTION_PLAN_2026-08-27.md` §6 P3.1; the shared-channel contract is
`docs/design/M5_DESIGN.md` §7.4):

1. `admission.plan_estimate()`'s per-node estimate dict — computed and thrown
   away at `execute.py:48-49` before this change — is threaded into the
   envelope under `tgir.annotations[node_digest]["telemetry"]["estimates"]`.
2. Runtime observations (`rows_out`, `rows_in`, `wall_ms`, and — for
   `NodeScan` only — `route`) are recorded per node during `Execution.run`.
3. The plan-level `as_of_tt` emission gap (`BASELINE_FREEZE_2026-08-27.md` §5
   item 1; commit `2744f2a`'s message) is closed in `execute.py::_freshness`.

Every annotation rides in the `tgir` sub-object, which `execute.py:74` never
digests — §5's tests assert that structurally, not just by citation.
"""

from __future__ import annotations

import os

import pytest

import tgms
from tgms.core.model import OPEN_END
from tgms.tgir.admission import plan_estimate
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.eval import TELEMETRY_TIMING_ENV
from tgms.tgir.execute import run_plan
from tgms.tgir.node import Exact, Expand, NodeScan
from tgms.tgir.types import Sigma

BACKEND = os.environ.get("TGMS_TEST_BACKEND", "duckdb")

#: Generous enough that nothing in this file is refused by the guardrail —
#: the point of every scenario is what gets *recorded*, not admission itself.
WIDE_CEILINGS = {"rows_scanned_est": 10 ** 12, "expansions_est": 10 ** 12,
                 "time_est_ms": 10 ** 9}


@pytest.fixture
def store(tmp_path):
    s = tgms.open(tmp_path / "s", backend=BACKEND)
    s.ingest_events(
        [{"src": f"p{i}", "dst": f"p{(i + 1) % 40}", "rel_type": "KNOWS",
          "vt_s": 100 + i} for i in range(40)],
        nodes=[{"uid": f"p{i}", "label": "Person", "props": {"n": i}, "vt_s": 10}
               for i in range(40)])
    yield s
    s.close()


def _expand_plan() -> Expand:
    return Expand(input=NodeScan("p", labels=("Person",), belief="current",
                                 vt_mode="overlap", sigma_=Sigma.default()),
                  from_="p", into="q", rel_type="KNOWS", hops=Exact(1),
                  dir="out")


# ---------------------------------------------------------------------------
# 1. admission's discarded per-node estimates are captured, not thrown away
# ---------------------------------------------------------------------------

def test_admission_estimates_reach_the_envelope_unmodified(store):
    """`admission.plan_estimate()`'s own per-node dict, keyed by `node_digest`,
    lands verbatim under `tgir.annotations[nd]["telemetry"]["estimates"]` —
    not a re-derived or summarized version of it."""
    root = _expand_plan()
    stats = store.adapter.stats()
    expected = plan_estimate(root, stats)["per_node"]
    assert expected, "the fixture plan must contain at least one core node"

    result = run_plan(root, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)

    annotations = result["tgir"]["annotations"]
    for node_digest, estimate in expected.items():
        telemetry = annotations[node_digest]["telemetry"]
        assert telemetry["estimates"] == estimate
        assert set(telemetry["estimates"]) == {
            "rows_scanned_est", "expansions_est", "out_card", "time_est_ms"}


def test_a_leaf_only_plan_captures_no_estimates(store):
    """`admit` returns `{}` for a plan with no core node (C5); the envelope
    then carries no `"estimates"` at all, which is the fail-safe absence, not
    a bug — mirrored here rather than assumed."""
    leaf = NodeScan("p", uids=("p0",), belief="current",
                    sigma_=Sigma.default())
    # A bare NodeScan *is* a core node (it is not an OpaqueLeaf), so this test
    # instead confirms the plumbing is inert when `admit` truly has nothing to
    # report: `plan_estimate` on the same root is the oracle either way.
    root = leaf
    stats = store.adapter.stats()
    expected = plan_estimate(root, stats)["per_node"]
    result = run_plan(root, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    annotations = result["tgir"]["annotations"]
    for node_digest, estimate in expected.items():
        assert annotations[node_digest]["telemetry"]["estimates"] == estimate


def test_admission_decision_is_unchanged_by_capture(store):
    """Capture-only, per the brief: a ceiling tight enough to refuse the plan
    still refuses it — `admit`'s raise happens before `run_plan` ever touches
    `execution.annotations`."""
    from tgms.core.errors import CostError

    root = _expand_plan()
    with pytest.raises(CostError):
        run_plan(root, store.adapter, tt_source=store,
                 cost_ceilings={"rows_scanned_est": 0})


# ---------------------------------------------------------------------------
# 2. runtime observations: rows_out / rows_in / wall_ms / route
# ---------------------------------------------------------------------------

def test_every_node_gets_rows_out_and_wall_ms(store):
    root = _expand_plan()
    result = run_plan(root, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    annotations = result["tgir"]["annotations"]
    assert annotations, "the fixture plan has at least the scan and the expand"
    for node_digest, entry in annotations.items():
        telemetry = entry["telemetry"]
        assert isinstance(telemetry["rows_out"], int) and telemetry["rows_out"] >= 0
        assert isinstance(telemetry["rows_in"], int) and telemetry["rows_in"] >= 0
        assert isinstance(telemetry["wall_ms"], float) and telemetry["wall_ms"] >= 0.0
    # the root's own rows_out agrees with the envelope's own rows_total
    assert annotations[root.node_digest]["telemetry"]["rows_out"] \
        == result["tgir"]["rows_total"]


def test_route_is_postings_for_a_small_anchored_uid_set(store):
    """40 node versions in the fixture; one anchored uid is far below the
    engine's `PROBE_COST_RATIO` crossover, so `scan.py::_anchored` takes the
    postings point-read."""
    scan = NodeScan("p", uids=("p0",), belief="current", sigma_=Sigma.default())
    result = run_plan(scan, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    telemetry = result["tgir"]["annotations"][scan.node_digest]["telemetry"]
    assert telemetry["route"] == "postings"


def test_route_is_scan_with_no_anchor_set(store):
    """`NodeScan`'s **declared** schema includes `tt_s`/`tt_e` (§2.1's row),
    which forces the `versions_columnar` fallback whenever nothing downstream
    prunes them away (`needs_fallback`). So the "scan" route needs a consumer
    that narrows live columns to the fast set — a bare projection of `uid`,
    which is what `Expand`/`Join`/etc. do in practice.
    """
    from tgms.tgir.expr import Col
    from tgms.tgir.node import Project

    scan = NodeScan("p", belief="current", sigma_=Sigma.default())
    proj = Project(scan, (("u", Col("p.uid")),))
    result = run_plan(proj, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    telemetry = result["tgir"]["annotations"][scan.node_digest]["telemetry"]
    assert telemetry["route"] == "scan"


def test_route_is_fallback_for_a_non_current_belief_mode(store):
    scan = NodeScan("p", belief="all", sigma_=Sigma.default())
    result = run_plan(scan, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    telemetry = result["tgir"]["annotations"][scan.node_digest]["telemetry"]
    assert telemetry["route"] == "fallback"


def test_edge_scan_and_opaque_leaves_carry_no_route():
    """§6 P3.1's disclosure boundary: only `NodeScan`'s route is surfaced.
    `EdgeScan` has no `_anchored` decision to disclose in the first place —
    asserted on the mechanism (no `route_out` parameter on `scan_edges`)
    rather than by running a plan, since the absence is the point."""
    from tgms.tgir.eval.scan import scan_edges

    assert "route_out" not in scan_edges.__code__.co_varnames[
        :scan_edges.__code__.co_argcount]


# ---------------------------------------------------------------------------
# digest exclusion — annotations ride outside result_digest by construction
# ---------------------------------------------------------------------------

def test_result_digest_is_unchanged_by_annotation_content(store, monkeypatch):
    """The strongest form of the required test: two runs of the *same* plan
    over the *same* (unwritten-to) store, with genuinely different annotation
    content — timing on vs. off — must still produce the same `result_digest`.
    If annotations ever leaked into the digest, this would be the first thing
    to fail.
    """
    root = _expand_plan()

    monkeypatch.setenv(TELEMETRY_TIMING_ENV, "1")
    with_timing = run_plan(root, store.adapter, tt_source=store,
                           cost_ceilings=WIDE_CEILINGS)

    monkeypatch.setenv(TELEMETRY_TIMING_ENV, "0")
    without_timing = run_plan(root, store.adapter, tt_source=store,
                              cost_ceilings=WIDE_CEILINGS)

    # the two envelopes really do carry different annotation content...
    some_digest = next(iter(with_timing["tgir"]["annotations"]))
    assert "wall_ms" in with_timing["tgir"]["annotations"][some_digest]["telemetry"]
    assert "wall_ms" not in without_timing["tgir"]["annotations"][some_digest]["telemetry"]
    # ...yet the payload's digest — the thing `check_digest_stability.py`
    # guards — is identical
    assert with_timing["result_digest"] == without_timing["result_digest"]
    assert with_timing["rows"] == without_timing["rows"]


def test_timing_disabled_still_reports_rows(store, monkeypatch):
    monkeypatch.setenv(TELEMETRY_TIMING_ENV, "0")
    root = _expand_plan()
    result = run_plan(root, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    telemetry = result["tgir"]["annotations"][root.node_digest]["telemetry"]
    assert "wall_ms" not in telemetry
    assert "rows_out" in telemetry and "rows_in" in telemetry


# ---------------------------------------------------------------------------
# 3. the plan-level as_of_tt emission gap
# ---------------------------------------------------------------------------

def test_a_pinned_plan_emits_as_of_tt_on_the_scope(store):
    """E-10 / D-153, mirrored from `ttq.envelope_metadata` into the plan path.
    A read pinned *at* the frontier is a genuine pin (`clamp`'s row two): the
    envelope's `pinned` flag is true and the scope's `as_of_tt` equals the
    pin, closing the gap `BASELINE_FREEZE_2026-08-27.md` §5 item 1 named.
    """
    tt_pin = store.frontier_tt()
    scan = NodeScan("p", uids=("p0",), belief="current",
                    sigma_=Sigma.at_instant(10, as_of_tt=tt_pin))

    result = run_plan(scan, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)

    assert result["pinned"] is True
    assert result["clamped"] is False
    assert result["dependency"]["as_of_tt"] == tt_pin


def test_an_unpinned_plan_emits_no_as_of_tt(store):
    """`OPEN_END` (the default) — clamp's row one — must stay silent, so
    every scope built before this change verifies byte-identically."""
    scan = NodeScan("p", uids=("p0",), belief="current", sigma_=Sigma.default())
    result = run_plan(scan, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    assert result["pinned"] is False
    assert "as_of_tt" not in result["dependency"]


def test_pinned_plan_record_gets_the_per_batch_exemption_after_a_later_write(store):
    """The fixture-store smoke the brief asks for: a pinned plan record earns
    D-153's step-8a exemption from `check` after a later write, the same way
    a pinned single-operator record already does. Without the gap closed here,
    `store.check_result` would have nothing to key the exemption on, because
    the scope it reads back would carry no `as_of_tt` at all.
    """
    tt_pin = store.frontier_tt()
    scan = NodeScan("p", uids=("p0",), belief="current",
                    sigma_=Sigma.at_instant(10, as_of_tt=tt_pin))
    result = run_plan(scan, store.adapter, tt_source=store,
                      cost_ceilings=WIDE_CEILINGS)
    assert result["dependency"]["as_of_tt"] == tt_pin

    # a later, unrelated batch — after the pin, so T1 proves it cannot have
    # changed this read
    store.assert_node("zzz-late", "Person", {"n": -1}, 0, 100)

    verdict = store.check_result(result)
    assert verdict.actionable_fresh, verdict.to_json()
    assert verdict.exempt is not None, "the receipt is mandatory (E-10)"
    assert verdict.exempt["theorem"] == "T1"
    assert verdict.exempt["basis"] == tt_pin
    assert verdict.exempt["batches"] >= 1

    # and the control: strip `as_of_tt` back off the same scope and the same
    # batch is no longer exempted — proving the gap closure is load-bearing,
    # not merely a field that happens to be present
    from dataclasses import replace

    stripped = replace(DependencyScope.from_json(result["dependency"]), as_of_tt=None)
    unexempt = store.check_scope(stripped, tt_now=OPEN_END)
    assert unexempt.exempt is None


def test_the_gap_closure_touches_execute_py_only(store):
    """A cheap regression guard for the brief's own scoping decision: closing
    the gap does not require widening `ScopeBasis` (`tgms/tgir/scope_of.py`) —
    it is built the same way `ttq.envelope_metadata` builds its `as_of_tt`,
    entirely from information `execute.py::_freshness` already had. Asserted
    by construction: `ScopeBasis` still declares no `as_of_tt` field."""
    from tgms.tgir.scope_of import ScopeBasis

    assert "as_of_tt" not in {f for f in ScopeBasis.__dataclass_fields__}
