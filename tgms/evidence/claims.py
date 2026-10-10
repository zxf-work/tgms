"""The typed claim language v0 — the initial verified fragment.

{membership, scalar, exact count, complete set, existence, nonexistence}
plus the historical-basis obligation, which is not an eighth type but a
property every claim can carry: claims carry scope (Gate A constraint 3),
so each claim optionally names the pinned transaction-time basis it is
about (`basis_tt`); a claim with `basis_tt=None` is about whatever
well-identified basis its cited evidence executed under.

TopK is the first ordered claim form: it reads the cited result as a
sequence, where the six base forms read it as a set or a length.

Deferred: extremal, approximation, temporal-pattern claims.
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


def normalize_order(key: Any, direction: Any = "asc") -> list[list[str]]:
    """Canonical ``[[key, "asc"|"desc"], ...]`` form of an ordering.

    `key` is one key or a list of keys; `direction` is one direction for
    every key or a list aligned with `key`. Key text is compared verbatim
    (adapters emit canonical key text), directions case-insensitively.
    """
    keys = [key] if isinstance(key, str) else list(key or [])
    if isinstance(direction, str):
        dirs = [direction] * len(keys)
    else:
        dirs = list(direction or [])
    if len(dirs) != len(keys):
        raise ValueError("order keys and directions differ in length")
    out = []
    for k, d in zip(keys, dirs):
        d = str(d).lower()
        if d not in ("asc", "desc"):
            raise ValueError(f"order direction {d!r} is not asc or desc")
        out.append([str(k), d])
    return out


@dataclass
class TopK(Claim):
    """`rows` are, in sequence, the first `k` rows of the complete
    candidate result R*(Q', B) under the total order (`key`, `dir`),
    where Q' is the cited candidate domain (the query without its outer
    LIMIT). When fewer than k candidates exist, `rows` is all of them.

    `key`/`dir` name the WHOLE ordering, tie-break keys included; the
    claim is about that order, not about a coarser key prefix.

    With `as_set=True` the claim asserts only the SET of those rows
    (duplicate-insensitive, order among them not asserted): the first k
    rows as a set, which is well defined whenever the row at rank k
    sorts strictly before the row at rank k+1, even if the order is not
    total.
    """
    rows: list[Any] | None = None
    key: Any = None            # str or list[str]
    k: int = 0
    dir: Any = "asc"           # str or list[str], aligned with key
    as_set: bool = False

    kind = "top_k"

    def order(self) -> list[list[str]]:
        return normalize_order(self.key, self.dir)
