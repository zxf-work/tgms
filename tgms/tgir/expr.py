"""The row-local expression language of TGIR_SPEC §2.7 (the formal content of R2).

`Project`, `Filter`, `Order` and `Aggregate` all take expressions, and §4.2's
schema propagation for `Project` infers each binding's τ from its expression —
so the language is a *typing* obligation for M2.0 even though M2.0 evaluates
nothing. Every node here is a frozen dataclass with two operations:
`tau(schema)` (static type, which also validates) and `columns()` (the bound
columns it reads, for reference resolution).

**Properties required of every expression: pure, row-local, total** — it reads
columns of *one* row and constants, and nothing else.

What is deliberately absent is as normative as what is present (§2.7's
exclusion table): no aggregates, no list values or `collect()`, no `mod`, no
string functions, no set membership, no non-uniform calendar units, no
percentiles. Each is excluded because no B1 row demands it, and adding one here
is a spec change, not a convenience.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal

from tgms.core.errors import InvalidArgError
from tgms.tgir.types import (
    CAST_TAUS, NUMERIC_TAUS, Schema, T_BOOL, T_FLOAT, T_INT, T_JSON, T_STR, Tau,
)

ArithOp = Literal["+", "-", "*", "/", "floor_div"]
ARITH_OPS: frozenset[str] = frozenset({"+", "-", "*", "/", "floor_div"})

CmpOp = Literal["=", "!=", "<", "<=", ">", ">="]
CMP_OPS: frozenset[str] = frozenset({"=", "!=", "<", "<=", ">", ">="})

MathFnName = Literal["abs", "round", "floor", "ceil", "sqrt"]
MATH_FNS: frozenset[str] = frozenset({"abs", "round", "floor", "ceil", "sqrt"})

#: `round`/`floor`/`ceil` are the explicit rounding forms — §2.7's "rounding is
#: never implicit", and the only integer-truncating operator is `floor_div`.
_INT_RESULT_FNS: frozenset[str] = frozenset({"round", "floor", "ceil"})


class Expr(abc.ABC):
    """A pure, row-local, total expression."""

    __slots__ = ()

    @abc.abstractmethod
    def tau(self, schema: Schema) -> Tau:
        """The expression's static type against `schema`; raises
        `InvalidArgError` if the expression is ill-typed or names an unbound
        column."""

    @abc.abstractmethod
    def columns(self) -> tuple[str, ...]:
        """Bound column names read by this expression, in first-seen order."""

    @abc.abstractmethod
    def to_json(self) -> Any:
        """Canonical, JSON-serializable form — feeds `node_digest` (§5.4)."""

    def validate(self, schema: Schema) -> None:
        self.tau(schema)


def _dedup(names: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Lit(Expr):
    """`Literal — int | float | bool | str | null`."""

    value: Any
    lit_tau: Tau | None = None

    def __post_init__(self) -> None:
        if self.lit_tau is None and self.value is not None:
            inferred = _literal_tau(self.value)
            object.__setattr__(self, "lit_tau", inferred)
        if self.value is None and self.lit_tau is None:
            # a bare null needs no declared type; it is `json?`
            object.__setattr__(self, "lit_tau", T_JSON.optional())

    def tau(self, schema: Schema) -> Tau:
        assert self.lit_tau is not None
        return self.lit_tau.optional() if self.value is None else self.lit_tau

    def columns(self) -> tuple[str, ...]:
        return ()

    def to_json(self) -> Any:
        assert self.lit_tau is not None
        return {"lit": self.value, "tau": self.lit_tau.to_json()}


def _literal_tau(value: Any) -> Tau:
    if isinstance(value, bool):
        return T_BOOL
    if isinstance(value, int):
        return T_INT
    if isinstance(value, float):
        return T_FLOAT
    if isinstance(value, str):
        return T_STR
    raise InvalidArgError(f"unsupported literal type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Col(Expr):
    """A bound column, addressed through its variable prefix (§4.2)."""

    name: str

    def tau(self, schema: Schema) -> Tau:
        return schema.tau_of(self.name)

    def columns(self) -> tuple[str, ...]:
        return (self.name,)

    def to_json(self) -> Any:
        return {"col": self.name}


@dataclass(frozen=True, slots=True)
class PropRef(Expr):
    """`var.props[k]` — a property value, which is `json` and always nullable
    (a row need not carry the key)."""

    column: str
    key: str

    def tau(self, schema: Schema) -> Tau:
        schema.tau_of(self.column)
        return T_JSON.optional()

    def columns(self) -> tuple[str, ...]:
        return (self.column,)

    def to_json(self) -> Any:
        return {"prop": [self.column, self.key]}


@dataclass(frozen=True, slots=True)
class Arith(Expr):
    """`Expr ⊕ Expr`, `⊕ ∈ {+, -, *, /, floor_div}`."""

    op: str
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in ARITH_OPS:
            raise InvalidArgError(f"unknown arithmetic operator: {self.op!r}",
                                  allowed=sorted(ARITH_OPS))

    def tau(self, schema: Schema) -> Tau:
        lt, rt = self.left.tau(schema), self.right.tau(schema)
        for t in (lt, rt):
            if t.base not in NUMERIC_TAUS and t.base != "json":
                raise InvalidArgError(f"arithmetic on a non-numeric operand: {t.to_json()}")
        nullable = lt.nullable or rt.nullable or "json" in (lt.base, rt.base)
        if self.op == "/":
            # The blessed arithmetic rule (§2.7) returns an int when an
            # all-int quotient is exact and a float otherwise. §4.1 has no
            # union type, so the static type is the wider of the two.
            out = T_FLOAT
        elif self.op == "floor_div":
            out = T_INT if (lt.base != "float" and rt.base != "float") else T_FLOAT
        elif lt.base == "float" or rt.base == "float":
            out = T_FLOAT
        else:
            out = T_INT if "json" in (lt.base, rt.base) else _wider(lt, rt)
        return out.optional() if nullable else out

    def columns(self) -> tuple[str, ...]:
        return _dedup(self.left.columns() + self.right.columns())

    def to_json(self) -> Any:
        return {"arith": self.op, "l": self.left.to_json(), "r": self.right.to_json()}


def _wider(a: Tau, b: Tau) -> Tau:
    if "float" in (a.base, b.base):
        return T_FLOAT
    if a.base == b.base:
        return Tau(a.base)
    # ts ⊕ int is an int offset; the result is a timestamp only when one side
    # already is one, and int otherwise.
    return Tau("ts") if "ts" in (a.base, b.base) else T_INT


@dataclass(frozen=True, slots=True)
class MathFn(Expr):
    """`abs | round | floor | ceil | sqrt` — §2.7's explicit rounding forms."""

    fn: str
    arg: Expr

    def __post_init__(self) -> None:
        if self.fn not in MATH_FNS:
            raise InvalidArgError(f"unknown function: {self.fn!r}", allowed=sorted(MATH_FNS))

    def tau(self, schema: Schema) -> Tau:
        at = self.arg.tau(schema)
        if at.base not in NUMERIC_TAUS and at.base != "json":
            raise InvalidArgError(f"{self.fn}() on a non-numeric operand: {at.to_json()}")
        if self.fn == "sqrt":
            out = T_FLOAT
        elif self.fn in _INT_RESULT_FNS:
            out = T_INT
        else:  # abs preserves its argument's type
            out = T_INT if at.base == "json" else Tau(at.base)
        return out.optional() if at.nullable else out

    def columns(self) -> tuple[str, ...]:
        return self.arg.columns()

    def to_json(self) -> Any:
        return {"fn": self.fn, "arg": self.arg.to_json()}


@dataclass(frozen=True, slots=True)
class Cmp(Expr):
    """`Expr ⋈ Expr`, `⋈ ∈ {=, ≠, <, ≤, >, ≥}`. Strings compare by code point
    (`COLLATE "C"`)."""

    op: str
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in CMP_OPS:
            raise InvalidArgError(f"unknown comparison: {self.op!r}", allowed=sorted(CMP_OPS))

    def tau(self, schema: Schema) -> Tau:
        self.left.tau(schema)
        self.right.tau(schema)
        return T_BOOL

    def columns(self) -> tuple[str, ...]:
        return _dedup(self.left.columns() + self.right.columns())

    def to_json(self) -> Any:
        return {"cmp": self.op, "l": self.left.to_json(), "r": self.right.to_json()}


@dataclass(frozen=True, slots=True)
class Not(Expr):
    arg: Expr

    def tau(self, schema: Schema) -> Tau:
        _require_bool(self.arg, schema, "¬")
        return T_BOOL

    def columns(self) -> tuple[str, ...]:
        return self.arg.columns()

    def to_json(self) -> Any:
        return {"not": self.arg.to_json()}


@dataclass(frozen=True, slots=True)
class BoolOp(Expr):
    """`Expr ∧ Expr` / `Expr ∨ Expr`."""

    op: str
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in ("and", "or"):
            raise InvalidArgError(f"unknown boolean operator: {self.op!r}")

    def tau(self, schema: Schema) -> Tau:
        _require_bool(self.left, schema, self.op)
        _require_bool(self.right, schema, self.op)
        return T_BOOL

    def columns(self) -> tuple[str, ...]:
        return _dedup(self.left.columns() + self.right.columns())

    def to_json(self) -> Any:
        return {"bool": self.op, "l": self.left.to_json(), "r": self.right.to_json()}


def _require_bool(e: Expr, schema: Schema, where: str) -> None:
    t = e.tau(schema)
    if t.base not in ("bool", "json"):
        raise InvalidArgError(f"{where} needs a boolean operand, got {t.to_json()}")


@dataclass(frozen=True, slots=True)
class IsNull(Expr):
    arg: Expr

    def tau(self, schema: Schema) -> Tau:
        self.arg.tau(schema)
        return T_BOOL

    def columns(self) -> tuple[str, ...]:
        return self.arg.columns()

    def to_json(self) -> Any:
        return {"is_null": self.arg.to_json()}


@dataclass(frozen=True, slots=True)
class Coalesce(Expr):
    left: Expr
    right: Expr

    def tau(self, schema: Schema) -> Tau:
        lt, rt = self.left.tau(schema), self.right.tau(schema)
        out = _unify(lt, rt, "coalesce")
        return out.required() if not rt.nullable else out

    def columns(self) -> tuple[str, ...]:
        return _dedup(self.left.columns() + self.right.columns())

    def to_json(self) -> Any:
        return {"coalesce": [self.left.to_json(), self.right.to_json()]}


@dataclass(frozen=True, slots=True)
class If(Expr):
    """Row-local CASE."""

    cond: Expr
    then: Expr
    otherwise: Expr

    def tau(self, schema: Schema) -> Tau:
        _require_bool(self.cond, schema, "if")
        return _unify(self.then.tau(schema), self.otherwise.tau(schema), "if")

    def columns(self) -> tuple[str, ...]:
        return _dedup(self.cond.columns() + self.then.columns() + self.otherwise.columns())

    def to_json(self) -> Any:
        return {"if": [self.cond.to_json(), self.then.to_json(), self.otherwise.to_json()]}


def _unify(a: Tau, b: Tau, where: str) -> Tau:
    nullable = a.nullable or b.nullable
    if a.same_base(b):
        out = Tau(a.base, a.args)
    elif a.base == "json" or b.base == "json":
        out = T_JSON
    elif a.is_numeric and b.is_numeric:
        out = _wider(a, b)
    else:
        raise InvalidArgError(
            f"{where}: incompatible branch types {a.to_json()} / {b.to_json()}")
    return out.optional() if nullable else out


@dataclass(frozen=True, slots=True)
class TupleExpr(Expr):
    """`tuple(Expr, …)` — the pair / composite key constructor. This is where a
    `Join` key or a canonical pair key is built (§2.8)."""

    items: tuple[Expr, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise InvalidArgError("tuple() needs at least one component")

    def tau(self, schema: Schema) -> Tau:
        return Tau.tuple_of(*(i.tau(schema) for i in self.items))

    def columns(self) -> tuple[str, ...]:
        return _dedup(tuple(c for i in self.items for c in i.columns()))

    def to_json(self) -> Any:
        return {"tuple": [i.to_json() for i in self.items]}


@dataclass(frozen=True, slots=True)
class Cast(Expr):
    """`cast(Expr, τ)`, `τ ∈ {int, float, str, ts}`.

    `cast` on an identity type is **legal but partial, and row-determining**
    (§2.7): well-typed iff the identity string is a canonical decimal integer,
    an `E_ARG` error otherwise. A silent null would change which rows exist,
    which is why the partiality is an error rather than a null — the same
    discipline division by zero gets. R2's absorption of `cast-in-ordering`
    depends on this rule.
    """

    arg: Expr
    to: Tau

    def __post_init__(self) -> None:
        if self.to.base not in CAST_TAUS or self.to.args:
            raise InvalidArgError(f"cast target must be one of {sorted(CAST_TAUS)}",
                                  got=self.to.to_json())

    def tau(self, schema: Schema) -> Tau:
        at = self.arg.tau(schema)
        if at.base == "tuple":
            raise InvalidArgError("cast on a tuple is not defined")
        return self.to.optional() if at.nullable else self.to

    def columns(self) -> tuple[str, ...]:
        return self.arg.columns()

    def to_json(self) -> Any:
        return {"cast": self.arg.to_json(), "to": self.to.to_json()}


__all__ = [
    "ARITH_OPS", "Arith", "ArithOp", "BoolOp", "CMP_OPS", "Cast", "Cmp", "CmpOp",
    "Coalesce", "Col", "Expr", "If", "IsNull", "Lit", "MATH_FNS", "MathFn",
    "MathFnName", "Not", "PropRef", "TupleExpr",
]
