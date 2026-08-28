"""The artifact registry — M5 design memo (`docs/design/M5_DESIGN.md`).

Public API re-export only, over its guarded siblings (§2.2's boundary table
— `record.py`, `registry.py`, `lookup.py`, `witness.py`; **not**
`refresh.py`, which deliberately does not join
`scripts/check_freshness_boundary.py`'s allowlist and is imported directly
as `tgms.artifact.refresh` by whatever needs it).
"""

from __future__ import annotations

from tgms.artifact.lookup import LookupResult, affected, affected_over
from tgms.artifact.record import (
    REFRESH_KINDS, SCHEMA_NAME, SCHEMA_VERSION, ArtifactId, ArtifactRecord, StepDependency,
)
from tgms.artifact.registry import Registry
from tgms.artifact.witness import (
    ArtifactVerdict, ExemptReceipt, RefreshHandle, TermRef, check_artifact, render_verdict,
)

__all__ = [
    "REFRESH_KINDS", "SCHEMA_NAME", "SCHEMA_VERSION", "ArtifactId", "ArtifactRecord",
    "ArtifactVerdict", "ExemptReceipt", "LookupResult", "RefreshHandle", "Registry",
    "StepDependency", "TermRef", "affected", "affected_over", "check_artifact",
    "render_verdict",
]
