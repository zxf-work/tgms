"""The generic claim verifier — capabilities in, typed verdicts out.

Backend-neutral by construction: this module imports only the ECQR
descriptor and the claim language, never a backend (tested — the M3 exit
gate is "no backend-specific branches in verifier core"). It answers one
question: does the cited evidence discharge the proof obligation of this
claim? The result value travels beside the descriptor (bound to it by
`result_id` under trust assumption A4); the verifier reads values from it
but takes every *condition* from the descriptor.

Verdict semantics follow EVIDENCE_MODEL.md §3. The two directions of the
cardinality rule (Gate A constraint 1) are both here: a certificate
survives incomplete delivery, and no certificate is conjured from a
complete-looking page unless delivery AND execution are complete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tgms.evidence.claims import (
    Claim,
    CompleteSet,
    ExactCount,
    Existence,
    Membership,
    Nonexistence,
    Scalar,
    TopK,
)
from tgms.evidence.ecqr import ECQR, basis_identity


class Verdict(Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED = "UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED"
    UNSUPPORTED_BASIS_MISMATCH = "UNSUPPORTED_BASIS_MISMATCH"
    UNSUPPORTED_MISSING_CERTIFICATE = "UNSUPPORTED_MISSING_CERTIFICATE"
    UNSUPPORTED_VALUE_MISMATCH = "UNSUPPORTED_VALUE_MISMATCH"
    UNSUPPORTED_NO_WITNESS = "UNSUPPORTED_NO_WITNESS"
    OUTSIDE_VERIFIED_FRAGMENT = "OUTSIDE_VERIFIED_FRAGMENT"
    # top-k obligations (the ordered claim form)
    UNSUPPORTED_ORDER_NOT_TOTAL = "UNSUPPORTED_ORDER_NOT_TOTAL"
    UNSUPPORTED_ORDER_MISMATCH = "UNSUPPORTED_ORDER_MISMATCH"
    UNSUPPORTED_EXECUTION_NOT_CERTIFIED = "UNSUPPORTED_EXECUTION_NOT_CERTIFIED"
    UNSUPPORTED_K_MISMATCH = "UNSUPPORTED_K_MISMATCH"


@dataclass
class Judgment:
    verdict: Verdict
    reason: str
    #: which support route certified (top-k: "total_order" or
    #: "boundary_strict"); None for single-route claim forms
    route: str | None = None


def _rows(result: Any) -> list[Any]:
    if isinstance(result, dict):
        rows = result.get("rows")
        return rows if isinstance(rows, list) else []
    return result if isinstance(result, list) else []


def _row_matches(row: Any, value: Any, fld: str | None) -> bool:
    if fld is not None:
        return isinstance(row, dict) and row.get(fld) == value
    if isinstance(row, dict):
        return value in row.values()
    if isinstance(row, (list, tuple)):
        # positional rows (SQL tuples): a witness is a matching cell
        return value in row
    return row == value


def _resolve_path(result: Any, path: str) -> tuple[bool, Any]:
    cur = result
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return False, None
            cur = cur[part]
    return True, cur


def _check_basis(claim: Claim, e: ECQR) -> Judgment | None:
    if claim.basis_tt is None:
        return None
    if not e.basis.pinned or e.basis.as_of_tt != claim.basis_tt:
        return Judgment(
            Verdict.UNSUPPORTED_BASIS_MISMATCH,
            f"claim is about pinned basis tt={claim.basis_tt}; evidence "
            f"basis is {'pinned ' if e.basis.pinned else 'unpinned '}"
            f"tt={e.basis.as_of_tt}")
    return None


def verify(claim: Claim, evidence: ECQR, result: Any = None) -> Judgment:
    """Does `evidence` (with its bound `result` value) support `claim`?"""
    bad_basis = _check_basis(claim, evidence)
    if bad_basis is not None:
        return bad_basis
    s = evidence.scope

    if isinstance(claim, Membership):
        if any(_row_matches(r, claim.value, claim.field)
               for r in _rows(result)):
            # a witness in the delivered page supports membership even when
            # delivery is incomplete — the witness rule
            return Judgment(Verdict.SUPPORTED, "witness in cited result")
        return Judgment(Verdict.UNSUPPORTED_NO_WITNESS,
                        "value not in cited result")

    if isinstance(claim, Scalar):
        ok, got = _resolve_path(result, claim.path)
        if not ok:
            return Judgment(Verdict.UNSUPPORTED_NO_WITNESS,
                            f"path {claim.path!r} not in cited result")
        if got == claim.value:
            return Judgment(Verdict.SUPPORTED, "cited value matches")
        return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                        f"cited value is {got!r}, claim says {claim.value!r}")

    if isinstance(claim, ExactCount):
        # the certificate path itself requires a completed execution: a
        # rows-so-far counter from an interrupted computation must never
        # certify (defense in depth beside the adapters' issuance rule —
        # the M4 matrix found exactly this hole on its first run, D-104)
        if s.exact_cardinality is not None and s.execution_complete:
            if s.exact_cardinality == claim.n:
                return Judgment(Verdict.SUPPORTED,
                                "certified cardinality matches")
            return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                            f"certified cardinality is {s.exact_cardinality}")
        if s.delivery_complete and s.execution_complete:
            n = len(_rows(result))
            if n == claim.n:
                return Judgment(Verdict.SUPPORTED,
                                "complete delivery; count equals page")
            return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                            f"complete result has {n} rows")
        return Judgment(Verdict.UNSUPPORTED_MISSING_CERTIFICATE,
                        "no cardinality certificate and delivery/execution "
                        "incomplete — a page count would be a wrong number")

    if isinstance(claim, CompleteSet):
        if not (s.delivery_complete and s.execution_complete):
            return Judgment(Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED,
                            "complete-set claims need delivery and "
                            "execution completeness over the cited domain")
        want = {v if not isinstance(v, dict) else str(v)
                for v in (claim.members or [])}
        got_vals = []
        for r in _rows(result):
            if claim.field is not None and isinstance(r, dict):
                got_vals.append(r.get(claim.field))
            else:
                got_vals.append(r if not isinstance(r, dict) else str(r))
        if want == set(got_vals):
            return Judgment(Verdict.SUPPORTED, "set equals complete result")
        return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                        "claimed set differs from complete result")

    if isinstance(claim, Existence):
        if _rows(result):
            return Judgment(Verdict.SUPPORTED, "witness row exists")
        if s.delivery_complete and s.execution_complete:
            return Judgment(Verdict.UNSUPPORTED_NO_WITNESS,
                            "complete result is empty")
        return Judgment(Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED,
                        "empty page of an incomplete result proves nothing")

    if isinstance(claim, Nonexistence):
        if not s.execution_complete:
            return Judgment(Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED,
                            "nonexistence needs a completed execution")
        if s.exact_cardinality == 0:
            return Judgment(Verdict.SUPPORTED, "certified zero cardinality")
        if s.delivery_complete and not _rows(result):
            return Judgment(Verdict.SUPPORTED,
                            "complete delivery contains no rows")
        if _rows(result):
            return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                            "cited result contains rows")
        return Judgment(Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED,
                        "incomplete delivery cannot prove absence")

    if isinstance(claim, TopK):
        return _verify_topk(claim, evidence, result)

    return Judgment(Verdict.OUTSIDE_VERIFIED_FRAGMENT,
                    f"claim kind {getattr(claim, 'kind', '?')!r} is outside "
                    f"the verified fragment")


def _seq(rows: Any) -> list[Any]:
    """Positional rows as lists, so a tuple-valued claim row equals the
    list-valued delivered row it names."""
    out = []
    for r in rows or []:
        out.append(list(r) if isinstance(r, tuple) else r)
    return out


def _row_key(r: Any) -> str:
    return json.dumps(r, sort_keys=True, default=str)


def _verify_topk(claim: TopK, e: ECQR, result: Any) -> Judgment:
    """TopK(S, key, k, dir[, as_set]) over the candidate domain Q'.

    Two support routes. (i) total_order: the recorded order is a total
    order, so the delivered prefix of length k IS the first k rows, as a
    sequence (and hence as a set). (ii) boundary_strict: the order is not
    total, but the adapter established on the descriptor's own basis that
    rank k sorts strictly before rank k+1 (or fewer than k+1 candidates
    exist), so the first k rows are unique as a SET; only a set claim
    (`as_set`) can use it.

    Checked in this order, first failure wins: an order with at least one
    route (ORDER_NOT_TOTAL), the claim names that order (ORDER_MISMATCH),
    the route serves the claim (ORDER_NOT_TOTAL for a sequence claim on
    route (ii); BASIS_MISMATCH when the boundary check ran on another
    basis), certified execution over Q' (EXECUTION_NOT_CERTIFIED), k
    equals the recorded limit and the page holds at most k rows
    (K_MISMATCH), S is the delivered sequence, or set under `as_set`
    (VALUE_MISMATCH), and a page shorter than k is certified complete
    (COMPLETENESS_NOT_CERTIFIED). A full page of k rows needs no
    delivery certificate.
    """
    s, rk = e.scope, e.ranking
    if (rk is None or not rk.order
            or not (rk.order_total or rk.boundary_strict)):
        why = ("descriptor records no ranking" if rk is None
               else "no ORDER BY: the page is an arbitrary prefix"
               if not rk.order
               else "recorded order is not established as a total order "
                    "(no unique key in the sort list) and rank k is not "
                    "established strictly before rank k+1")
        return Judgment(Verdict.UNSUPPORTED_ORDER_NOT_TOTAL, why)
    try:
        want = claim.order()
    except ValueError as err:
        return Judgment(Verdict.UNSUPPORTED_ORDER_MISMATCH,
                        f"claim order malformed: {err}")
    if want != rk.order:
        return Judgment(Verdict.UNSUPPORTED_ORDER_MISMATCH,
                        f"claim orders by {want}, evidence ranked by "
                        f"{rk.order}")
    if rk.order_total:
        route = "total_order"
    else:
        if not claim.as_set:
            return Judgment(Verdict.UNSUPPORTED_ORDER_NOT_TOTAL,
                            "order is not total: a strict rank boundary "
                            "certifies the top-k set, not its sequence")
        if rk.boundary_basis != basis_identity(e.basis):
            return Judgment(Verdict.UNSUPPORTED_BASIS_MISMATCH,
                            "rank-boundary check ran on a different basis "
                            "than the ranked page")
        route = "boundary_strict"
    if not s.execution_complete:
        return Judgment(Verdict.UNSUPPORTED_EXECUTION_NOT_CERTIFIED,
                        "top-k needs a certified execution over the whole "
                        "candidate domain")
    rows = _seq(_rows(result))
    if (not isinstance(claim.k, int) or isinstance(claim.k, bool)
            or claim.k < 1 or claim.k != rk.limit):
        return Judgment(Verdict.UNSUPPORTED_K_MISMATCH,
                        f"claim k={claim.k!r}, evidence limit is {rk.limit}")
    if len(rows) > rk.limit:
        return Judgment(Verdict.UNSUPPORTED_K_MISMATCH,
                        f"page holds {len(rows)} rows, more than its limit "
                        f"{rk.limit}")
    if claim.as_set:
        same = ({_row_key(r) for r in _seq(claim.rows)}
                == {_row_key(r) for r in rows})
    else:
        same = _seq(claim.rows) == rows
    if not same:
        return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                        "claimed rows differ from the delivered "
                        + ("set" if claim.as_set else "sequence"))
    what = "set" if claim.as_set else "prefix"
    if len(rows) == claim.k:
        return Judgment(Verdict.SUPPORTED,
                        f"delivered {what} of length k ({route})", route)
    if s.delivery_complete:
        return Judgment(Verdict.SUPPORTED,
                        f"fewer than k candidates ({len(rows)}) and "
                        f"delivery certified complete ({route})", route)
    return Judgment(Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED,
                    f"page of {len(rows)} < k rows without certified "
                    f"delivery: omitted candidates may rank in the top k")
