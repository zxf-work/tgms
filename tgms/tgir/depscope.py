"""`DependencyScope` — the C3 wire format (FRESHNESS_SEMANTICS §13.1, D13.2–D13.9).

The object a result carries so a later reader can ask "could anything written
since have changed this?" without re-running it. Three properties govern every
line here:

- **D13.1 (monotone approximation).** *Every approximation in this contract
  widens.* A scope may be replaced by any superset and a `"*"` substituted for
  any component at any time; **no operation may narrow anything at runtime.**
  That is what makes every cost/precision trade-off automatically sound.
- **D13.5.** ⊤ is the JSON string `"*"`, one spelling at every level, and is
  deliberately distinct from `[]`, which means *nothing matches* and makes the
  term vacuous. In Python it is the `TOP` singleton, so a uid that happens to
  spell `"*"` cannot masquerade as ⊤.
- **D13.6.** `vt` intervals are **copied from Σ's window with no adjustment**.
  The right-closure lives entirely on the *footprint* side, in the two
  constructors `vt_closed` / `vt_from` below — the `+1` exists at exactly one
  site.

`terms` is a **disjunction**: the scope admits an op iff any term admits it. An
empty `terms` list is the empty scope ∅ — nothing can ever invalidate this
result — and is the correct, non-degenerate value for a `compute` node over
literal inputs, not a defect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal, Union

from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, canonical_json, digest
from tgms.storage.eventlog import SEED_CHAIN

SCHEMA_NAME = "tgms-depscope"

#: D13.9 — an integer. A reader that does not recognize a scope's version must
#: return `UNDECIDABLE`, never `FRESH`.
SCHEMA_VERSION = 1


class _Top:
    """⊤. Serializes to the JSON string `"*"` (D13.5) and is a singleton, so
    `is TOP` is the membership test and no list can be mistaken for it."""

    __slots__ = ()
    _instance: "_Top | None" = None

    def __new__(cls) -> "_Top":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "TOP"

    def to_json(self) -> str:
        return "*"


TOP = _Top()
TOP_JSON = "*"

#: The five logged op kinds (D13.3). `correct` and `retract` are not split by
#: entity kind on the wire, so the shorthand classes below map onto them.
Kind = Literal["assert_node", "assert_edge", "correct", "retract", "ingest_events"]
KINDS: tuple[str, ...] = ("assert_node", "assert_edge", "correct", "retract", "ingest_events")

#: D8.4's kind classes, expressed in the wire vocabulary. `correct`/`retract`
#: carry no entity-kind discriminator on the wire, so `𝒩` and `ℰ` both name
#: them — a widening (D13.1), and the only encoding the format allows.
K_NODE: tuple[str, ...] = ("assert_node", "correct", "retract", "ingest_events")
K_EDGE: tuple[str, ...] = ("assert_edge", "correct", "retract", "ingest_events")
#: `𝒟` — ops that register a dense entity id **without** writing a node
#: version, via `ensure_entities`. L13.3: wherever `kinds` includes `𝒟`,
#: `targets` must carry an `incident` arm over the same uids, or `𝒟`'s presence
#: is inert.
K_DENSE_ID: tuple[str, ...] = ("assert_edge", "ingest_events")

#: D13.3 — the storage layer's own role enum, all four values. `both` is the
#: one genuine narrowing it offers; omitting it was gate finding FF-8, where an
#: implementer who wrote the term down faithfully got one that matched nothing.
IncidentRole = Literal["src", "dst", "either", "both"]
INCIDENT_ROLES: tuple[str, ...] = ("src", "dst", "either", "both")

VT_MODES: tuple[str, ...] = ("overlap", "instant", "event")

#: D13.7 — real property keys are bare strings; pseudo-keys are `@`-prefixed,
#: which no real key can be. Two are unbounded in valid time and ride the
#: *carve* arm; the rest are value-arm and are bounded by the op.
PSEUDO_PROPS: tuple[str, ...] = (
    "@label", "@identity", "@extent", "@event_key", "@recut", "@version",
)
#: D13.7a: a scope that names neither of these is untouched by the carve arm
#: and keeps its window; one that names either loses `vt` against Class B/C/D.
CARVE_PROPS: frozenset[str] = frozenset({"@recut", "@version"})

#: The store identity used when there is no event log behind the read at all —
#: an adapter-only context (`DuckDBAdapter(":memory:")`, every oracle-family
#: test). Coordinator ruling, M2.0.
UNANCHORED = "unanchored"

UidSet = Union[_Top, tuple[str, ...]]


def _uids_json(value: UidSet) -> Any:
    return TOP_JSON if value is TOP else list(value)  # type: ignore[arg-type]


def _parse_uids(value: Any, where: str) -> UidSet:
    if value == TOP_JSON:
        return TOP
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise InvalidArgError(f"{where}: expected \"*\" or a list of strings", got=value)


# ---------------------------------------------------------------------------
# valid-time constructors — the footprint side (D13.21)
# ---------------------------------------------------------------------------

def vt_closed(vt_s: int, vt_e: int) -> tuple[tuple[int, int], ...]:
    """`[vt_s, min(vt_e + 1, OPEN_END))` — the value arm for asserts, corrects
    and events (D13.21).

    D8.6 made structural: a footprint's interval is closed at the right,
    because a carve fragment can start exactly at the op's `vt_e`. The
    saturation is exact rather than defensive — a fragment `[max(vs, ce), ve)`
    requires `ce < ve <= OPEN_END`, so no fragment can start at `OPEN_END`.

    **This and `vt_from` are the only sites that build a footprint `vt`.** A
    per-operator *scope* derivation never performs an interval adjustment: it
    writes down Σ's window and stops (D13.6).
    """
    if not (0 <= vt_s < vt_e <= OPEN_END):
        raise InvalidArgError(f"vt_closed needs 0 <= vt_s < vt_e <= OPEN_END, got [{vt_s}, {vt_e})")
    return ((vt_s, min(vt_e + 1, OPEN_END)),)


def vt_from(t: int) -> tuple[tuple[int, int], ...]:
    """`[t, OPEN_END)` — the value arm for a retract (D13.21)."""
    if not (0 <= t < OPEN_END):
        raise InvalidArgError(f"vt_from needs 0 <= t < OPEN_END, got {t}")
    return ((t, OPEN_END),)


def vt_carve() -> _Top:
    """`"*"` — the carve arm (D13.21a).

    A carve does not confine itself to the carved interval: `_remainder`
    re-inserts fragments whose endpoints come from the **superseded** version,
    which is apply-time store state the log record does not carry. No interval
    arithmetic can recover it, so the only sound move is to widen.
    """
    return TOP


# ---------------------------------------------------------------------------
# D13.3 — ScopeTerm and its target arms
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Incident:
    """`{"role": …, "uids": …}` — **the load-bearing arm** (D13.4).

    An endpoint-scoped term matches an edge write whose `eid` *did not exist
    when the scope was written*. Without it every narrow operator would have to
    fall back to `"*"` to stay sound against newly created edges, and the narrow
    anchors would evaporate.
    """

    role: str
    uids: UidSet = TOP

    def __post_init__(self) -> None:
        if self.role not in INCIDENT_ROLES:
            raise InvalidArgError(f"unknown incident role: {self.role!r}",
                                  allowed=list(INCIDENT_ROLES))
        if self.uids is not TOP and not isinstance(self.uids, tuple):
            raise InvalidArgError("incident.uids must be TOP or a tuple of uids")

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "uids": _uids_json(self.uids)}

    @staticmethod
    def from_json(obj: Any) -> "Incident":
        if not isinstance(obj, dict):
            raise InvalidArgError("incident arm must be an object or \"*\"", got=obj)
        return Incident(obj["role"], _parse_uids(obj.get("uids", TOP_JSON), "incident.uids"))


@dataclass(frozen=True, slots=True)
class EdgeKey:
    """One member of the `edges` arm's object form:
    `{"src":?, "dst":?, "rel_type":?, "disc":?}`."""

    src: str | None = None
    dst: str | None = None
    rel_type: str | None = None
    disc: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in
                (("src", self.src), ("dst", self.dst),
                 ("rel_type", self.rel_type), ("disc", self.disc))
                if v is not None}

    @staticmethod
    def from_json(obj: Any) -> "EdgeKey":
        if not isinstance(obj, dict):
            raise InvalidArgError("an edges-arm member must be an eid string or an object")
        return EdgeKey(obj.get("src"), obj.get("dst"), obj.get("rel_type"), obj.get("disc"))


EdgeSet = Union[_Top, tuple[str, ...], tuple[EdgeKey, ...]]


@dataclass(frozen=True, slots=True)
class Targets:
    """The three target arms of `I` (D13.3).

    `I` is split into three because the operators genuinely scope three
    different ways: by node identity (`entity_history`), by edge identity (a
    point read), and **by endpoint** (`neighborhood_evolution`,
    `aggregate_events`' `endpoint_filter`, `Expand`'s seeds).

    **An absent arm means ∅ for that entity kind** (D13.5) — which is why
    `None` and `TOP` are different values here, and why a `nodes` arm that is
    absent is not the same as one that is `"*"`.
    """

    nodes: UidSet | None = None
    edges: EdgeSet | None = None
    incident: Union[_Top, Incident, None] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.nodes is not None:
            out["nodes"] = _uids_json(self.nodes)
        if self.edges is not None:
            out["edges"] = (TOP_JSON if self.edges is TOP
                            else [e.to_json() if isinstance(e, EdgeKey) else e
                                  for e in self.edges])  # type: ignore[union-attr]
        if self.incident is not None:
            out["incident"] = (TOP_JSON if self.incident is TOP
                               else self.incident.to_json())  # type: ignore[union-attr]
        return out

    @staticmethod
    def from_json(obj: Any) -> Union[_Top, "Targets"]:
        if obj == TOP_JSON:
            return TOP
        if not isinstance(obj, dict):
            raise InvalidArgError("targets must be \"*\" or an object", got=obj)
        nodes = _parse_uids(obj["nodes"], "targets.nodes") if "nodes" in obj else None
        edges: EdgeSet | None = None
        if "edges" in obj:
            raw = obj["edges"]
            if raw == TOP_JSON:
                edges = TOP
            elif isinstance(raw, list):
                edges = tuple(e if isinstance(e, str) else EdgeKey.from_json(e) for e in raw)
            else:
                raise InvalidArgError("targets.edges must be \"*\" or a list", got=raw)
        incident: Union[_Top, Incident, None] = None
        if "incident" in obj:
            incident = TOP if obj["incident"] == TOP_JSON else Incident.from_json(obj["incident"])
        return Targets(nodes, edges, incident)


PropSet = Union[_Top, tuple[str, ...]]
RelTypeSet = Union[_Top, tuple[str, ...]]
VtSet = Union[_Top, tuple[tuple[int, int], ...]]
TargetSet = Union[_Top, Targets]


@dataclass(frozen=True, slots=True)
class ScopeTerm:
    """`⟨K, I, T, V, P⟩` — a conjunction (D13.3). The scope's `terms` list is
    the disjunction of these."""

    kinds: Union[_Top, tuple[str, ...]] = TOP
    targets: TargetSet = TOP
    rel_types: RelTypeSet = TOP
    vt: VtSet = TOP
    vt_mode: str = "overlap"
    props: PropSet = TOP

    def __post_init__(self) -> None:
        if self.kinds is not TOP:
            unknown = [k for k in self.kinds if k not in KINDS]  # type: ignore[union-attr]
            if unknown:
                raise InvalidArgError(f"unknown op kind(s): {unknown}", allowed=list(KINDS))
            if set(self.kinds) == set(KINDS):  # type: ignore[arg-type]
                # D13.5's one-spelling rule: a set naming every kind *is* ⊤, and
                # ⊤ has exactly one encoding. Canonicalizing at construction
                # rather than at serialization keeps `==` and the round-trip
                # agreeing — `𝒩 ∪ 𝒟` is all five, so this fires on every
                # `NodeScan` and `Expand` term (coordinator ruling, M2.1).
                object.__setattr__(self, "kinds", TOP)
        if self.vt_mode not in VT_MODES:
            raise InvalidArgError(f"unknown vt_mode: {self.vt_mode!r}", allowed=list(VT_MODES))
        if self.vt is not TOP:
            for iv in self.vt:  # type: ignore[union-attr]
                if not (isinstance(iv, tuple) and len(iv) == 2):
                    raise InvalidArgError("a vt entry is a [v_a, v_b) pair", got=iv)
                if not (0 <= iv[0] < iv[1] <= OPEN_END):
                    raise InvalidArgError(f"invalid vt interval: [{iv[0]}, {iv[1]})")
        if self.props is not TOP:
            bad = [p for p in self.props  # type: ignore[union-attr]
                   if p.startswith("@") and p not in PSEUDO_PROPS]
            if bad:
                raise InvalidArgError(f"unknown pseudo-property key(s): {bad}",
                                      allowed=list(PSEUDO_PROPS))

    @property
    def carve_reachable(self) -> bool:
        """D13.7a: an operator is reachable by the carve arm iff its `P` names
        `@recut` or `@version` — and then its `vt` is worthless against Class
        B/C/D ops."""
        if self.props is TOP:
            return True
        return bool(CARVE_PROPS & set(self.props))  # type: ignore[arg-type]

    def to_json(self) -> dict[str, Any]:
        return {
            "kinds": TOP_JSON if self.kinds is TOP else list(self.kinds),  # type: ignore[arg-type]
            "targets": TOP_JSON if self.targets is TOP else self.targets.to_json(),  # type: ignore[union-attr]
            "rel_types": (TOP_JSON if self.rel_types is TOP
                          else list(self.rel_types)),  # type: ignore[arg-type]
            "vt": TOP_JSON if self.vt is TOP else [list(iv) for iv in self.vt],  # type: ignore[union-attr]
            "vt_mode": self.vt_mode,
            "props": TOP_JSON if self.props is TOP else list(self.props),  # type: ignore[arg-type]
        }

    @staticmethod
    def from_json(obj: Any) -> "ScopeTerm":
        if not isinstance(obj, dict):
            raise InvalidArgError("a term must be an object", got=obj)
        kinds = TOP if obj["kinds"] == TOP_JSON else tuple(obj["kinds"])
        rel_types = TOP if obj["rel_types"] == TOP_JSON else tuple(obj["rel_types"])
        vt: VtSet = (TOP if obj["vt"] == TOP_JSON
                     else tuple((int(a), int(b)) for a, b in obj["vt"]))
        props = TOP if obj["props"] == TOP_JSON else tuple(obj["props"])
        return ScopeTerm(kinds, Targets.from_json(obj["targets"]), rel_types, vt,
                         obj["vt_mode"], props)


#: The coarse default of plan §7.1 — explicitly legal, and the value every
#: operator whose derivation is not yet written carries (§5.5.4 constraint 1:
#: `"*"` everywhere is a valid v1 answer).
TOP_TERM = ScopeTerm()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One `(offset, chain)` pair. `offset` is the absolute file position past
    the last applied record; `chain` is the rolling hash of that prefix."""

    offset: int
    chain: str

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise InvalidArgError(f"checkpoint offset must be >= 0, got {self.offset}")
        if not self.chain:
            raise InvalidArgError("a checkpoint needs a chain hash")

    def to_json(self) -> list[Any]:
        return [self.offset, self.chain]

    @staticmethod
    def from_json(obj: Any) -> "Checkpoint":
        if not (isinstance(obj, (list, tuple)) and len(obj) == 2):
            raise InvalidArgError("a checkpoint is an [offset, chain] pair", got=obj)
        return Checkpoint(int(obj[0]), str(obj[1]))


#: D13.8a's fallback: a scope that cannot establish the cursor invariant
#: carries a full-log scan — "widening and therefore sound. Slow, never wrong."
FULL_SCAN_CHECKPOINTS: tuple[Checkpoint, ...] = (Checkpoint(0, SEED_CHAIN),)


@dataclass(frozen=True, slots=True)
class DependencyScope:
    """D13.2's object, canonical JSON, versioned, carried per result."""

    store: str
    tt_q: int
    terms: tuple[ScopeTerm, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = FULL_SCAN_CHECKPOINTS
    pinned: bool = False
    clamped: bool = False
    version: int = SCHEMA_VERSION
    #: **Additive to D13.2, and emitted only when false** (coordinator ruling,
    #: M2.1): the read's `tt_q` could not be established against the applied
    #: prefix — a legacy store whose backend keeps no event cursor, where the
    #: log's tail is an upper bound on what was applied rather than a statement
    #: about it. A reader that ignores the key sees exactly D13.2's object; one
    #: that honours it knows this `tt_q` was not rounded down against a cursor
    #: and must not be trusted in the `FRESH` direction.
    tt_q_verified: bool = True
    #: **Additive to D13.2, on `tt_q_verified`'s (E-5) pattern — §15's
    #: 2026-08-27 entry E-10.** The `as_of_tt` the read actually applied, an
    #: int64 µs value, emitted **only when the producer wants the per-batch
    #: exemption of `check` step 8a** (D13.24). **Absent means no exemption** —
    #: the fail-safe default, so every scope written before E-10 behaves
    #: exactly as it did before it (byte-identical verdicts) and the feature is
    #: opt-in per scope. Not a version bump for the same reason: a v1 reader
    #: that ignores this key computes today's verdict, which is sound.
    as_of_tt: int | None = None

    def __post_init__(self) -> None:
        if not self.store:
            raise InvalidArgError("a dependency scope needs a store identity")
        if not (0 <= self.tt_q <= OPEN_END):
            raise InvalidArgError(f"tt_q out of range: {self.tt_q}")
        if self.as_of_tt is not None and not (0 <= self.as_of_tt <= OPEN_END):
            raise InvalidArgError(f"as_of_tt out of range: {self.as_of_tt}")
        if not self.checkpoints:
            # D13.2 makes `checkpoints` mandatory and D13.24 makes it
            # load-bearing as the scan's starting point; an empty list has no
            # minimum offset, so `⊎` could not move the triple either.
            raise InvalidArgError(
                "checkpoints is mandatory — use FULL_SCAN_CHECKPOINTS for the D13.8a fallback")

    # -- the empty scope -------------------------------------------------
    @staticmethod
    def empty(store: str, tt_q: int, *, checkpoints: tuple[Checkpoint, ...] | None = None,
              pinned: bool = False, clamped: bool = False) -> "DependencyScope":
        """∅ — `terms: []`. Nothing can ever invalidate this result. The
        correct, non-degenerate value for a `compute` node over literal
        inputs (D13.2, §6 #15), never a defect."""
        return DependencyScope(store, tt_q, (), checkpoints or FULL_SCAN_CHECKPOINTS,
                               pinned, clamped)

    @staticmethod
    def top(store: str, tt_q: int, *, checkpoints: tuple[Checkpoint, ...] | None = None,
            pinned: bool = False, clamped: bool = False) -> "DependencyScope":
        """The single all-`"*"` term — the coarse day-one default."""
        return DependencyScope(store, tt_q, (TOP_TERM,),
                               checkpoints or FULL_SCAN_CHECKPOINTS, pinned, clamped)

    @property
    def is_empty(self) -> bool:
        return not self.terms

    @property
    def min_offset(self) -> int:
        return min(c.offset for c in self.checkpoints)

    # -- D13.8 union -----------------------------------------------------
    def union(self, other: "DependencyScope") -> "DependencyScope":
        """`⊎` — **concatenation**, plus one comparison (D13.8, D13.8a, D13.8b).

        - `terms` and `checkpoints` **concatenate**. Keeping checkpoints as a
          list is what stops a union from shrinking D13.18's tamper-evidence to
          the earliest prefix: with offsets `{500, 900}` both chains are
          verified, `[500, 900)` regains its integrity test, and the scan still
          starts at `500`.
        - `(tt_q, pinned, clamped)` move **as a unit**, taken from whichever
          operand has the smaller minimum checkpoint offset — never a
          component-wise combination. Taking `min` of the `tt_q`s while keeping
          the other operand's offset breaks the cursor invariant: batches lying
          in the tt-suffix but before the retained offset would never be
          scanned, and the verdict would be `FRESH` on a changed result.
        - Two operands whose `store` differs do **not** union: `⊎` refuses **at
          construction** rather than producing an object `check` would reject
          one plan too late (RG-6).

        Normalization (merging terms, hulling intervals) is optional and always
        widening; this implementation skips it entirely, which is what keeps
        composing scopes O(1) per step.
        """
        if self.store != other.store:
            raise InvalidArgError(
                "⊎ refuses at construction: the two scopes are over different stores",
                left=self.store, right=other.store)
        if self.version != other.version:
            raise InvalidArgError(
                "⊎ refuses at construction: the two scopes carry different schema versions "
                "(D13.9 — a reader must not silently pick one)",
                left=self.version, right=other.version)
        mine, theirs = self.min_offset, other.min_offset
        if mine < theirs or (mine == theirs and self.tt_q <= other.tt_q):
            basis = self
        else:
            basis = other
        return DependencyScope(
            store=self.store,
            tt_q=basis.tt_q,
            terms=self.terms + other.terms,
            checkpoints=self.checkpoints + other.checkpoints,
            pinned=basis.pinned,
            clamped=basis.clamped,
            version=self.version,
            # the verification flag belongs to the `tt_q` it describes, so it
            # moves with the triple rather than being combined
            tt_q_verified=basis.tt_q_verified,
            # E-10: `max`, not the `tt_q`/`pinned`/`clamped` triple's "moves as
            # a unit from whichever operand is basis" rule. The merged scope
            # may exempt only what BOTH operands would have exempted — the
            # opposite direction from `tt_q`'s earliest-wins union, and for the
            # same reason both choices widen. Absent on either operand means
            # that operand exempts nothing, so the merge exempts nothing.
            as_of_tt=(max(self.as_of_tt, other.as_of_tt)
                     if self.as_of_tt is not None and other.as_of_tt is not None
                     else None),
        )

    def with_terms(self, terms: Iterable[ScopeTerm]) -> "DependencyScope":
        return replace(self, terms=tuple(terms))

    # -- serialization ---------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        out = {
            "schema": SCHEMA_NAME,
            "version": self.version,
            "store": self.store,
            "tt_q": self.tt_q,
            "pinned": self.pinned,
            "clamped": self.clamped,
            "checkpoints": [c.to_json() for c in self.checkpoints],
            "terms": [t.to_json() for t in self.terms],
        }
        if not self.tt_q_verified:
            out["tt_q_verified"] = False
        if self.as_of_tt is not None:
            out["as_of_tt"] = self.as_of_tt
        return out

    def canonical(self) -> str:
        return canonical_json(self.to_json())

    def digest(self) -> str:
        return digest(self.to_json())

    @staticmethod
    def from_json(obj: Any) -> "DependencyScope":
        if not isinstance(obj, dict):
            raise InvalidArgError("a dependency scope must be an object", got=type(obj).__name__)
        if obj.get("schema") != SCHEMA_NAME:
            raise InvalidArgError(f"not a {SCHEMA_NAME} object", got=obj.get("schema"))
        return DependencyScope(
            store=obj["store"],
            tt_q=int(obj["tt_q"]),
            terms=tuple(ScopeTerm.from_json(t) for t in obj["terms"]),
            checkpoints=tuple(Checkpoint.from_json(c) for c in obj["checkpoints"]),
            pinned=bool(obj["pinned"]),
            clamped=bool(obj["clamped"]),
            version=int(obj["version"]),
            tt_q_verified=bool(obj.get("tt_q_verified", True)),
            as_of_tt=(int(obj["as_of_tt"]) if obj.get("as_of_tt") is not None else None),
        )


def union_all(scopes: Iterable[DependencyScope]) -> DependencyScope:
    """`⊎` over a plan's steps. Refuses on an empty sequence: ∅ needs a store
    identity and a `tt_q`, neither of which can be invented here."""
    it = iter(scopes)
    try:
        acc = next(it)
    except StopIteration:
        raise InvalidArgError("union_all needs at least one scope") from None
    for s in it:
        acc = acc.union(s)
    return acc


def store_identity(header_record: Any, first_batch_record: Any = None) -> str:
    """The store identity `⊎` refuses across (D13.2's `store`).

    **Coordinator ruling (M2.1): the digest of the event log's header record
    concatenated with its FIRST BATCH record.** The header alone is the
    constant `{"format": "tgms-eventlog", "version": 1}`
    (`tgms/storage/eventlog.py:31`) and discriminates nothing; the first batch
    carries a content-addressed `batch_id` and that history's own `tt`, so the
    pair is **discriminating between stores and stable across replays of one
    history** — which `store_digest()` is not, being content-dependent and
    therefore changing on every write.

    A log with **no batches yet** has no identity to state, so it takes the
    `UNANCHORED` sentinel until its first write. Adapter-only contexts — an
    in-memory adapter with no event log at all — take it too.

    Records may be passed parsed, as raw JSON text, or as raw bytes.
    """
    if first_batch_record is None:
        return UNANCHORED
    return digest([_as_record(header_record), _as_record(first_batch_record)])


def _as_record(record: Any) -> Any:
    if isinstance(record, (bytes, bytearray)):
        record = record.decode("utf-8")
    if isinstance(record, str):
        record = json.loads(record)
    return record


__all__ = [
    "CARVE_PROPS", "Checkpoint", "DependencyScope", "EdgeKey", "FULL_SCAN_CHECKPOINTS",
    "INCIDENT_ROLES", "Incident", "IncidentRole", "KINDS", "K_DENSE_ID", "K_EDGE",
    "K_NODE", "Kind", "PSEUDO_PROPS", "SCHEMA_NAME", "SCHEMA_VERSION", "ScopeTerm",
    "TOP", "TOP_JSON", "TOP_TERM", "Targets", "UNANCHORED", "union_all",
    "store_identity", "vt_carve", "vt_closed", "vt_from",
]
