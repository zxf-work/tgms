"""Compiled core expansions for the R7 leaves (§6, M3.3).

Each module here builds one or more TGIR `Plan`s and assembles the operator's
payload in Python — which is exactly what §6's "several roots plus a
result-assembly step" describes. The assembly is **code, not an algebra node**:
adding a node kind that is not in §2 would be adding a primitive.

**M3.3 was cut to two operators by coordinator ruling.** `entity_history` and
`version_history` compile here; `snapshot_subgraph`, `diff_snapshots` and the
`aggregate_events` fragment stay opaque leaves permanently — see
`tgms/tgir/rollout.py` for the decision and its reason. §6's safety valve
(§8.12) blesses exactly this outcome: M3 exits green with every operator on
`leaf`, and a compiled path is a proof that the core is expressive enough, not
a shipping route.
"""

from __future__ import annotations

from typing import Any, Callable

from tgms.tgir.compiled import entity_history as _entity_history
from tgms.tgir.compiled import version_history as _version_history

#: `op → compiled(adapter, filled_args) -> payload`. An operator absent here
#: has no compiled form and can never leave `leaf`.
COMPILED: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "entity_history": _entity_history.run,
    "version_history": _version_history.run,
}

__all__ = ["COMPILED"]
