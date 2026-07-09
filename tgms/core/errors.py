"""Typed error taxonomy (spec §2.5).

Every error carries a stable machine-readable code and a JSON-serializable
payload; the planner repair loop consumes these payloads verbatim, so keep
`details` structured and actionable (estimates, narrowing suggestions).
"""

from __future__ import annotations

from typing import Any


class TgmsError(Exception):
    code = "E_INTERNAL"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class SchemaError(TgmsError):
    """Input or output failed JSON-Schema validation."""

    code = "E_SCHEMA"


class InvalidArgError(TgmsError):
    """Arguments are schema-valid but semantically ill-formed (t_a >= t_b, delta <= 0, ...)."""

    code = "E_INVALID_ARG"


class NotFoundError(TgmsError):
    """Referenced uid / eid / step output does not exist in the store or trace."""

    code = "E_NOT_FOUND"


class CostError(TgmsError):
    """Estimated cost exceeds ceilings. `details` must include the estimate and
    actionable narrowing suggestions (smaller window, add node_filter)."""

    code = "E_COST"


class LimitError(TgmsError):
    """Hard cap violated (hops, k, limit, plan steps, wall clock)."""

    code = "E_LIMIT"


class StateError(TgmsError):
    """Store invariant violated or operation illegal in current state
    (overlap conflicts, non-monotonic clock, replay divergence)."""

    code = "E_STATE"


class InternalError(TgmsError):
    code = "E_INTERNAL"
