"""`Join{inner}` — §2.8. Hash build on the right, probe in left order.

M3.0 implements the inner join only. `left_outer` and `anti` derive rows from
*absence* on the right and therefore **refuse `E_INCOMPLETE` unless the right
input is execution-complete** (§2.8) — a precondition that needs §3.9's
completeness machinery, which is M3.2's. An anti-join against a truncated probe
reports false absences: not merely uncertified rows, but wrong ones.

**Canonical order: left row position, then right row position** (§2.8). Probing
the left in order and emitting each match in the build side's stored order gives
exactly that, with no sort — which is why `Join` is not one of §3.4's four
sorting node kinds.

**Bag semantics: multiplicities multiply.** A left row matching three right rows
yields three output rows. That is not an accident of the algorithm; §8.4's
CLOSED ruling makes duplicate probe keys load-bearing for bo31 and BI18 on any
corrected store, where several believed versions per identity are the normal
case rather than the exception.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from tgms.core.errors import InternalError
from tgms.tgir.eval.select import check_join_keys
from tgms.tgir.node import Join
from tgms.tgir.relation import Relation


def eval_join(node: Join, left: Relation, right: Relation) -> Relation:
    if node.join_type != "inner":
        raise NotImplementedError(
            f"Join{{{node.join_type}}} derives rows from absence on the right and "
            f"must refuse E_INCOMPLETE unless the probe is execution-complete "
            f"(§2.8); that precondition is M3.2's")

    left_keys = tuple(pair[0] for pair in node.on)
    right_keys = tuple(pair[1] for pair in node.on)
    check_join_keys(left, left_keys, "left")
    check_join_keys(right, right_keys, "right")

    index: dict[Any, list[int]] = defaultdict(list)
    for i, key in enumerate(_keys(right, right_keys)):
        index[key].append(i)

    left_idx: list[int] = []
    right_idx: list[int] = []
    for i, key in enumerate(_keys(left, left_keys)):
        matches = index.get(key)
        if not matches:
            continue
        left_idx.extend([i] * len(matches))
        right_idx.extend(matches)

    taken_left = left.take(np.array(left_idx, dtype=np.int64))
    taken_right = right.take(np.array(right_idx, dtype=np.int64))
    out = taken_left.with_columns(taken_right.schema, taken_right.cols,
                                  taken_right.nulls)
    if out.schema.names != node.out_schema.names:  # pragma: no cover - guard
        raise InternalError("join output schema drifted from the plan's")
    return out


def _keys(rel: Relation, names: tuple[str, ...]) -> list[Any]:
    """Hashable join keys, one per row.

    A single key column hashes as its own value; several hash as a tuple, which
    is also how a `tuple(…)` key built in `Project` already stores itself. numpy
    scalars are converted, so an `int64` key and a Python `int` key from two
    different upstream shapes land in the same hash bucket rather than in two.
    """
    columns = [rel.column(name) for name in names]
    if len(columns) == 1:
        return [_hashable(v) for v in columns[0]]
    return [tuple(_hashable(col[i]) for col in columns) for i in range(rel.n)]


def _hashable(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):  # pragma: no cover - defensive
        return tuple(value.tolist())
    return value


__all__ = ["eval_join"]
