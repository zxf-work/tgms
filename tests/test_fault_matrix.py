"""[tests] The M4 gate: the fault × claim matrix stays clean.

Zero false certifications inside the published fragment is Critical
Result 1 — a failure here is not a lower metric, it means the claimed
semantics need revision (deep review §36). The cell that once failed is
pinned by name so the hole the matrix found on its first run can never
quietly reopen.
"""

from __future__ import annotations

from tgms.evidence.faultbench import run_matrix


def test_matrix_has_no_false_certifications():
    r = run_matrix()
    bad = [c for c in r.cells
           if c["expectation"] == "must_not_certify" and not c["ok"]]
    assert r.false_certifications == 0, bad


def test_matrix_has_no_false_rejections_on_clean_controls():
    r = run_matrix()
    bad = [c for c in r.cells
           if c["expectation"] == "must_certify" and not c["ok"]]
    assert r.false_rejections == 0, bad


def test_the_found_hole_stays_closed():
    # D-104: a rows-so-far counter on an interrupted execution must never
    # certify an exact count — the matrix's first-run finding
    r = run_matrix()
    cell = next(c for c in r.cells
                if c["claim"] == "exact_count"
                and c["fault"] == "execution_incomplete")
    assert cell["verdict"] != "SUPPORTED"


def test_integrity_precondition_rejects_tampered_results():
    r = run_matrix()
    cells = [c for c in r.cells if c["fault"] == "digest_mismatch"]
    assert cells
    assert all(c["verdict"] == "REJECTED_INTEGRITY" for c in cells)
