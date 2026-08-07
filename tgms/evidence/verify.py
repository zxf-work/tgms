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
)
from tgms.evidence.ecqr import ECQR


class Verdict(Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_INCOMPLETE = "UNSUPPORTED_INCOMPLETE"
    UNSUPPORTED_BASIS_MISMATCH = "UNSUPPORTED_BASIS_MISMATCH"
    UNSUPPORTED_MISSING_CERTIFICATE = "UNSUPPORTED_MISSING_CERTIFICATE"
    UNSUPPORTED_VALUE_MISMATCH = "UNSUPPORTED_VALUE_MISMATCH"
    UNSUPPORTED_NO_WITNESS = "UNSUPPORTED_NO_WITNESS"
    OUTSIDE_VERIFIED_FRAGMENT = "OUTSIDE_VERIFIED_FRAGMENT"


@dataclass
class Judgment:
    verdict: Verdict
    reason: str


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
        if s.exact_cardinality is not None:
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
            return Judgment(Verdict.UNSUPPORTED_INCOMPLETE,
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
        return Judgment(Verdict.UNSUPPORTED_INCOMPLETE,
                        "empty page of an incomplete result proves nothing")

    if isinstance(claim, Nonexistence):
        if not s.execution_complete:
            return Judgment(Verdict.UNSUPPORTED_INCOMPLETE,
                            "nonexistence needs a completed execution")
        if s.exact_cardinality == 0:
            return Judgment(Verdict.SUPPORTED, "certified zero cardinality")
        if s.delivery_complete and not _rows(result):
            return Judgment(Verdict.SUPPORTED,
                            "complete delivery contains no rows")
        if _rows(result):
            return Judgment(Verdict.UNSUPPORTED_VALUE_MISMATCH,
                            "cited result contains rows")
        return Judgment(Verdict.UNSUPPORTED_INCOMPLETE,
                        "incomplete delivery cannot prove absence")

    return Judgment(Verdict.OUTSIDE_VERIFIED_FRAGMENT,
                    f"claim kind {getattr(claim, 'kind', '?')!r} is outside "
                    f"the verified fragment")
