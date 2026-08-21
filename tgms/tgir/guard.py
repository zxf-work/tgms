"""The `∅`-kernel guard (TGIR_SPEC §2.0 obligation 6; FRESHNESS_SEMANTICS D13.11).

`∅` means *this node is a pure function of its input relations and its bind-time
arguments* — exactly the class §3.6's pinned-stability induction relies on. An
`∅`-classified kernel that later starts reading store state becomes **silently
unsound**, so the spec makes the classification a *checkable* property:

> an `∅`-classified kernel must not receive a live storage adapter (it receives
> none, or a null one), so the misclassification fails loudly at the first read
> instead of rotting.

`NullAdapter` is that null one. It is M2.0 data-only: nothing here is wired into
`call_operator`, and no existing suite imports it. M2.2 is where an
`∅`-classified leaf is actually evaluated against it.
"""

from __future__ import annotations

from typing import Any

from tgms.core.errors import StateError
from tgms.tgir.node import EMPTY_SCOPE_OPS, Node


class NullAdapter:
    """Stands in for the storage adapter an `∅`-classified kernel must not get.

    Any attribute access raises `StateError`, so the first read fails by name
    and at the site rather than producing a quietly wrong answer. Of the
    fifteen, `compute` is the only `∅` leaf (§6 #15: "the kernel never touches
    the adapter"; its registry row carries `cost_fn=None` and takes no
    `as_of_tt`).
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise StateError(f"∅-classified kernel touched the storage adapter: {name}",
                         attribute=name)

    def __repr__(self) -> str:
        return "NullAdapter()"


def adapter_for(node: Node, adapter: Any) -> Any:
    """The adapter a node's evaluation may see. Returns `NullAdapter()` for a
    node classified `∅`, and `adapter` otherwise.

    A `Node` classified `∅` is one whose `reads_store` is false: the eight pure
    core operators (`Filter`, `PropertyPredicate`, `TypeConstraint`, `Project`,
    `Join`, `Aggregate`, `Order`, `Limit`) and any `OpaqueLeaf` whose operator
    is in `EMPTY_SCOPE_OPS`.
    """
    return adapter if node.reads_store else NullAdapter()


def is_empty_scope_op(op: str) -> bool:
    return op in EMPTY_SCOPE_OPS


__all__ = ["NullAdapter", "adapter_for", "is_empty_scope_op"]
