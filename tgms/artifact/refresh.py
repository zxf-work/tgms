"""P2.1's refresh executor — not yet implemented (M5 design memo §2.2, §8 P1.2-g).

This file exists now so the lane boundary is real from day one. Refresh
**must** open a store and run a kernel — the opposite of every other module
in this package, which reads only a `DependencyScope` and an `EventLog`
(D13.20's boundary, `scripts/check_freshness_boundary.py`). Putting refresh
here, deliberately outside the guarded allowlist (§7.1), is what lets the
boundary be drawn *through* `tgms/artifact/` rather than around it: the four
checking modules keep the "runs against a log it did not produce" property,
and this one — which cannot have that property, because refreshing means
recomputing — visibly does not claim it.

`RefreshHandle` (`tgms.artifact.witness`) is built and returned by
`check_artifact`; it is never executed by anything in this package. P2.1
wires this module in as the thing that actually reads a `RefreshHandle` and
acts on it.
"""

from __future__ import annotations

from typing import Any


def refresh(*args: Any, **kwargs: Any) -> Any:
    """Execute a `RefreshHandle` — recompute the plan (or opaque-leaf call)
    it names and register the resulting generation. Not implemented before
    P2.1."""
    raise NotImplementedError("P2.1")


__all__ = ["refresh"]
