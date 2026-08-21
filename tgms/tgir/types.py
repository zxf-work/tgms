"""TGIR type lattice, schemas and the evaluation scope Σ (TGIR_SPEC §3.1, §4.1).

Three things live here and nothing else:

- `Tau` — the §4.1 type lattice. Identity types are opaque and code-point
  ordered; there is **no list type** and no subtype relation, which is what
  keeps `list-aggregation` and the path family out of the core.
- `Column` / `Schema` — an ordered list of `(name, τ)`. Every column is
  addressed through the variable prefix supplied by the operator that binds it
  (`as` on the two scans, `into`/`edge_var` on `Expand`, the pattern's own
  variable names). A name collision is a **static plan error**, never a silent
  resolution (§4.2).
- `Sigma` — `(T_v, T_b)`, **and nothing else** (§3.1, adjudication §8.6). The
  keying mode by which a scan tests a version against `T_v` — §3.2's
  `overlap` / `instant` / `event` — is a *per-scan parameter*, carried by the
  scan nodes and by `ScopeTerm.vt_mode`, never by Σ.

Nothing here evaluates anything: M2.0 is types, validation and schema
propagation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, Interval

#: §4.1's scalar constructors. `tuple(τ, …)` is built by `Tau.tuple_of`;
#: nullability is the `nullable` flag rather than a wrapper, so `τ??` cannot
#: be spelled.
SCALAR_TAUS: frozenset[str] = frozenset({
    "uid", "eid", "vid",          # identity types — opaque, code-point ordered
    "label", "rel_type",          # interned name types
    "ts",                         # int64 epoch µs, OPEN_END-aware
    "int", "float", "bool", "str",
    "json",                       # an opaque property value
})

#: Identity types, called out because `cast(identity, int)` is legal but
#: partial and row-determining (§2.7).
IDENTITY_TAUS: frozenset[str] = frozenset({"uid", "eid", "vid"})

#: Types `cast` may target (§2.7's `τ ∈ {int, float, str, ts}`).
CAST_TAUS: frozenset[str] = frozenset({"int", "float", "str", "ts"})

#: Types arithmetic and the numeric fns accept.
NUMERIC_TAUS: frozenset[str] = frozenset({"int", "float", "ts"})

#: §3.2's three valid-time keying modes. A *scan* parameter, defaulting to
#: `overlap` — not a component of Σ.
VtMode = Literal["overlap", "instant", "event"]
VT_MODES: frozenset[str] = frozenset({"overlap", "instant", "event"})

#: §3.3's belief-time visibility modes.
BeliefMode = Literal["current", "superseded", "all"]
BELIEF_MODES: frozenset[str] = frozenset({"current", "superseded", "all"})


@dataclass(frozen=True, slots=True)
class Tau:
    """A TGIR type. `base` is a member of `SCALAR_TAUS` or the constructor
    `"tuple"`, in which case `args` carries the component types."""

    base: str
    args: tuple["Tau", ...] = ()
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.base == "tuple":
            if not self.args:
                raise InvalidArgError("tuple(τ, …) needs at least one component")
        elif self.base in SCALAR_TAUS:
            if self.args:
                raise InvalidArgError(f"scalar type {self.base!r} takes no arguments")
        else:
            raise InvalidArgError(f"unknown type: {self.base!r}")

    # -- constructors ----------------------------------------------------
    @staticmethod
    def of(base: str) -> "Tau":
        return Tau(base)

    @staticmethod
    def tuple_of(*args: "Tau") -> "Tau":
        return Tau("tuple", tuple(args))

    def optional(self) -> "Tau":
        return Tau(self.base, self.args, True)

    def required(self) -> "Tau":
        return Tau(self.base, self.args, False)

    # -- predicates ------------------------------------------------------
    @property
    def is_numeric(self) -> bool:
        return self.base in NUMERIC_TAUS

    @property
    def is_identity(self) -> bool:
        return self.base in IDENTITY_TAUS

    def same_base(self, other: "Tau") -> bool:
        """Equality modulo nullability — what a join key comparison needs."""
        return self.base == other.base and self.args == other.args

    def to_json(self) -> str:
        inner = f"({','.join(a.to_json() for a in self.args)})" if self.args else ""
        return f"{self.base}{inner}{'?' if self.nullable else ''}"

    def __str__(self) -> str:  # pragma: no cover - repr sugar
        return self.to_json()


# Convenience singletons — the ones the node signatures use over and over.
T_UID = Tau("uid")
T_EID = Tau("eid")
T_VID = Tau("vid")
T_LABEL = Tau("label")
T_REL_TYPE = Tau("rel_type")
T_TS = Tau("ts")
T_INT = Tau("int")
T_FLOAT = Tau("float")
T_BOOL = Tau("bool")
T_STR = Tau("str")
T_JSON = Tau("json")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    tau: Tau

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidArgError("a column needs a name")

    @property
    def var(self) -> str | None:
        """The binding variable prefix, or None for a `Project`-introduced name."""
        return self.name.split(".", 1)[0] if "." in self.name else None

    def prefixed(self, var: str) -> "Column":
        return Column(f"{var}.{self.name}", self.tau)

    def to_json(self) -> list[str]:
        return [self.name, self.tau.to_json()]


@dataclass(frozen=True, slots=True)
class Schema:
    """An ordered list of `(name, τ)`. Construction rejects duplicate names —
    §4.2's "name collisions are a static plan error, never silently resolved"."""

    columns: tuple[Column, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for c in self.columns:
            if c.name in seen:
                raise InvalidArgError(f"duplicate column in schema: {c.name!r}")
            seen.add(c.name)

    @staticmethod
    def of(*columns: Column) -> "Schema":
        return Schema(tuple(columns))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def __contains__(self, name: object) -> bool:
        return any(c.name == name for c in self.columns)

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self):
        return iter(self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise InvalidArgError(f"unbound column: {name!r}", schema=list(self.names))

    def tau_of(self, name: str) -> Tau:
        return self.column(name).tau

    def concat(self, other: "Schema") -> "Schema":
        """⧺ — the schema constructor every binary operator uses. A collision
        is a static plan error (§4.2), so this is where `Join`'s and
        `Expand`'s collisions surface."""
        clash = sorted(set(self.names) & set(other.names))
        if clash:
            raise InvalidArgError(f"column name collision: {clash}", collision=clash)
        return Schema(self.columns + other.columns)

    def nullable(self) -> "Schema":
        """Every column made nullable — `Join{left_outer}`'s right side (§4.2)."""
        return Schema(tuple(Column(c.name, c.tau.optional()) for c in self.columns))

    def prefixed(self, var: str) -> "Schema":
        return Schema(tuple(c.prefixed(var) for c in self.columns))

    def vars(self) -> tuple[str, ...]:
        out: list[str] = []
        for c in self.columns:
            if c.var is not None and c.var not in out:
                out.append(c.var)
        return tuple(out)

    def to_json(self) -> list[list[str]]:
        return [c.to_json() for c in self.columns]


#: §2.1 / §4.2 — the columns a node variable binds, before prefixing.
NODE_COLUMNS: tuple[Column, ...] = (
    Column("uid", T_UID), Column("vid", T_VID), Column("label", T_LABEL),
    Column("vt_s", T_TS), Column("vt_e", T_TS),
    Column("tt_s", T_TS), Column("tt_e", T_TS),
    Column("props", T_JSON),
)

#: §2.2 / §4.2 — the columns an edge variable binds, before prefixing.
EDGE_COLUMNS: tuple[Column, ...] = (
    Column("eid", T_EID), Column("vid", T_VID),
    Column("src", T_UID), Column("dst", T_UID),
    Column("rel_type", T_REL_TYPE), Column("disc", T_STR),
    Column("vt_s", T_TS), Column("vt_e", T_TS),
    Column("tt_s", T_TS), Column("tt_e", T_TS),
    Column("props", T_JSON),
)

#: §2.9 — `PatternMatch` exposes a *subset* of the node columns to its node
#: variables: `(uid, vid, label, vt_s, vt_e, props)`, without the tt pair.
PATTERN_NODE_COLUMNS: tuple[Column, ...] = tuple(
    c for c in NODE_COLUMNS if c.name not in ("tt_s", "tt_e")
)


def node_schema(var: str) -> Schema:
    return Schema(NODE_COLUMNS).prefixed(var)


def edge_schema(var: str) -> Schema:
    return Schema(EDGE_COLUMNS).prefixed(var)


@dataclass(frozen=True, slots=True)
class Sigma:
    """`Σ = (T_v, T_b)` — the evaluation scope (§3.1).

    `t_v` is a tuple of half-open valid-time intervals. §3.1 writes `T_v` as a
    single `[v_a, v_b)`; the tuple exists because the M2 plan's §5.2 table gives
    `diff_snapshots` the valid-time scope `[t1, t1+1) ∪ [t2, t2+1)` — "the one
    leaf whose Σ is a *pair* of instants". Carrying both exactly is narrower
    than the hull and therefore never less sound (FRESHNESS_SEMANTICS D13.1
    runs the other way: widening is what is always permitted).

    `t_b` is the as-of belief instant; `OPEN_END` means current beliefs, and it
    is **plan-global** in v1 (§3.5).
    """

    t_v: tuple[Interval, ...] = (Interval(0, OPEN_END),)
    t_b: int = OPEN_END

    def __post_init__(self) -> None:
        if not self.t_v:
            raise InvalidArgError("Σ needs at least one valid-time interval")
        if not (0 <= self.t_b <= OPEN_END):
            raise InvalidArgError(f"T_b out of range: {self.t_b}")

    # -- constructors ----------------------------------------------------
    @staticmethod
    def default() -> "Sigma":
        """`T_v = [0, OPEN_END)`, `T_b = OPEN_END` — every existing operator's
        default (§3.1)."""
        return Sigma()

    @staticmethod
    def in_window(t_a: int, t_b_val: int, *, as_of_tt: int = OPEN_END) -> "Sigma":
        return Sigma((Interval(t_a, t_b_val),), as_of_tt)

    @staticmethod
    def at_instant(t: int, *, as_of_tt: int = OPEN_END) -> "Sigma":
        """`T_v = [t, t+1)` — §3.1's instant read."""
        return Sigma((Interval(t, t + 1),), as_of_tt)

    @staticmethod
    def at_instants(*ts: int, as_of_tt: int = OPEN_END) -> "Sigma":
        """`diff_snapshots`' pair-of-instants scope."""
        if not ts:
            raise InvalidArgError("at_instants needs at least one instant")
        return Sigma(tuple(Interval(t, t + 1) for t in ts), as_of_tt)

    # -- §3.5 narrowing --------------------------------------------------
    def covers(self, other: "Sigma") -> bool:
        """True iff `other`'s valid-time extent is contained in this one's and
        the belief instant matches — the test "no node may widen `T_v`" (§3.5)
        and "`T_b` is plan-global in v1"."""
        if other.t_b != self.t_b:
            return False
        return all(
            any(mine.start <= iv.start and iv.end <= mine.end for mine in self.t_v)
            for iv in other.t_v
        )

    def narrow(self, *intervals: Interval) -> "Sigma":
        """An explicit `AtInstant(t)` / `InWindow(w)` scope modifier. Widening
        is refused rather than silently accepted (§3.5)."""
        narrowed = Sigma(tuple(intervals), self.t_b)
        if not self.covers(narrowed):
            raise InvalidArgError(
                "a scope modifier may narrow T_v, never widen it",
                outer=self.to_json(), inner=narrowed.to_json(),
            )
        return narrowed

    @property
    def hull(self) -> Interval:
        return Interval(min(i.start for i in self.t_v), max(i.end for i in self.t_v))

    @property
    def is_instant(self) -> bool:
        return all(i.end - i.start == 1 for i in self.t_v)

    def vt_json(self) -> list[list[int]]:
        """The `vt` component of a `ScopeTerm`, copied from Σ with **no
        interval adjustment** (FRESHNESS_SEMANTICS D13.6)."""
        return [[i.start, i.end] for i in self.t_v]

    def to_json(self) -> dict[str, Any]:
        return {"t_v": self.vt_json(), "t_b": self.t_b}


#: The scope every plan inherits when its root declares none (§3.1).
DEFAULT_SIGMA = Sigma.default()


def check_vt_mode(mode: str) -> str:
    if mode not in VT_MODES:
        raise InvalidArgError(f"unknown vt_mode: {mode!r}", allowed=sorted(VT_MODES))
    return mode


def check_belief(mode: str) -> str:
    if mode not in BELIEF_MODES:
        raise InvalidArgError(f"unknown belief mode: {mode!r}", allowed=sorted(BELIEF_MODES))
    return mode


__all__ = [
    "BELIEF_MODES", "BeliefMode", "CAST_TAUS", "Column", "DEFAULT_SIGMA",
    "EDGE_COLUMNS", "IDENTITY_TAUS", "NODE_COLUMNS", "NUMERIC_TAUS",
    "PATTERN_NODE_COLUMNS", "SCALAR_TAUS", "Schema", "Sigma", "T_BOOL", "T_EID",
    "T_FLOAT", "T_INT", "T_JSON", "T_LABEL", "T_REL_TYPE", "T_STR", "T_TS",
    "T_UID", "T_VID", "Tau", "VT_MODES", "VtMode", "check_belief",
    "check_vt_mode", "edge_schema", "node_schema",
]
