"""Membership masks that do not degrade on object columns.

`np.isin` is the obvious way to write "which of these rows are in that set", and
on numeric arrays it is the right one: numpy sorts both sides and merges, so it
is `O((n + m) log(n + m))`. On **object** arrays it cannot sort, and falls back
to an elementwise comparison against every element of the needle — `O(n * m)`,
in Python-level object comparisons.

Every identity column this evaluator carries is an object array (uids, labels,
rel_types are strings), and the needle is frequently *not* small: `Expand`
resolves the version of every uid in its frontier, so `m` grows with the
expansion. Measured on a 2,997,352-row uid column:

| needle | `np.isin` | this helper |
|---|---|---|
| 1 | 0.023 s | 0.142 s |
| 100 | 1.94 s | 0.121 s |
| 2,000 | **39.0 s** | 0.126 s |
| 10,000 | **196.1 s** | 0.148 s |

That is the whole of a measured LDBC IC2 execution at SF1: 231.9 s of 263.4 s
(88%) inside `numpy._isin`, over six calls.

**The mask is bit-identical to `np.isin`'s**, because set membership and `==`
agree for every hashable value with consistent `__hash__`/`__eq__` — which is
what strings are. Anything unhashable falls back to `np.isin`, so the helper is
total over inputs `np.isin` accepts.
"""

from __future__ import annotations

import numpy as np

#: Below this, `np.isin`'s C loop beats a Python-level set membership: at a
#: needle of one it is ~6x faster. The crossover measured on a 3M-row object
#: column is between 1 and 100, and the cost of guessing wrong on the low side
#: is milliseconds, where guessing wrong on the high side is minutes.
SMALL_NEEDLE = 8


def mask_in(values: np.ndarray, wanted: np.ndarray) -> np.ndarray:
    """`np.isin(values, wanted)`, without the object-array blow-up.

    Numeric arrays go straight to `np.isin` — it is already the better
    algorithm there and this helper has nothing to add.
    """
    if values.dtype != object and wanted.dtype != object:
        return np.isin(values, wanted)
    if len(wanted) <= SMALL_NEEDLE or len(values) == 0:
        return np.isin(values, wanted)
    try:
        needle = set(wanted.tolist())
    except TypeError:  # pragma: no cover - unhashable needle
        return np.isin(values, wanted)
    try:
        return np.fromiter((v in needle for v in values.tolist()),
                           dtype=bool, count=len(values))
    except TypeError:  # pragma: no cover - unhashable values
        return np.isin(values, wanted)


__all__ = ["SMALL_NEEDLE", "mask_in"]
