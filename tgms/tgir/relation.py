"""The runtime relation: a columnar struct-of-arrays with parallel null masks.

`TGIR_SPEC.md` §4.1 defines a relation as `(schema, ordered bag of rows)`. This
is that, held column-wise:

```
Relation(schema, cols={name: np.ndarray}, nulls={name: bool mask}, n=rows)
```

Four properties the shape is chosen to have (M3 plan §3.1):

1. **It is what the adapter already hands over.** `edges_columnar` and
   `nodes_columnar` return `dict[str, np.ndarray]` — int64 for fixed-width,
   `dtype=object` for strings. A relation is that dict with a schema attached:
   no conversion layer, no per-row object.
2. **It is the idiom the fifteen kernels are written in.** The one documented
   row-loop exception (`resolve_entities`) is priced at 116× the columnar rate
   in the guardrail's own coefficient table, so a row-oriented evaluator would
   be a performance class this system has already measured and refused.
3. **Nulls are a first-class requirement.** `Join{left_outer}` fills every right
   column with null, `§2.7` has `is_null`/`coalesce`, and — the case that fixes
   the design rather than merely motivating it — an `Expand` may bind an `into`
   whose node has **no version visible under Σ** (§6 #3 rejects making node
   validity a global `Expand` rule). int64 has no null, so a parallel bool mask
   is the only honest encoding. Keeping it a *separate dict* rather than using
   masked arrays keeps the common no-nulls path allocation-free: an absent key
   means "this column has no nulls", which is the overwhelmingly common case.
4. **`props` is a parsed dict per row in an object array**, produced through
   `tgms/temporal/props.py` and consumed through its `matches` / `numeric_value`
   / `SKIP`. That module is the single source shared by the kernel, the oracle
   and the SQL twins, and D-052's type-fit rule is *defined* there.

**Values under a null are unspecified** — 0 for int columns, `None` for object
columns — and a reader that consults `cols` without `nulls` is reading a value
the relation does not claim. Every operation here carries the masks along.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from tgms.core.errors import InternalError
from tgms.tgir.types import Column, Schema, Tau

#: Columns whose runtime dtype is int64. Everything else is an object array:
#: identity strings, labels, parsed prop dicts, tuples built by `Project`.
INT_TAUS: frozenset[str] = frozenset({"int", "ts", "bool"})


def array_for(tau: Tau, values: Sequence[Any] | np.ndarray) -> np.ndarray:
    """Build the storage array for a column of type `tau`."""
    if isinstance(values, np.ndarray):
        return values
    if tau.base == "bool":
        return np.asarray(values, dtype=bool)
    if tau.base in ("int", "ts") and not tau.nullable:
        return np.asarray(values, dtype=np.int64)
    return np.asarray(values, dtype=object)


@dataclass(frozen=True, slots=True)
class Relation:
    """An ordered bag of rows, held column-wise."""

    schema: Schema
    cols: dict[str, np.ndarray]
    n: int
    nulls: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [c.name for c in self.schema if c.name not in self.cols]
        if missing:
            raise InternalError(f"relation is missing column(s): {missing}")
        for name, arr in self.cols.items():
            if len(arr) != self.n:
                raise InternalError(
                    f"column {name!r} has {len(arr)} rows, relation has {self.n}")
        for name, mask in self.nulls.items():
            if name not in self.cols:
                raise InternalError(f"null mask for unknown column {name!r}")
            if len(mask) != self.n:
                raise InternalError(f"null mask {name!r} has {len(mask)} rows")

    # -- constructors ----------------------------------------------------
    @staticmethod
    def of(schema: Schema, cols: Mapping[str, np.ndarray], n: int | None = None,
           nulls: Mapping[str, np.ndarray] | None = None) -> "Relation":
        if n is None:
            n = len(next(iter(cols.values()))) if cols else 0
        return Relation(schema, dict(cols), n,
                        {k: v for k, v in (nulls or {}).items() if v is not None})

    @staticmethod
    def empty(schema: Schema) -> "Relation":
        return Relation(schema,
                        {c.name: array_for(c.tau, []) for c in schema}, 0, {})

    # -- accessors -------------------------------------------------------
    def __len__(self) -> int:
        return self.n

    def column(self, name: str) -> np.ndarray:
        try:
            return self.cols[name]
        except KeyError:
            raise InternalError(
                f"column {name!r} was pruned or never materialized",
                available=sorted(self.cols)) from None

    def null_mask(self, name: str) -> np.ndarray | None:
        """The column's null mask, or None when it has no nulls at all."""
        return self.nulls.get(name)

    def is_null(self, name: str) -> np.ndarray:
        """A materialized mask, allocated only when asked for."""
        mask = self.nulls.get(name)
        return np.zeros(self.n, dtype=bool) if mask is None else mask

    def has_nulls(self, name: str) -> bool:
        mask = self.nulls.get(name)
        return mask is not None and bool(mask.any())

    # -- row operations, all order-preserving ----------------------------
    def take(self, idx: np.ndarray) -> "Relation":
        """Rows at `idx`, in `idx` order. The primitive under masking, sorting
        and joining alike — so all three carry null masks along by construction.
        """
        idx = np.asarray(idx)
        return Relation(self.schema,
                        {k: v[idx] for k, v in self.cols.items()},
                        int(idx.shape[0]),
                        {k: v[idx] for k, v in self.nulls.items()})

    def filter(self, mask: np.ndarray) -> "Relation":
        """Rows where `mask` is true, in input order — §2.4's "the input's,
        restricted", which boolean indexing gives for free."""
        return self.take(np.flatnonzero(np.asarray(mask, dtype=bool)))

    def select(self, names: Sequence[str]) -> "Relation":
        """A projection onto existing columns, preserving their declared types
        and every row."""
        keep = Schema(tuple(self.schema.column(n) for n in names))
        return Relation(keep, {n: self.cols[n] for n in names}, self.n,
                        {n: self.nulls[n] for n in names if n in self.nulls})

    def with_columns(self, added: Schema,
                     cols: Mapping[str, np.ndarray],
                     nulls: Mapping[str, np.ndarray] | None = None) -> "Relation":
        """This relation plus `added`. A name collision is a static plan error
        the node layer already rejected, so it is an internal error here."""
        return Relation(self.schema.concat(added),
                        {**self.cols, **dict(cols)}, self.n,
                        {**self.nulls,
                         **{k: v for k, v in (nulls or {}).items() if v is not None}})

    def rename(self, mapping: Mapping[str, str]) -> "Relation":
        def to(name: str) -> str:
            return mapping.get(name, name)

        return Relation(
            Schema(tuple(Column(to(c.name), c.tau) for c in self.schema)),
            {to(k): v for k, v in self.cols.items()}, self.n,
            {to(k): v for k, v in self.nulls.items()})

    def nullable_copy(self) -> "Relation":
        """Every column marked nullable — `Join{left_outer}`'s right side
        (§4.2), where the fill rows carry null in every column."""
        return Relation(Schema(tuple(Column(c.name, c.tau.optional())
                                     for c in self.schema)),
                        dict(self.cols), self.n, dict(self.nulls))

    def concat_rows(self, other: "Relation") -> "Relation":
        """Vertical concatenation, `self`'s rows first. Used by the fill side of
        an outer join and by a variable-length expansion's depth levels."""
        if self.schema.names != other.schema.names:
            raise InternalError("cannot concatenate relations of different schemas")
        cols, nulls = {}, {}
        for name in self.schema.names:
            cols[name] = np.concatenate([self.cols[name], other.cols[name]]) \
                if self.n and other.n else (self.cols[name] if other.n == 0
                                            else other.cols[name])
            if name in self.nulls or name in other.nulls:
                nulls[name] = np.concatenate([self.is_null(name), other.is_null(name)])
        return Relation(self.schema, cols, self.n + other.n, nulls)

    # -- diagnostics -----------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        """Row dicts, for tests and receipts — never for evaluation. A null is
        `None` whatever the column's storage holds."""
        out = []
        for i in range(self.n):
            row = {}
            for name in self.schema.names:
                mask = self.nulls.get(name)
                if mask is not None and mask[i]:
                    row[name] = None
                else:
                    row[name] = _py(self.cols[name][i])
            out.append(row)
        return out

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"Relation(n={self.n}, cols={list(self.schema.names)})"


def _py(value: Any) -> Any:
    """A numpy scalar as its Python equivalent, so receipts and digests see the
    same JSON the operators emit."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def combine_nulls(masks: Iterable[np.ndarray | None], n: int) -> np.ndarray | None:
    """The union of several null masks — an expression is null where any of its
    operands is. Returns None when nothing is null, which keeps the fast path
    allocation-free."""
    out: np.ndarray | None = None
    for mask in masks:
        if mask is None:
            continue
        out = mask if out is None else (out | mask)
    if out is None or not out.any():
        return None
    return out


__all__ = ["INT_TAUS", "Relation", "array_for", "combine_nulls"]
