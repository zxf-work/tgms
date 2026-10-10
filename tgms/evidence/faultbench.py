"""EvidenceBench core: the fault × claim matrix (M4; the M1-B seed).

Systematic evidence-fault injection crossed with the claim fragment. Every
cell is a (claim, evidence, result) triple with a ground-truth expectation:

- ``must_certify``   — a clean control; any non-SUPPORTED verdict is a
                       false rejection (usefulness metric),
- ``must_not_certify`` — the injected fault makes the claim unsupported in
                       truth; a SUPPORTED verdict is a **false
                       certification**, the critical safety failure.

The formal rules define the verified fragment; this matrix tests the
shipped implementation's conformance to them over the declared fault
families (D-098/plan §M4, review P1.8). The catalog is backend-independent:
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


# --------------------------------------------------------------------------- #
# v2: the composition family and the five-checker matrix                      #
# --------------------------------------------------------------------------- #
#
# The 27 cells above are single-step: each cites one descriptor whose own
# fields carry the fault. The composition family adds plans of two or three
# steps in which the cited step's OWN metadata is clean while an upstream
# input was truncated, interrupted, or read at another basis. Each plan runs
# through the real Executor (tgms.agent.executor) over a scripted router, so
# the descriptors are the ones the shipped executor and TGMS adapter emit,
# with cross-step propagation (Lemma 3.10: blocking, rule (a); marking,
# rule (c)) on, and again with it off (Executor(propagate=False)).

#: composition fixture: 8 rows, two groups, a numeric weight
COMP_ROWS = [{"uid": f"n{i}", "grp": i % 2, "w": i} for i in range(8)]
COMP_PAGE = COMP_ROWS[:3]


@dataclass
class CompositionCase:
    claim_kind: str
    fault: str                     # "clean" for controls
    claim: Claim
    plan: dict[str, Any]           # plan JSON, executed by the real Executor
    sources: dict[str, dict[str, Any]]  # source name -> rows/truncated/rows_total
    cite: str                      # step id the claim cites
    expectation: str               # must_certify | must_not_certify
    note: str = ""


class _BenchAdapter:
    path = "bench"


class _BenchRouter:
    """Scripted stand-in for ToolRouter: source steps return their fixture
    page with the fixture's own `truncated` / `rows_total`; `compute` steps
    evaluate a small row-local or reducing function over resolved inputs."""

    def __init__(self, sources: dict[str, dict[str, Any]]) -> None:
        self.sources = sources
        self.adapter = _BenchAdapter()
        self.payloads: dict[str, Any] = {}

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "compute":
            payload, truncated, total = _bench_compute(args)
        else:
            src = self.sources[args["src"]]
            payload = {"rows": [dict(r) for r in src["rows"]]}
            truncated = bool(src.get("truncated", False))
            total = src.get("rows_total")
        d = _digest(payload)
        self.payloads[d] = payload
        env: dict[str, Any] = {
            "op": op, "truncated": truncated, "result_digest": d,
            "args_echo": {k: v for k, v in args.items()
                          if k not in ("rows", "a", "b")}}
        env.update(payload)
        if total is not None:
            env["rows_total"] = total
        return env


def _bench_compute(args: dict[str, Any]) -> tuple[Any, bool, int | None]:
    fn = args["fn"]
    if fn == "filter":        # row-local selection
        out = [r for r in args["rows"] if r.get(args["field"]) in args["in"]]
        return {"rows": out}, False, len(out)
    if fn == "derive":        # row-local projection
        out = [{args["field"]: r.get(args["field"])} for r in args["rows"]]
        return {"rows": out}, False, len(out)
    if fn == "intersect":     # same-state set operation over two inputs
        out = [{"uid": u} for u in sorted(set(args["a"]) & set(args["b"]))]
        return {"rows": out}, False, len(out)
    if fn == "count":         # reducer
        return {"value": len(args["rows"])}, False, None
    if fn == "mean":          # reducer
        xs = [r[args["field"]] for r in args["rows"]]
        return {"value": sum(xs) / len(xs)}, False, None
    raise ValueError(f"bench compute has no fn {fn!r}")


def _plan(pid: str, steps: list[dict[str, Any]], answer: str) -> dict[str, Any]:
    return {"plan_id": pid, "steps": steps,
            "answer_spec": {"kind": "value", "from": answer}}


def _scan(sid: str, src: str, as_of: int | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"src": src}
    if as_of is not None:
        args["as_of_tt"] = as_of
    return {"id": sid, "op": "scan", "args": args}


def _compute(sid: str, deps: list[str], **args: Any) -> dict[str, Any]:
    return {"id": sid, "op": "compute", "args": args, "depends_on": deps}


def composition_cases() -> list[CompositionCase]:
    page = {"rows": COMP_PAGE, "truncated": True, "rows_total": 8}
    partial = {"rows": COMP_PAGE, "truncated": True}   # rows-so-far, no total
    full = {"rows": COMP_ROWS, "truncated": False, "rows_total": 8}
    # the store before (tt=100) and after (tt=200) a correction that
    # retracts n7 and asserts n8
    at100 = {"rows": COMP_ROWS, "truncated": False, "rows_total": 8}
    at200 = {"rows": COMP_ROWS[:7] + [{"uid": "n8", "grp": 0, "w": 8}],
             "truncated": False, "rows_total": 8}
    rows1 = {"$ref": "s1.rows"}
    rows2 = {"$ref": "s2.rows"}
    filt = dict(fn="filter", rows=rows1, field="grp", **{"in": [0]})
    s2_filter = _compute("s2", ["s1"], **filt)

    def p(pid, steps, cite):
        return _plan(pid, steps, cite)

    return [
        CompositionCase(
            "scalar", "filter_then_reduce", Scalar(path="value", value=2),
            p("c-filter-count", [_scan("s1", "page"), s2_filter,
                                 _compute("s3", ["s2"], fn="count",
                                          rows=rows2)], "s3"),
            {"page": page}, "s3", "must_not_certify",
            "count of a filter over a 3-of-8 page; the full-input count is 4"),
        CompositionCase(
            "exact_count", "filter_then_count", ExactCount(n=2),
            p("c-filter-exact", [_scan("s1", "page"), s2_filter], "s2"),
            {"page": page}, "s2", "must_not_certify",
            "the filter's own rows_total counts the page; full input gives 4"),
        CompositionCase(
            "complete_set", "projection_then_complete_set",
            CompleteSet(members=["n0", "n1", "n2"], field="uid"),
            p("c-proj-set", [_scan("s1", "page"),
                             _compute("s2", ["s1"], fn="derive", rows=rows1,
                                      field="uid")], "s2"),
            {"page": page}, "s2", "must_not_certify",
            "projection of a truncated page, offered as the complete set"),
        CompositionCase(
            "scalar", "scalar_from_incomplete_input",
            Scalar(path="value", value=1.0),
            p("c-mean", [_scan("s1", "page"),
                         _compute("s2", ["s1"], fn="mean", rows=rows1,
                                  field="w")], "s2"),
            {"page": page}, "s2", "must_not_certify",
            "mean weight over the page; the full-input mean is 3.5"),
        CompositionCase(
            "nonexistence", "filter_over_interrupted_scan", Nonexistence(),
            p("c-absent", [_scan("s1", "partial"),
                           _compute("s2", ["s1"], fn="filter", rows=rows1,
                                    field="uid", **{"in": ["n6"]})], "s2"),
            {"partial": partial}, "s2", "must_not_certify",
            "n6 is absent from the rows-so-far, not from the input"),
        CompositionCase(
            "exact_count", "two_hop_truncation", ExactCount(n=2),
            p("c-two-hop", [_scan("s1", "page"), s2_filter,
                            _compute("s3", ["s2"], fn="derive", rows=rows2,
                                     field="uid")], "s3"),
            {"page": page}, "s3", "must_not_certify",
            "truncation two row-local hops upstream of the cited step"),
        CompositionCase(
            "exact_count", "mixed_basis_inputs", ExactCount(n=7),
            p("c-mixed-basis", [
                _scan("s1", "at100", as_of=100),
                _scan("s2", "at200", as_of=200),
                _compute("s3", ["s1", "s2"], fn="intersect",
                         a={"$ref": "s1.rows[*].uid"},
                         b={"$ref": "s2.rows[*].uid"})], "s3"),
            {"at100": at100, "at200": at200}, "s3", "must_not_certify",
            "a same-state operator over inputs read at tt=100 and tt=200; "
            "the basis-compatibility definition requires the step to fail, "
            "while its own descriptor is complete and certified"),
        # controls: propagation keeps the evidence the paper says it keeps
        CompositionCase(
            "membership", "clean", Membership(value="n2", field="uid"),
            p("k-witness", [_scan("s1", "page"), s2_filter], "s2"),
            {"page": page}, "s2", "must_certify",
            "rule (b): a row-local filter preserves a delivered witness"),
        CompositionCase(
            "existence", "clean", Existence(),
            p("k-exists", [_scan("s1", "page"), s2_filter], "s2"),
            {"page": page}, "s2", "must_certify",
            "rule (b): one delivered witness establishes existence"),
        CompositionCase(
            "exact_count", "clean", ExactCount(n=4),
            p("k-full-exact", [_scan("s1", "full"), s2_filter], "s2"),
            {"full": full}, "s2", "must_certify",
            "complete input: the filter's certificate is kept"),
        CompositionCase(
            "scalar", "clean", Scalar(path="value", value=4),
            p("k-full-count", [_scan("s1", "full"), s2_filter,
                               _compute("s3", ["s2"], fn="count",
                                        rows=rows2)], "s3"),
            {"full": full}, "s3", "must_certify",
            "complete input: the reducer runs and its value is certified"),
    ]


@dataclass
class View:
    """What each checker may see for one cell. `raw_*` is the cited step as
    the adapter emitted it with propagation off; `ecqr` is the shipped
    system's descriptor (None when the step was blocked or failed)."""
    claim: Claim
    result: Any
    raw_ecqr: ECQR
    ecqr: ECQR | None
    ecqr_result: Any
    closure_incomplete: bool
    no_descriptor_reason: str | None = None


def _closure(plan: dict[str, Any], sid: str) -> set[str]:
    deps = {s["id"]: s.get("depends_on", []) for s in plan["steps"]}
    out, todo = set(), [sid]
    while todo:
        x = todo.pop()
        if x not in out:
            out.add(x)
            todo.extend(deps.get(x, []))
    return out


def _execute(case: CompositionCase, propagate: bool):
    from tgms.agent.executor import Executor
    from tgms.agent.ir import Plan
    router = _BenchRouter(case.sources)
    trace = Executor(router, propagate=propagate).run(  # type: ignore[arg-type]
        Plan.from_json(case.plan))
    return trace, router


def composition_view(case: CompositionCase) -> View:
    raw_trace, raw_router = _execute(case, propagate=False)
    full_trace, full_router = _execute(case, propagate=True)
    raw = {r["step_id"]: r for r in raw_trace.steps}
    full = {r["step_id"]: r for r in full_trace.steps}
    rec = raw[case.cite]
    if rec.get("status") != "ok" or not rec.get("ecqr"):
        raise RuntimeError(f"{case.fault}: cited step did not run without "
                           f"propagation: {rec}")
    raw_e = ECQR.from_json(rec["ecqr"])
    frec = full[case.cite]
    e, e_res, why = None, None, None
    if frec.get("status") == "ok" and frec.get("ecqr"):
        e = ECQR.from_json(frec["ecqr"])
        e_res = full_router.payloads[frec["result_digest"]]
    else:
        why = ("BLOCKED" if frec.get("upstream_truncated")
               and frec.get("status") == "failed" else "NO_DESCRIPTOR")
    closure = _closure(case.plan, case.cite)
    incomplete = any(raw[s].get("truncated") or raw[s].get("status") != "ok"
                     for s in closure)
    return View(claim=case.claim,
                result=raw_router.payloads[rec["result_digest"]],
                raw_ecqr=raw_e, ecqr=e, ecqr_result=e_res,
                closure_incomplete=incomplete, no_descriptor_reason=why)


def single_step_view(c: Case) -> View:
    s = c.ecqr.scope
    return View(claim=c.claim, result=c.result, raw_ecqr=c.ecqr, ecqr=c.ecqr,
                ecqr_result=c.result,
                closure_incomplete=not (s.delivery_complete
                                        and s.execution_complete))


# ---- the five checkers: View -> (certified, verdict) ---------------------- #

def _rows_of(result: Any) -> list[Any]:
    from tgms.evidence.verify import _rows
    return _rows(result)


def _projected(rows: list[Any], fld: str | None) -> set[Any]:
    return {r.get(fld) if fld and isinstance(r, dict)
            else (r if not isinstance(r, dict) else str(r)) for r in rows}


def check_value_only(v: View) -> tuple[bool, str]:
    """B1: the claimed value or witness appears in the cited result; the
    descriptor's cardinality is read as a number, never as a condition."""
    from tgms.evidence.verify import _resolve_path, _row_matches
    claim, rows = v.claim, _rows_of(v.result)
    if isinstance(claim, Membership):
        ok = any(_row_matches(r, claim.value, claim.field) for r in rows)
    elif isinstance(claim, Scalar):
        found, got = _resolve_path(v.result, claim.path)
        ok = found and got == claim.value
    elif isinstance(claim, ExactCount):
        ok = claim.n == len(rows) or \
            claim.n == v.raw_ecqr.scope.exact_cardinality
    elif isinstance(claim, CompleteSet):
        want = {x if not isinstance(x, dict) else str(x)
                for x in (claim.members or [])}
        ok = want == _projected(rows, claim.field)
    elif isinstance(claim, Existence):
        ok = bool(rows)
    elif isinstance(claim, Nonexistence):
        ok = not rows
    else:
        ok = False
    return ok, "ACCEPT" if ok else "REJECT"


def check_taint_all(v: View) -> tuple[bool, str]:
    """B2: reject anything whose dependency closure (the cited step
    included) has a delivery- or execution-incomplete step; otherwise B1."""
    if v.closure_incomplete:
        return False, "TAINTED"
    return check_value_only(v)


def metadata_of(e: ECQR) -> dict[str, Any]:
    """The cited step's ordinary result metadata, with nothing inherited
    from its inputs: the truncation flag, rows_total when the backend
    reports one, an execution status (partial / timed-out), and the
    basis identity."""
    s = e.scope
    return {"truncated": not s.delivery_complete,
            "rows_total": s.exact_cardinality,
            "execution_complete": s.execution_complete,
            "pinned": e.basis.pinned, "as_of_tt": e.basis.as_of_tt}


def check_metadata_rules(v: View) -> tuple[bool, str]:
    """The strongest per-step rule set we could write without descriptor
    propagation. It reads only the cited step's own metadata
    (`metadata_of`) and the delivered result:

    R0 AtBasis(T): reject unless the step's basis is pinned at exactly T.
    R1 Membership: accept iff a delivered row projects to the value
       (a witness on a page is still a witness).
    R2 Scalar: accept iff the path resolves to the value in the result.
    R3 ExactCount: accept iff rows_total is present, the execution is not
       partial, and rows_total equals n; or the result is not truncated,
       the execution is not partial, and the delivered row count is n.
    R4 CompleteSet: accept iff not truncated, not partial, and the
       delivered projection equals the claimed set.
    R5 Existence: accept iff a delivered row exists (R1 without a value).
    R6 Nonexistence: accept iff the execution is not partial and either
       rows_total is 0 or the result is not truncated and empty.
    """
    from tgms.evidence.verify import _resolve_path, _row_matches
    md = metadata_of(v.raw_ecqr)
    claim, rows = v.claim, _rows_of(v.result)
    if claim.basis_tt is not None and not (
            md["pinned"] and md["as_of_tt"] == claim.basis_tt):
        return False, "R0_BASIS"
    done = md["execution_complete"]
    whole = done and not md["truncated"]
    if isinstance(claim, Membership):
        ok = any(_row_matches(r, claim.value, claim.field) for r in rows)
        return ok, "R1"
    if isinstance(claim, Scalar):
        found, got = _resolve_path(v.result, claim.path)
        return found and got == claim.value, "R2"
    if isinstance(claim, ExactCount):
        if md["rows_total"] is not None and done:
            return md["rows_total"] == claim.n, "R3_ROWS_TOTAL"
        return whole and len(rows) == claim.n, "R3_PAGE"
    if isinstance(claim, CompleteSet):
        want = {x if not isinstance(x, dict) else str(x)
                for x in (claim.members or [])}
        return whole and want == _projected(rows, claim.field), "R4"
    if isinstance(claim, Existence):
        return bool(rows), "R5"
    if isinstance(claim, Nonexistence):
        ok = done and (md["rows_total"] == 0 or (whole and not rows))
        return ok, "R6"
    return False, "OUTSIDE"


def check_ecqr_no_propagation(v: View) -> tuple[bool, str]:
    """The real verifier over the cited step's descriptor as the adapter
    emitted it with Lemma 3.10 propagation off (no blocking, no marking)."""
    j = verify(v.claim, v.raw_ecqr, v.result)
    return j.verdict == Verdict.SUPPORTED, j.verdict.value


def check_ecqr(v: View) -> tuple[bool, str]:
    """The shipped system: propagation on; a blocked or failed step emits
    no descriptor, so a claim citing it cannot be certified."""
    if v.ecqr is None:
        return False, v.no_descriptor_reason or "NO_DESCRIPTOR"
    j = verify(v.claim, v.ecqr, v.ecqr_result)
    return j.verdict == Verdict.SUPPORTED, j.verdict.value


CHECKERS: dict[str, Callable[[View], tuple[bool, str]]] = {
    "value_only": check_value_only,
    "taint_all": check_taint_all,
    "metadata_rules": check_metadata_rules,
    "ecqr_no_propagation": check_ecqr_no_propagation,
    "ecqr": check_ecqr,
}

_FAMILY = {
    _fault_page_truncation: "page_truncation",
    _fault_execution_incomplete: "execution_incomplete",
    _fault_value_mutations: "value_mutation",
    _fault_basis: "basis",
    _fault_citation: "citation",
    _fault_integrity: "integrity",
}


def v2_cells() -> list[dict[str, Any]]:
    """Every v2 cell with its view: the 27 single-step cells (in all_cases
    order) followed by the composition family."""
    cells = [{"scope": "single_step", "family": "control",
              "case": c, "view": single_step_view(c)} for c in clean_cases()]
    for b in FAULT_BUILDERS:
        cells += [{"scope": "single_step", "family": _FAMILY[b],
                   "case": c, "view": single_step_view(c)} for c in b()]
    cells += [{"scope": "composition", "family": "composition",
               "case": c, "view": composition_view(c)}
              for c in composition_cases()]
    return cells


def _integrity_ok(v: View) -> bool:
    if _digest(v.result) != v.raw_ecqr.result_id:
        return False
    return v.ecqr is None or _digest(v.ecqr_result) == v.ecqr.result_id


def run_matrix_v2() -> dict[str, Any]:
    """Five checkers over all v2 cells, each behind the same integrity
    precheck (A4). Returns per-cell decisions and per-checker false
    accepts / false rejects by scope and family."""
    cells = v2_cells()
    out_cells: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for name in CHECKERS:
        summary[name] = {"false_accepts": 0, "false_rejects": 0,
                         "by_scope": {}, "by_family": {},
                         "fa_cells": [], "fr_cells": []}
    for i, cell in enumerate(cells):
        c, v = cell["case"], cell["view"]
        rec: dict[str, Any] = {
            "id": f"{cell['scope']}/{c.claim_kind}/{c.fault}/{i}",
            "scope": cell["scope"], "family": cell["family"],
            "claim": c.claim_kind, "fault": c.fault,
            "expectation": c.expectation, "note": c.note, "decisions": {}}
        intact = _integrity_ok(v)
        for name, fn in CHECKERS.items():
            certified, verdict = (fn(v) if intact
                                  else (False, "REJECTED_INTEGRITY"))
            fa = certified and c.expectation == "must_not_certify"
            fr = (not certified) and c.expectation == "must_certify"
            rec["decisions"][name] = {"certified": certified,
                                      "verdict": verdict,
                                      "ok": not (fa or fr)}
            s = summary[name]
            for key, bucket in (("by_scope", cell["scope"]),
                                ("by_family", cell["family"])):
                b = s[key].setdefault(bucket, {"n": 0, "false_accepts": 0,
                                               "false_rejects": 0})
                b["n"] += 1
                b["false_accepts"] += fa
                b["false_rejects"] += fr
            s["false_accepts"] += fa
            s["false_rejects"] += fr
            if fa:
                s["fa_cells"].append(rec["id"])
            if fr:
                s["fr_cells"].append(rec["id"])
        out_cells.append(rec)
    return {"cells": out_cells, "summary": summary}
