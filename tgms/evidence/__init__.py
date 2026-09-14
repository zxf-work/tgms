"""Evidence-Carrying Query Results (ECQR) — reference implementation.

The backend-neutral core of the evidence semantics (EVIDENCE_MODEL.md,
Gate A): `ecqr` is the interchange descriptor, `claims` the typed claim
language, `verify` the generic claim verifier. Backend adapters — which
may use any implementation internals to *produce* capabilities — live
beside the core (`adapter_tgms`); the core never imports a backend.
"""

from tgms.evidence.claims import (
    Claim,
    CompleteSet,
    ExactCount,
    Existence,
    Membership,
    Nonexistence,
    Scalar,
)
from tgms.evidence.ecqr import ECQR, SCHEMA, VERSION
from tgms.evidence.verify import Verdict, verify

__all__ = ["ECQR", "SCHEMA", "VERSION", "Claim", "Membership", "Scalar",
           "ExactCount", "CompleteSet", "Existence", "Nonexistence",
           "Verdict", "verify"]
