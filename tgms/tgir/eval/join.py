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

from tgms.tgir.eval.select import check_join_keys
from tgms.tgir.node import Join
from tgms.tgir.relation import Relation


def eval_join(node: Join, left: Relation, right: Relation) -> Relation:
    """All three join types. The execution-completeness precondition for the two
    absence-deriving ones is enforced by `propagate.meta_for` **before** this
    runs, so a refusal costs no work and no partial relation exists."""
    if node.join_type == "left_outer":
        return _left_outer(node, left, right)
    if node.join_type == "anti":
        return _anti(node, left, right)

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
    # No assertion against `node.out_schema` here: that is the *declared*
    # schema, and column pruning (`tgms/tgir/prune.py`) legitimately realizes a
    # subset of it. An equality check fired on any pruned join — declared 49
    # columns, realized 11 — which made pruning look like a defect when it is
    # the design. The output's shape is guaranteed structurally instead: it is
    # the concatenation of the two inputs' realized schemas, and `concat`
    # already refuses a collision.
    return taken_left.with_columns(taken_right.schema, taken_right.cols,
                                   taken_right.nulls)


def _left_outer(node: Join, left: Relation, right: Relation) -> Relation:
    """`inner ∪ { l ⧺ null_R | l ∈ L, ¬∃ r ∈ R. key(r) = key(l) }`.

    **Duplicate probe keys multiply** (§8.4 CLOSED): a left row matching three
    probe rows yields three output rows. That differs from today's
    `compute join`, whose keys must be unique on both sides, and the divergence
    is deliberate — it is load-bearing for bo31 and BI18 on any *corrected*
    store, where several believed versions per identity is the normal case.
    Ruling "reject" instead would turn two `yes` rows into refusals.

    The fill side is `Relation.nullable_copy()` plus an all-null row — the same
    construction §9.1's version-less `into` uses, which is why both landed in
    the relation rather than in either caller.
    """
    left_keys = tuple(pair[0] for pair in node.on)
    right_keys = tuple(pair[1] for pair in node.on)
    check_join_keys(left, left_keys, "left")
    check_join_keys(right, right_keys, "right")

    index: dict[Any, list[int]] = defaultdict(list)
    for i, key in enumerate(_keys(right, right_keys)):
        index[key].append(i)

    left_idx: list[int] = []
    right_idx: list[int] = []
    unmatched: list[int] = []
    for i, key in enumerate(_keys(left, left_keys)):
        matches = index.get(key)
        if matches:
            left_idx.extend([i] * len(matches))
            right_idx.extend(matches)
        else:
            unmatched.append(i)

    nullable = right.nullable_copy()
    matched = left.take(np.array(left_idx, dtype=np.int64)).with_columns(
        nullable.schema, nullable.take(np.array(right_idx, dtype=np.int64)).cols,
        nullable.take(np.array(right_idx, dtype=np.int64)).nulls)
    if not unmatched:
        return matched
    fill_left = left.take(np.array(unmatched, dtype=np.int64))
    fill = Relation(nullable.schema,
                    {c.name: _null_column(nullable, c.name, len(unmatched))
                     for c in nullable.schema},
                    len(unmatched),
                    {c.name: np.ones(len(unmatched), dtype=bool)
                     for c in nullable.schema})
    filled = fill_left.with_columns(fill.schema, fill.cols, fill.nulls)
    # left row order is the contract, and the two halves were built by
    # partitioning it — so the concatenation is re-sorted back into it
    out = matched.concat_rows(filled)
    order = np.concatenate([np.array(left_idx, dtype=np.int64),
                            np.array(unmatched, dtype=np.int64)])
    return out.take(np.argsort(order, kind="stable"))


def _anti(node: Join, left: Relation, right: Relation) -> Relation:
    """`{ l | l ∈ L, ¬∃ r ∈ R. key(r) = key(l) }`.

    The right relation contributes **no columns** — it is a *probe*. Duplicate
    probe keys are accepted without a second thought: duplicates cannot change
    an absence test, so the result is unaffected (§8.4 CLOSED).
    """
    left_keys = tuple(pair[0] for pair in node.on)
    right_keys = tuple(pair[1] for pair in node.on)
    check_join_keys(left, left_keys, "left")
    check_join_keys(right, right_keys, "right")

    probe = set(_keys(right, right_keys))
    keep = np.array([key not in probe for key in _keys(left, left_keys)], dtype=bool)
    return left.filter(keep) if left.n else left


def _null_column(rel: Relation, name: str, n: int) -> np.ndarray:
    """A fill column of the right storage type: the values are unspecified
    under a null mask, so what matters is only that the dtype matches."""
    template = rel.cols[name]
    if template.dtype.kind in "iu":
        return np.zeros(n, dtype=template.dtype)
    if template.dtype.kind == "f":
        return np.zeros(n, dtype=template.dtype)
    if template.dtype == bool:
        return np.zeros(n, dtype=bool)
    return np.full(n, None, dtype=object)


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
