"""EvidenceBench cells for the top-k claim form.

Each cell is a (claim, evidence, result) triple over one fixed candidate
relation, with a ground-truth expectation derived from that relation by
`truth()`, the reference oracle, and the verdict expected from each of
three checkers:

- ``ecqr``          the shipped verifier (`verify`), behind the A4
                    integrity precheck; a step blocked by Lemma 3.10
                    rule (a) emits no descriptor and is reported BLOCKED;
- ``b1_value_only`` certifies iff the claimed rows are the cited rows,
                    in order (as a set for a set claim); ignores order totality, execution, k and
                    basis (the value-only baseline);
- ``b2_taint_all``  rejects whenever delivery or execution is not
                    certified complete, otherwise behaves as b1 (the
                    taint baseline).

The baselines see the descriptor a non-blocking executor would emit for
a blocked step (`Cell.unblocked`), so the blocked cell measures what
rule (a) buys. `cases()` returns the cells; `run_cells()` evaluates them
under all three checkers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tgms.core.model import canonical_json, sha256_hex
from tgms.evidence.claims import TopK
from tgms.evidence.ecqr import ECQR, Basis, Ranking, Scope, basis_identity
from tgms.evidence.verify import Verdict, verify

BASIS_TT = 100
CHECKERS = ("ecqr", "b1_value_only", "b2_taint_all")

#: the candidate relation R*(Q', B): (uid, score); uid is unique
CANDIDATES = [["n0", 50], ["n1", 90], ["n2", 70], ["n3", 90],
              ["n4", 10], ["n5", 60], ["n6", 30], ["n7", 80]]
#: total order: score desc, uid asc (uid is the tie-break key)
TOTAL = [["score", "desc"], ["uid", "asc"]]
#: the same order without its tie-break: n1 and n3 tie at score 90,
#: and with k=1 the boundary falls between them
COARSE = [["score", "desc"]]
DOMAIN = {"sql": "SELECT uid, score FROM c"}


def _sorted(rows: list[list[Any]], order: list[list[str]]) -> list[list[Any]]:
    col = {"uid": 0, "score": 1}
    out = list(rows)
    for key, d in reversed(order):
        out.sort(key=lambda r, i=col[key]: r[i], reverse=(d == "desc"))
    return out


def _digest(result: Any) -> str:
    return sha256_hex(canonical_json(result))


def _ecqr(result: dict, *, order: list[list[str]] | None, total: bool,
          limit: int, delivery: bool, execution: bool = True,
          pinned: bool = True, as_of: int = BASIS_TT,
          result_id: str | None = None, boundary: bool = False,
          boundary_as_of: int | None = None) -> ECQR:
    basis = Basis(store="bench", as_of_tt=as_of, pinned=pinned)
    bb = None
    if boundary:
        bb = basis_identity(Basis(
            store="bench", pinned=pinned,
            as_of_tt=as_of if boundary_as_of is None else boundary_as_of))
    return ECQR(
        result_id=result_id or _digest(result),
        basis=basis,
        scope=Scope(domain=dict(DOMAIN), execution_complete=execution,
                    delivery_complete=delivery,
                    rows_returned=len(result["rows"])),
        ranking=(None if order is None else Ranking(
            candidate_domain=dict(DOMAIN), limit=limit,
            order=[list(p) for p in order], order_total=total,
            boundary_strict=boundary, boundary_basis=bb)))


def _claim(rows, order, k, basis_tt=None, as_set=False) -> TopK:
    return TopK(rows=[list(r) for r in rows], key=[p[0] for p in order],
                dir=[p[1] for p in order], k=k, basis_tt=basis_tt,
                as_set=as_set)


@dataclass
class Cell:
    fault: str                       # "clean" for controls
    claim: TopK
    ecqr: ECQR | None                # None: the step is blocked
    result: Any
    expectation: str                 # must_certify | must_not_certify
    expected: dict[str, str]         # checker -> expected verdict
    candidates: list[list[Any]] = field(default_factory=lambda: CANDIDATES)
    unblocked: ECQR | None = None    # what the baselines see if blocked
    note: str = ""

    claim_kind = "top_k"


def truth(cell: Cell) -> bool:
    """Is the claim true of the full candidate relation it is about?

    A sequence claim is true iff the claimed order is a total order on
    the candidates (no two candidates share every key value) and the
    claimed rows are the first k candidates under it (all of them when
    fewer than k exist). A set claim (`as_set`) is true iff the first k
    candidates form a unique set (fewer than k+1 candidates, or the key
    at rank k differs from the key at rank k+1) and the claimed rows are
    that set. Either way the claimed basis must be the relation's.
    """
    c = cell.claim
    if c.basis_tt is not None and c.basis_tt != BASIS_TT:
        return False
    order = c.order()
    col = {"uid": 0, "score": 1}
    ranked = _sorted(cell.candidates, order)

    def key(r):
        return tuple(r[col[k]] for k, _ in order)
    if c.as_set:
        if len(ranked) > c.k and key(ranked[c.k - 1]) == key(ranked[c.k]):
            return False
        return ({tuple(r) for r in c.rows}
                == {tuple(r) for r in ranked[:c.k]})
    keys = [key(r) for r in cell.candidates]
    if len(set(keys)) != len(keys):
        return False
    return [list(r) for r in c.rows] == ranked[:c.k]


def cases() -> list[Cell]:
    top = _sorted(CANDIDATES, TOTAL)
    page3 = {"rows": top[:3]}
    cells: list[Cell] = []

    def add(fault, claim, e, result, expectation, expected, **kw):
        cells.append(Cell(fault, claim, e, result, expectation,
                          dict(zip(CHECKERS, expected)), **kw))

    S = Verdict.SUPPORTED.value
    # clean: top-3 of 8, page holds k rows, more candidates exist
    add("clean", _claim(top[:3], TOTAL, 3),
        _ecqr(page3, order=TOTAL, total=True, limit=3, delivery=False),
        page3, "must_certify", (S, S, "REJECT"),
        note="a full page of k rows needs no delivery certificate")
    # clean: k equals the number of candidates, delivery certified
    full = {"rows": top}
    add("clean_k_equals_n", _claim(top, TOTAL, 8),
        _ecqr(full, order=TOTAL, total=True, limit=8, delivery=True),
        full, "must_certify", (S, S, S))
    # fewer than k candidates, delivery certified complete (exhausted)
    few = [r for r in CANDIDATES if r[1] >= 80]
    few_sorted = _sorted(few, TOTAL)
    few_page = {"rows": few_sorted}
    add("fewer_than_k_certified", _claim(few_sorted, TOTAL, 5),
        _ecqr(few_page, order=TOTAL, total=True, limit=5, delivery=True),
        few_page, "must_certify", (S, S, S), candidates=few,
        note="the completed LIMIT 5 statement returned 3 rows")
    # LIMIT without ORDER BY: an arbitrary prefix of the candidates
    arb = {"rows": CANDIDATES[:3]}
    add("limit_without_order", _claim(CANDIDATES[:3], TOTAL, 3),
        _ecqr(arb, order=[], total=False, limit=3, delivery=False),
        arb, "must_not_certify",
        ("UNSUPPORTED_ORDER_NOT_TOTAL", S, "REJECT"),
        note="not a ranking at all; the claim names an order no one ran")
    # ORDER BY score DESC LIMIT 1: n1 and n3 tie at the boundary
    tie_page = {"rows": [["n3", 90]]}
    add("ties_without_tiebreak", _claim([["n3", 90]], COARSE, 1),
        _ecqr(tie_page, order=COARSE, total=False, limit=1,
              delivery=False),
        tie_page, "must_not_certify",
        ("UNSUPPORTED_ORDER_NOT_TOTAL", S, "REJECT"),
        note="n1 has the same score; which row is 'first' is undefined")
    # interrupted execution: the top-3 of the rows scanned so far
    scanned = CANDIDATES[:3] + CANDIDATES[4:6]
    part = {"rows": _sorted(scanned, TOTAL)[:3]}
    add("execution_incomplete", _claim(part["rows"], TOTAL, 3),
        _ecqr(part, order=TOTAL, total=True, limit=3, delivery=False,
              execution=False),
        part, "must_not_certify",
        ("UNSUPPORTED_EXECUTION_NOT_CERTIFIED", S, "REJECT"),
        note="n3 and n7 were never ranked")
    # k mismatch: a LIMIT 3 page claimed as the top 5
    add("k_mismatch", _claim(top[:3], TOTAL, 5),
        _ecqr(page3, order=TOTAL, total=True, limit=3, delivery=False),
        page3, "must_not_certify",
        ("UNSUPPORTED_K_MISMATCH", S, "REJECT"))
    # page shorter than k without certified delivery: delivery cut the
    # LIMIT 5 page at 3 rows; the 4th and 5th candidates are omitted
    add("fewer_than_k_uncertified", _claim(top[:3], TOTAL, 5),
        _ecqr(page3, order=TOTAL, total=True, limit=5, delivery=False),
        page3, "must_not_certify",
        ("UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED", S, "REJECT"))
    # top-k over an input that was itself a truncated page: blocked
    page_in = CANDIDATES[4:]
    over_page = {"rows": _sorted(page_in, TOTAL)[:2]}
    add("over_truncated_input", _claim(over_page["rows"], TOTAL, 2),
        None, over_page, "must_not_certify", ("BLOCKED", S, "REJECT"),
        unblocked=_ecqr(over_page, order=TOTAL, total=True, limit=2,
                        delivery=False),
        note="the top-2 of a 4-row input page; n1 and n3 are the full top-2")
    # claim names a different order than the evidence ranked by
    add("order_mismatch",
        _claim(top[:3], [["score", "asc"], ["uid", "asc"]], 3),
        _ecqr(page3, order=TOTAL, total=True, limit=3, delivery=False),
        page3, "must_not_certify",
        ("UNSUPPORTED_ORDER_MISMATCH", S, "REJECT"),
        note="the highest three called the lowest three")
    # wrong rows: a swapped member
    wrong = [top[0], top[1], top[4]]
    add("wrong_rows", _claim(wrong, TOTAL, 3),
        _ecqr(page3, order=TOTAL, total=True, limit=3, delivery=False),
        page3, "must_not_certify",
        ("UNSUPPORTED_VALUE_MISMATCH", "REJECT", "REJECT"))
    # basis: claim about tt=101, evidence pinned at tt=100 (complete
    # delivery, so the taint baseline has nothing to taint)
    add("wrong_snapshot", _claim(top, TOTAL, 8, basis_tt=BASIS_TT + 1),
        _ecqr(full, order=TOTAL, total=True, limit=8, delivery=True),
        full, "must_not_certify",
        ("UNSUPPORTED_BASIS_MISMATCH", S, S))
    # integrity: the page was tampered with after the descriptor bound it
    tampered = {"rows": [top[0], top[1], ["nX", 75]]}
    add("digest_mismatch", _claim(tampered["rows"], TOTAL, 3),
        _ecqr(page3, order=TOTAL, total=True, limit=3, delivery=False),
        tampered, "must_not_certify",
        ("REJECTED_INTEGRITY", "REJECTED_INTEGRITY", "REJECTED_INTEGRITY"))
    cells += _set_route_cells()
    return cells


def _set_route_cells() -> list[Cell]:
    """Cells for the boundary-strict route: the order is score DESC alone
    (not total; n1 and n3 tie at 90), and the adapter records whether
    rank k sorts strictly before rank k+1."""
    S = Verdict.SUPPORTED.value
    NT = "UNSUPPORTED_ORDER_NOT_TOTAL"
    cells: list[Cell] = []

    def add(fault, claim, e, result, expectation, expected, **kw):
        cells.append(Cell(fault, claim, e, result, expectation,
                          dict(zip(CHECKERS, expected)), **kw))

    coarse = _sorted(CANDIDATES, COARSE)          # n1, n3, n7, n2, ...
    top2 = {"rows": coarse[:2]}                   # tie inside, 90 > 80
    e_strict = _ecqr(top2, order=COARSE, total=False, limit=2,
                     delivery=False, boundary=True)
    add("set_boundary_strict_inner_tie", _claim(coarse[:2], COARSE, 2,
                                                as_set=True),
        e_strict, top2, "must_certify", (S, S, "REJECT"),
        note="{n1, n3} is the unique top-2 set; their order is a tie")
    add("sequence_boundary_strict_inner_tie", _claim(coarse[:2], COARSE, 2),
        e_strict, top2, "must_not_certify", (NT, S, "REJECT"),
        note="the set is certified, the sequence n1 before n3 is not")
    tie1 = {"rows": [["n3", 90]]}
    add("set_boundary_tie", _claim([["n3", 90]], COARSE, 1, as_set=True),
        _ecqr(tie1, order=COARSE, total=False, limit=1, delivery=False),
        tie1, "must_not_certify", (NT, S, "REJECT"),
        note="rank 1 and rank 2 tie: {n1} and {n3} are both valid top-1")
    few = [["n1", 90], ["n3", 90], ["n7", 80]]
    few_page = {"rows": _sorted(few, COARSE)}
    add("set_boundary_fewer_than_k_certified",
        _claim(few_page["rows"], COARSE, 5, as_set=True),
        _ecqr(few_page, order=COARSE, total=False, limit=5, delivery=True,
              boundary=True),
        few_page, "must_certify", (S, S, S), candidates=few,
        note="3 < k+1 candidates: the boundary is strict trivially")
    add("set_boundary_other_basis", _claim([["n3", 90]], COARSE, 1,
                                           as_set=True),
        _ecqr(tie1, order=COARSE, total=False, limit=1, delivery=False,
              boundary=True, boundary_as_of=BASIS_TT + 1),
        tie1, "must_not_certify", ("UNSUPPORTED_BASIS_MISMATCH", S, "REJECT"),
        note="strict on another state; on this one rank 1 and 2 tie")
    return cells


# ------------------------------------------------------------- checkers

def _b1(claim: TopK, e: ECQR, result: Any) -> bool:
    rows = [list(r) for r in (result.get("rows") or [])]
    mine = [list(r) for r in (claim.rows or [])]
    if claim.as_set:
        return {tuple(r) for r in mine} == {tuple(r) for r in rows}
    return mine == rows


def _b2(claim: TopK, e: ECQR, result: Any) -> bool:
    s = e.scope
    return s.delivery_complete and s.execution_complete and \
        _b1(claim, e, result)


def judge(cell: Cell, checker: str) -> str:
    """The verdict string one checker returns on one cell."""
    e = cell.ecqr if cell.ecqr is not None else cell.unblocked
    if e is not None and _digest(cell.result) != e.result_id:
        return "REJECTED_INTEGRITY"
    if checker == "ecqr":
        if cell.ecqr is None:
            return "BLOCKED"
        return verify(cell.claim, cell.ecqr, cell.result).verdict.value
    fn = _b1 if checker == "b1_value_only" else _b2
    return Verdict.SUPPORTED.value if fn(cell.claim, e, cell.result) \
        else "REJECT"


def run_cells(cells: list[Cell] | None = None) -> dict[str, Any]:
    """All cells under all checkers: verdicts, false certifications
    (SUPPORTED on must_not_certify) and false rejections."""
    cells = cells if cells is not None else cases()
    out: dict[str, Any] = {"n_cells": len(cells), "cells": [],
                           "checkers": {}}
    for ch in CHECKERS:
        out["checkers"][ch] = {"false_certifications": [],
                               "false_rejections": []}
    for c in cells:
        row = {"claim": c.claim_kind, "fault": c.fault,
               "expectation": c.expectation, "truth": truth(c),
               "note": c.note, "verdicts": {}, "expected": c.expected}
        for ch in CHECKERS:
            v = judge(c, ch)
            row["verdicts"][ch] = v
            certified = v == Verdict.SUPPORTED.value
            if certified and c.expectation == "must_not_certify":
                out["checkers"][ch]["false_certifications"].append(c.fault)
            if not certified and c.expectation == "must_certify":
                out["checkers"][ch]["false_rejections"].append(c.fault)
        out["cells"].append(row)
    return out
