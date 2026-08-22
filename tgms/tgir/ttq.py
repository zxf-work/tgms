"""`tt_q` — the belief-time frontier a read was actually served from
(TGIR_SPEC §5.6; FRESHNESS_SEMANTICS D13.16–D13.19), and the read basis the
`DependencyScope` on every envelope is stamped with.

`tt_q` relates to the execution basis but **is not** it: `as_of_tt` is what was
*asked for*, `tt_q` is what was *served*. Two rules govern every line here:

- **D13.17 — round down, never up.** `tt_q` must be a *lower* bound on the
  frontier. A `tt_q` above what the read actually saw makes `check` skip the
  suffix that would have invalidated the answer, which is false freshness — the
  one class of error this contract exists to forbid. Everything below that
  cannot be established exactly is therefore rounded *down*, or marked.
- **D1.9a / D13.16 — `pinned` describes the basis requested.** A pin *above*
  the frontier is not a pin: it is clamped, and reports `pinned = false,
  clamped = true`. §3.6's own second non-claim says the same thing from the
  stability side — writes landing between the frontier and an above-frontier
  `T_b` legitimately change the result.

The frontier itself comes from the adapter's **applied** frontier
(`StorageAdapter.frontier_tt`), never from `Store.clock.last_tt`: the log is
fsynced *before* the batch is applied, so the clock over-reports what a reader
is being served — exactly the direction D13.17 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from tgms.core.model import OPEN_END
from tgms.tgir.depscope import (
    FULL_SCAN_CHECKPOINTS, TOP_TERM, UNANCHORED, Checkpoint, DependencyScope,
)
from tgms.tgir.node import EMPTY_SCOPE_OPS
from tgms.tgir.scope_of import ScopeBasis


@dataclass(frozen=True, slots=True)
class Frontier:
    """What the store can say about its own applied transaction-time frontier.

    `tt is None` means *unavailable* — the fourth row of §6.2's clamp table.
    `verified` is false when the value could not be established against the
    applied prefix: a **legacy cursorless store**, where the backend keeps no
    event cursor and the log's tail is therefore an upper bound on what was
    applied rather than a statement about it.
    """

    tt: int | None
    verified: bool = True


@dataclass(frozen=True, slots=True)
class TtQ:
    """The `(tt_q, pinned, clamped)` triple, plus the verification flag."""

    tt_q: int
    pinned: bool = False
    clamped: bool = False
    verified: bool = True

    def to_envelope(self) -> dict[str, Any]:
        """The flat envelope keys (D13.16's placement). `verified` is **not**
        flat — a fifth flat key would slip past every comparator's exclusion
        tuple; it rides inside the `DependencyScope` instead."""
        return {"tt_q": self.tt_q, "pinned": self.pinned, "clamped": self.clamped}


def clamp(as_of_tt: int, frontier: Frontier) -> TtQ:
    """§6.2's four cases, which are D13.16's clamp and D1.9a's `pinned` rule.

    | `as_of_tt` | `tt_q` | `pinned` | `clamped` |
    |---|---|---|---|
    | `OPEN_END` (the default) | frontier | false | false |
    | `<= frontier` | `as_of_tt` | **true** | false |
    | `> frontier`, `< OPEN_END` | frontier | **false** | **true** |
    | frontier unavailable | `0` | false | **true** |

    The third row is the one an implementer gets wrong: an above-frontier pin
    is `pinned = false`, because `pinned` describes the basis *requested* and
    that basis was not served. The fourth is maximally conservative and
    therefore always sound.
    """
    if frontier.tt is None:
        return TtQ(0, False, True, frontier.verified)
    if as_of_tt >= OPEN_END:
        return TtQ(frontier.tt, False, False, frontier.verified)
    if as_of_tt <= frontier.tt:
        return TtQ(as_of_tt, True, False, frontier.verified)
    return TtQ(frontier.tt, False, True, frontier.verified)


def frontier_of(adapter: Any, tt_source: Any = None) -> Frontier:
    """The frontier for this read.

    `tt_source` is the authoritative override — a `Store`, or any object with
    `frontier_tt()` (and optionally `frontier_verified`), or a plain callable
    returning an int. With none, the adapter's own applied frontier is used,
    which every backend maintains through the shared `apply_ops`.
    """
    if tt_source is not None:
        verified = bool(getattr(tt_source, "frontier_verified", True))
        getter = (tt_source if callable(tt_source)
                  else getattr(tt_source, "frontier_tt", None))
        if getter is not None:
            tt = getter()
            return Frontier(None if tt is None else int(tt), verified)
    getter = getattr(adapter, "frontier_tt", None)
    if getter is None:
        # An adapter predating the ABC method. Unavailable, not zero-by-luck.
        return Frontier(None, True)
    tt = getter()
    return Frontier(None if tt is None else int(tt), True)


def checkpoints_of(adapter: Any, tt_source: Any = None) -> tuple[Checkpoint, ...]:
    """D13.18's `checkpoints`, with D13.8a's sanctioned fallback.

    `event_cursor()` exists on `NativeAdapter` only; it is not on the ABC, and
    neither DuckDB nor Kuzu has one (§11.2's ruling). Their scopes therefore
    carry `[[0, SEED_CHAIN]]` — a full-log scan, "widening and therefore sound.
    Slow, never wrong." The same fallback covers a legacy store, whose cursor
    reports chain `""` and means *no cursor*, never *nothing applied*.
    """
    for holder in (tt_source, adapter):
        cursor = getattr(holder, "event_cursor", None) if holder is not None else None
        if cursor is None:
            continue
        try:
            offset, chain = cursor()
        except Exception:  # pragma: no cover - a backend that cannot answer
            continue
        if chain:
            return (Checkpoint(int(offset), chain),)
    return FULL_SCAN_CHECKPOINTS


def store_identity_of(adapter: Any, tt_source: Any = None) -> str:
    """The `store` field of D13.2. `UNANCHORED` for an adapter-only read — the
    oracle-family stores (`DuckDBAdapter(":memory:")`) have no event log at
    all, and no identity can be invented for them."""
    for holder in (tt_source, adapter):
        ident = getattr(holder, "store_identity", None) if holder is not None else None
        if isinstance(ident, str) and ident:
            return ident
    return UNANCHORED


def basis_of(adapter: Any, as_of_tt: int, tt_source: Any = None) -> ScopeBasis:
    """Assemble the read basis: store identity, the clamped `tt_q` triple and
    the log cursor. Capture happens **before** the read is issued, which is how
    D13.17's *round down* is satisfied by construction."""
    triple = clamp(as_of_tt, frontier_of(adapter, tt_source))
    return ScopeBasis(
        store=store_identity_of(adapter, tt_source),
        tt_q=triple.tt_q, pinned=triple.pinned, clamped=triple.clamped,
        checkpoints=checkpoints_of(adapter, tt_source),
        tt_q_verified=triple.verified,
    )


def dependency_of(op: str, basis: ScopeBasis, args: dict[str, Any] | None = None,
                  sigma: Any = None) -> DependencyScope:
    """The scope for one call: `leaves.terms_for` where a derivation exists,
    the coarse all-`"*"` term where it does not.

    `"*"` stays explicitly legal for any operator whose derivation is not yet
    written (§5.5.4 constraint 1), and D13.1 makes every later narrowing a
    strict improvement rather than a compatibility event.

    `compute` is the exception **from day one**: `terms: []`, the empty scope
    ∅, "the correct, non-degenerate value for a `compute` node over literal
    inputs" (D13.2, §6 #15).
    """
    from tgms.tgir.leaf import sigma_for
    from tgms.tgir.leaves import terms_for

    if op in EMPTY_SCOPE_OPS:
        return basis.empty_scope()
    if args is None:
        return basis.scope(TOP_TERM)
    return basis.scope(*terms_for(op, args, sigma or sigma_for(op, args)))


def envelope_metadata(adapter: Any, op: str, args: dict[str, Any] | None = None,
                      tt_source: Any = None) -> dict[str, Any]:
    """The four flat envelope keys plus the dependency object, for one call.

    Every one of them lands on the **envelope**, never in the kernel's
    `payload`, so digest exclusion is structural rather than a denylist
    (§5.4's ruling; the M2 plan's §6.3).

    `args` are the *filled* arguments the leaf was built from; with none — a
    step that never ran, and so has none — the scope falls back to `"*"`, which
    is the widening a failed step's mandatory contribution should be.
    """
    basis = basis_of(adapter, as_of_tt_of(args or {}), tt_source)
    triple = TtQ(basis.tt_q, basis.pinned, basis.clamped, basis.tt_q_verified)
    return {**triple.to_envelope(),
            "dependency": dependency_of(op, basis, args).to_json()}


def as_of_tt_of(args: dict[str, Any]) -> int:
    """The requested belief basis of a filled argument set. Fourteen of the
    fifteen operators take `as_of_tt`; `compute` takes none and is unpinned."""
    value = args.get("as_of_tt", OPEN_END)
    return OPEN_END if value is None else int(value)


#: A `tt_source` is anything `frontier_of` accepts; named for the signatures
#: that pass one through.
TtSource = Any
FrontierFn = Callable[[], int]

__all__ = [
    "Frontier", "FrontierFn", "TtQ", "TtSource", "as_of_tt_of", "basis_of",
    "checkpoints_of", "clamp", "dependency_of", "envelope_metadata",
    "frontier_of", "store_identity_of",
]
