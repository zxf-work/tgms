"""The typed claim language v0 — the initial verified fragment.

{membership, scalar, exact count, complete set, existence, nonexistence}
plus the historical-basis obligation, which is not an eighth type but a
property every claim can carry: claims carry scope (Gate A constraint 3),
so each claim optionally names the pinned transaction-time basis it is
about (`basis_tt`); a claim with `basis_tt=None` is about whatever
well-identified basis its cited evidence executed under.

Deferred (build only after these survive M4 fault injection): top-k,
extremal, approximation, temporal-pattern claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Claim:
    basis_tt: int | None = None  # pinned tt-basis the claim is about, if any

    kind = "abstract"


@dataclass
class Membership(Claim):
    """`value` occurs in the cited result (witness obligation)."""
    value: Any = None
    field: str | None = None  # match on this row field; None = whole row / any field

    kind = "membership"


@dataclass
class Scalar(Claim):
    """The cited result establishes `value` at `path` (dot/[i] path)."""
    path: str = ""
    value: Any = None

    kind = "scalar"


@dataclass
class ExactCount(Claim):
    """The complete logical result over the cited domain has exactly n rows."""
    n: int = 0

    kind = "exact_count"


@dataclass
class CompleteSet(Claim):
    """`members` is exactly the result set (support + completeness)."""
    members: list[Any] | None = None
    field: str | None = None

    kind = "complete_set"


@dataclass
class Existence(Claim):
    """At least one row satisfies the cited query (one witness)."""

    kind = "existence"


@dataclass
class Nonexistence(Claim):
    """No row satisfies the cited query (requires completeness or a
    zero-cardinality certificate over the domain)."""

    kind = "nonexistence"
