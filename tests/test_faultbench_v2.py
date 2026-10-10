"""[tests] EvidenceBench v2: five checkers, the composition family, and
the executor's propagation switch."""

from __future__ import annotations

import pytest

from tgms.evidence.faultbench import (
    CHECKERS,
    all_cases,
    composition_cases,
    composition_view,
    run_matrix,
    run_matrix_v2,
    _execute,
)


@pytest.fixture(scope="module")
def v2():
    return run_matrix_v2()


def _cells(v2, scope):
    return [c for c in v2["cells"] if c["scope"] == scope]


def _by(v2, fault):
    return next(c for c in v2["cells"] if c["fault"] == fault)


def test_v1_matrix_is_unchanged():
    assert len(all_cases()) == 27
    r = run_matrix()
    assert (r.false_certifications, r.false_rejections) == (0, 0)


def test_v2_shape(v2):
    single, comp = _cells(v2, "single_step"), _cells(v2, "composition")
    assert len(single) == 27
    faults = [c for c in comp if c["expectation"] == "must_not_certify"]
    controls = [c for c in comp if c["expectation"] == "must_certify"]
    assert len(faults) >= 6 and len(controls) >= 2
    assert set(v2["summary"]) == set(CHECKERS)


def test_ecqr_single_step_equals_v1_matrix(v2):
    v1 = run_matrix().cells
    for a, b in zip(v1, _cells(v2, "single_step")):
        assert (a["claim"], a["fault"]) == (b["claim"], b["fault"])
        assert (a["verdict"] == "SUPPORTED") == \
            b["decisions"]["ecqr"]["certified"]


def test_v1_baselines_reproduce_on_single_step_cells(v2):
    s = v2["summary"]
    vo = s["value_only"]["by_scope"]["single_step"]
    ta = s["taint_all"]["by_scope"]["single_step"]
    assert (vo["false_accepts"], vo["false_rejects"]) == (8, 0)
    assert (ta["false_accepts"], ta["false_rejects"]) == (2, 2)


def test_metadata_rules_match_ecqr_on_single_step_cells(v2):
    for c in _cells(v2, "single_step"):
        d = c["decisions"]
        assert d["metadata_rules"]["certified"] == d["ecqr"]["certified"], c


@pytest.mark.parametrize("fault", [
    "filter_then_reduce", "filter_then_count",
    "projection_then_complete_set", "scalar_from_incomplete_input",
    "filter_over_interrupted_scan", "two_hop_truncation"])
def test_propagation_is_what_rejects_truncation_compositions(v2, fault):
    d = _by(v2, fault)["decisions"]
    assert d["ecqr"]["certified"] is False
    assert d["taint_all"]["certified"] is False
    assert d["metadata_rules"]["certified"] is True
    assert d["ecqr_no_propagation"]["certified"] is True
    assert d["value_only"]["certified"] is True


def test_reducers_are_blocked_not_judged(v2):
    for fault in ("filter_then_reduce", "scalar_from_incomplete_input"):
        assert _by(v2, fault)["decisions"]["ecqr"]["verdict"] == "BLOCKED"


def test_controls_certify_under_propagation(v2):
    for c in _cells(v2, "composition"):
        if c["expectation"] == "must_certify":
            assert c["decisions"]["ecqr"]["certified"], c
            assert c["decisions"]["metadata_rules"]["certified"], c


def test_taint_all_over_rejects_witness_controls(v2):
    witness = [c for c in _cells(v2, "composition")
               if c["expectation"] == "must_certify"
               and c["claim"] in ("membership", "existence")]
    assert witness
    assert all(not c["decisions"]["taint_all"]["certified"]
               for c in witness)


@pytest.mark.xfail(strict=True, reason="the executor does not check basis "
                   "compatibility across a step's inputs")
def test_mixed_basis_inputs_are_not_certified(v2):
    assert not _by(v2, "mixed_basis_inputs")["decisions"]["ecqr"]["certified"]


def test_executor_propagation_switch():
    case = next(c for c in composition_cases()
                if c.fault == "filter_then_count")
    on, _ = _execute(case, propagate=True)
    off, _ = _execute(case, propagate=False)
    e_on = {s["step_id"]: s["ecqr"] for s in on.steps}["s2"]
    e_off = {s["step_id"]: s["ecqr"] for s in off.steps}["s2"]
    assert e_on["scope"]["exact_cardinality"] is None
    assert e_on["scope"]["delivery_complete"] is False
    assert e_off["scope"]["exact_cardinality"] == 2
    assert e_off["scope"]["delivery_complete"] is True

    red = next(c for c in composition_cases()
               if c.fault == "filter_then_reduce")
    on, _ = _execute(red, propagate=True)
    off, _ = _execute(red, propagate=False)
    assert {s["step_id"]: s["status"] for s in on.steps}["s3"] == "failed"
    assert {s["step_id"]: s["status"] for s in off.steps}["s3"] == "ok"


def test_views_are_integrity_bound():
    from tgms.evidence.faultbench import _digest
    for c in composition_cases():
        v = composition_view(c)
        assert _digest(v.result) == v.raw_ecqr.result_id
        if v.ecqr is not None:
            assert _digest(v.ecqr_result) == v.ecqr.result_id


def test_deterministic():
    a, b = run_matrix_v2(), run_matrix_v2()
    strip = [(c["id"], {k: (d["certified"], d["verdict"])
                        for k, d in c["decisions"].items()})
             for c in a["cells"]]
    assert strip == [(c["id"], {k: (d["certified"], d["verdict"])
                                for k, d in c["decisions"].items()})
                     for c in b["cells"]]
