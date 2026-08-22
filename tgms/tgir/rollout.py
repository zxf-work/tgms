"""Rollout control for the TGIR path (M2 plan §3.1's rollback column, §8.3).

Two switches live here, and the difference between them is the point:

- **`TGIR_PLAN_PATH`** — M2.2's escape hatch. `off` restores the pre-M2.2
  direct call: `call_operator` invokes `REGISTRY[op].fn(adapter, args)` itself
  instead of building and evaluating a single-leaf plan. It is a *structural*
  rollback, not a semantic one — both paths call the same kernel with the same
  arguments and produce the same payload and the same `result_digest` — so
  flipping it can only ever remove the wrapping, never change an answer. The
  plan removes this flag once the phase has been green for one full CI cycle.

- **`COMPILE_MODE`** (M2.4, not yet populated) — which operators evaluate as a
  *compiled core expansion* rather than as an opaque leaf. That one **is**
  semantic, which is why §8.3 rules it must be a checked-in per-operator table
  rather than an environment variable: an env var is global, unversioned and
  unreviewable, and would let a stray shell export change what a caller gets
  back. The one env override §8.3 permits there may only widen toward
  `shadow`, never toward `compiled`.

The asymmetry is deliberate. `TGIR_PLAN_PATH` is allowed to be an env var
precisely because it cannot change an answer.
"""

from __future__ import annotations

import os
from typing import Literal

#: The environment variable that disables the plan path.
PLAN_PATH_ENV = "TGIR_PLAN_PATH"

#: Values that turn the wrapping off. Anything else — unset included — leaves
#: it on, so the escape hatch has to be taken deliberately.
_OFF = frozenset({"off", "0", "false", "no"})


def plan_path_enabled() -> bool:
    """True unless `TGIR_PLAN_PATH` names one of the off values.

    Read per call rather than at import: a rollback should not need a process
    restart, and the cost is one `os.environ` lookup against an operator call
    that touches a storage backend.
    """
    return os.environ.get(PLAN_PATH_ENV, "on").strip().lower() not in _OFF


Mode = Literal["leaf", "shadow", "compiled"]

#: M2.4's per-operator rollout table. Every operator is `leaf` in M2, which is
#: §8.12's ruling and a legitimate exit state: M2.4 exists to *prove* the core
#: end-to-end, not to ship it.
COMPILE_MODE: dict[str, Mode] = {}


def compile_mode(op: str) -> Mode:
    return COMPILE_MODE.get(op, "leaf")


__all__ = ["COMPILE_MODE", "Mode", "PLAN_PATH_ENV", "compile_mode", "plan_path_enabled"]
