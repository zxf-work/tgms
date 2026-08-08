"""EvidenceBench core: the fault × claim matrix (M4; the M1-B seed).

Systematic evidence-fault injection crossed with the claim fragment. Every
cell is a (claim, evidence, result) triple with a ground-truth expectation:

- ``must_certify``   — a clean control; any non-SUPPORTED verdict is a
                       false rejection (usefulness metric),
- ``must_not_certify`` — the injected fault makes the claim unsupported in
                       truth; a SUPPORTED verdict is a **false
                       certification**, the critical safety failure.

The published verified fragment is what this matrix certifies, not what
anyone asserts (D-098/plan §M4). The catalog is backend-independent:
cases are constructed descriptors + results, so the same matrix can run
over any adapter's output; integrity (result bytes vs result_id) is
checked by the harness before verification, implementing trust
assumption A4 as a precondition rather than a hope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from tgms.core.model import canonical_json, sha256_hex
from tgms.evidence.claims import (
    Claim,
    CompleteSet,
    ExactCount,
    Existence,
    Membership,
    Nonexistence,
    Scalar,
)
from tgms.evidence.ecqr import ECQR, Basis, Scope
from tgms.evidence.verify import Verdict, verify

BASIS_TT = 100


def _digest(result: Any) -> str:
    return sha256_hex(canonical_json(result))


def _ecqr(result: Any, *, delivery=True, execution=True, cardinality=None,
          pinned=True, as_of=BASIS_TT) -> ECQR:
    return ECQR(result_id=_digest(result),
                basis=Basis(store="bench", as_of_tt=as_of, pinned=pinned),
                scope=Scope(domain={"op": "bench"},
                            execution_complete=execution,
                            delivery_complete=delivery,
                            rows_returned=len(result.get("rows", [])),
                            exact_cardinality=cardinality))


@dataclass
class Case:
    claim_kind: str
    fault: str                     # "clean" for controls
    claim: Claim
    ecqr: ECQR
    result: Any
    expectation: str               # must_certify | must_not_certify
    note: str = ""


ROWS = [{"uid": f"n{i}"} for i in range(8)]
UIDS = [r["uid"] for r in ROWS]
FULL = {"rows": ROWS}
PAGE = {"rows": ROWS[:3]}
EMPTY = {"rows": []}


def clean_cases() -> list[Case]:
    e_full = _ecqr(FULL, cardinality=8)
    e_trunc = _ecqr(PAGE, delivery=False, cardinality=8)
    e_empty = _ecqr(EMPTY, cardinality=0)
    return [
        Case("membership", "clean", Membership(value="n2", field="uid"),
             e_full, FULL, "must_certify"),
        Case("membership", "clean", Membership(value="n1", field="uid"),
             e_trunc, PAGE, "must_certify",
             "witness rule: a delivered witness survives truncation"),
        Case("scalar", "clean", Scalar(path="rows[0].uid", value="n0"),
             e_full, FULL, "must_certify"),
        Case("exact_count", "clean", ExactCount(n=8), e_full, FULL,
             "must_certify"),
        Case("exact_count", "clean", ExactCount(n=8), e_trunc, PAGE,
             "must_certify",
             "the flagship: a certificate survives incomplete delivery"),
        Case("complete_set", "clean",
             CompleteSet(members=list(UIDS), field="uid"), e_full, FULL,
             "must_certify"),
        Case("existence", "clean", Existence(), e_full, FULL,
             "must_certify"),
        Case("nonexistence", "clean", Nonexistence(), e_empty, EMPTY,
             "must_certify"),
        Case("historical_basis", "clean", ExactCount(n=8, basis_tt=BASIS_TT),
             e_full, FULL, "must_certify"),
    ]


def _fault_page_truncation() -> list[Case]:
    """Delivery truncated, certificate stripped: page-derived numbers and
    completeness claims must die; the D-061 wrong-number is the count of
    the page."""
    e = _ecqr(PAGE, delivery=False, cardinality=None)
    return [
        Case("exact_count", "page_truncation", ExactCount(n=3), e, PAGE,
             "must_not_certify", "the page count is a wrong number"),
        Case("complete_set", "page_truncation",
             CompleteSet(members=[r["uid"] for r in PAGE["rows"]],
                         field="uid"), e, PAGE, "must_not_certify"),
        Case("nonexistence", "page_truncation", Nonexistence(),
             _ecqr(EMPTY, delivery=False, cardinality=None), EMPTY,
             "must_not_certify", "an empty page of an incomplete result"),
    ]


def _fault_execution_incomplete() -> list[Case]:
    """A backend emits a descriptor whose execution did not complete but
    which still carries a rows-so-far counter as if it were a certificate
    (review §9.1's exact scenario). Nothing global may certify."""
    e = _ecqr(PAGE, delivery=False, execution=False, cardinality=3)
    e_empty = _ecqr(EMPTY, delivery=True, execution=False, cardinality=0)
    return [
        Case("exact_count", "execution_incomplete", ExactCount(n=3), e, PAGE,
             "must_not_certify", "rows-so-far is not a certificate"),
        Case("complete_set", "execution_incomplete",
             CompleteSet(members=[r["uid"] for r in PAGE["rows"]],
                         field="uid"), e, PAGE, "must_not_certify"),
        Case("nonexistence", "execution_incomplete", Nonexistence(), e_empty,
             EMPTY, "must_not_certify",
             "an interrupted search that found nothing proves nothing"),
    ]


def _fault_value_mutations() -> list[Case]:
    e_full = _ecqr(FULL, cardinality=8)
    e_empty = _ecqr(EMPTY, cardinality=0)
    omitted = list(UIDS[:-1])
    extra = list(UIDS) + ["n99"]
    return [
        Case("exact_count", "wrong_count", ExactCount(n=9), e_full, FULL,
             "must_not_certify"),
        Case("scalar", "wrong_scalar", Scalar(path="rows[0].uid", value="nX"),
             e_full, FULL, "must_not_certify"),
        Case("complete_set", "omitted_member",
             CompleteSet(members=omitted, field="uid"), e_full, FULL,
             "must_not_certify"),
        Case("complete_set", "fabricated_member",
             CompleteSet(members=extra, field="uid"), e_full, FULL,
             "must_not_certify"),
        Case("membership", "false_membership",
             Membership(value="n99", field="uid"), e_full, FULL,
             "must_not_certify"),
        Case("existence", "false_existence", Existence(), e_empty, EMPTY,
             "must_not_certify"),
        Case("nonexistence", "false_nonexistence", Nonexistence(), e_full,
             FULL, "must_not_certify"),
    ]


def _fault_basis() -> list[Case]:
    e_pinned = _ecqr(FULL, cardinality=8, pinned=True, as_of=BASIS_TT)
    e_unpinned = _ecqr(FULL, cardinality=8, pinned=False, as_of=2**62)
    return [
        Case("historical_basis", "wrong_snapshot",
             ExactCount(n=8, basis_tt=BASIS_TT + 1), e_pinned, FULL,
             "must_not_certify"),
        Case("historical_basis", "unpinned_snapshot",
             ExactCount(n=8, basis_tt=BASIS_TT), e_unpinned, FULL,
             "must_not_certify",
             "current-beliefs evidence cannot ground a pinned claim"),
    ]


def _fault_citation() -> list[Case]:
    e_full = _ecqr(FULL, cardinality=8)
    return [
        Case("scalar", "uncited_value", Scalar(path="value", value=42),
             e_full, FULL, "must_not_certify",
             "the cited result has no such path"),
    ]


def _fault_integrity() -> list[Case]:
    """Result tampered after the descriptor was recorded — caught by the
    harness's A4 precondition (digest recheck), never reaching verify."""
    tampered = {"rows": ROWS[:-1] + [{"uid": "nTAMPERED"}]}
    e = _ecqr(FULL, cardinality=8)  # id binds the ORIGINAL bytes
    return [
        Case("membership", "digest_mismatch",
             Membership(value="nTAMPERED", field="uid"), e, tampered,
             "must_not_certify"),
        Case("complete_set", "digest_mismatch",
             CompleteSet(members=[r["uid"] for r in tampered["rows"]],
                         field="uid"), e, tampered, "must_not_certify"),
    ]


FAULT_BUILDERS: list[Callable[[], list[Case]]] = [
    _fault_page_truncation, _fault_execution_incomplete,
    _fault_value_mutations, _fault_basis, _fault_citation, _fault_integrity,
]

#: fault families named by the review that v1 cannot yet exercise — listed
#: so the matrix reports what it does NOT cover (no silent caps)
NOT_YET_COVERED = [
    "sampling", "approximate_as_exact",       # no approximate operators yet
    "mixed_snapshots_across_steps",           # needs multi-evidence claims
    "wrong_extremum", "wrong_top_k",          # claim types outside fragment
    "wrong_ordering",                          # ordering claims deferred
    "dropped_partition",                       # subsumed by execution_incomplete
    "silent_semantic_substitution",            # repair-class scoring post-v1
                                               # (EVIDENCE_MODEL v1.0 §5)
]


def all_cases() -> list[Case]:
    cases = clean_cases()
    for b in FAULT_BUILDERS:
        cases += b()
    return cases


@dataclass
class MatrixResult:
    cells: list[dict[str, Any]] = field(default_factory=list)
    false_certifications: int = 0
    false_rejections: int = 0

    @property
    def n(self) -> int:
        return len(self.cells)


def run_matrix(cases: list[Case] | None = None) -> MatrixResult:
    out = MatrixResult()
    for c in cases or all_cases():
        if _digest(c.result) != c.ecqr.result_id:
            verdict, reason = "REJECTED_INTEGRITY", \
                "result bytes do not match the descriptor's result_id (A4)"
        else:
            j = verify(c.claim, c.ecqr, c.result)
            verdict, reason = j.verdict.value, j.reason
        certified = verdict == Verdict.SUPPORTED.value
        ok = (certified if c.expectation == "must_certify"
              else not certified)
        if not ok and c.expectation == "must_not_certify":
            out.false_certifications += 1
        if not ok and c.expectation == "must_certify":
            out.false_rejections += 1
        out.cells.append({
            "claim": c.claim_kind, "fault": c.fault, "verdict": verdict,
            "reason": reason, "expectation": c.expectation, "ok": ok,
            "note": c.note})
    return out
