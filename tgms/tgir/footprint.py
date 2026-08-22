"""`CorrectionFootprint` — the dual of a scope term (FRESHNESS_SEMANTICS D13.20–D13.22).

A scope says *what a read depended on*. A footprint says *what a write
touched*. `check` asks whether they intersect, and that question is decidable
because both sides are made of sets.

**The restriction that shapes this whole module: a footprint is derived from
one logged op record and nothing else** (D13.20). No store, no adapter, no
believed-version lookup — which is what lets a checker run against a log it did
not produce, and is enforced by `scripts/check_freshness_boundary.py` rather
than by good intentions. Two consequences are paid for rather than argued away:

- **The carve extent is not knowable.** `_remainder` re-inserts fragments whose
  endpoints come from the *superseded* version, so a carve reaches valid-time
  locations the op's own arguments do not bound, in both directions. D13.21a's
  answer is a second footprint per op — the **carve arm** — with `vt = "*"` and
  `props = {@recut, @version}`. It is emitted **unconditionally**, because
  whether the op actually carved is apply-time store state.
- **Effect class A vs B is not knowable** for an assert: `_assert_node` decides
  it with `believed_node_versions(uid)`. So the class field is the literal
  string `"A|B"` (CO-3), and **`class` is not a conjunct of `intersects`** — it
  is witness metadata. Soundness does not depend on it.

Everything is a **set** (CO-8). `identity.src` on the `ingest_events` edge arm
is a list of endpoints, not one endpoint; a membership test that reads
`["C"] ∈ ["X","B","C"]` returns false, and a false here is a false *negative*,
which is the one direction D1.13 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Union

from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, edge_eid
from tgms.tgir.depscope import (
    KINDS, PSEUDO_PROPS, TOP, TOP_JSON, PropSet, RelTypeSet, VtSet, _Top,
    vt_carve, vt_closed, vt_from,
)

Arm = Literal["value", "carve"]
EntityKind = Literal["node", "edge"]

#: A coarsened identity field above this many distinct values becomes `"*"`.
#: D13.22 sanctions it in as many words — *"an implementation may coarsen
#: either arm to `"*"` at any size threshold it likes"* — because every such
#: coarsening only ever **adds** matches and is therefore a widening (D13.1).
#:
#: 256 is chosen, not measured: it is far above any hand-written correction
#: batch (so the narrow set survives where precision could matter) and far
#: below a bulk `ingest_events` load (so a 50,000-event batch does not carry
#: 50,000 hashes into every witness it produces). M4.6 measures scope and
#: witness bytes; if this number turns out to matter, it is one constant.
COARSEN_ABOVE = 256

#: The class-A-versus-B question is not answerable from a log record, so the
#: wire carries the disjunction literally (D13.20, CO-3, plan §9.10).
CLASS_ASSERT = "A|B"

#: Value-arm props for the two ops that carve without replacing a whole
#: version. `@event_key` is CE-5's channel: a property-only correction over a
#: sub-interval multiplies an identity's *events*, so an event-keyed operator
#: that reads no property at all still changes. `@extent` is coverage.
_CORRECT_NODE_PSEUDO = ("@label", "@extent", "@event_key")
_CORRECT_EDGE_PSEUDO = ("@extent", "@event_key")
_RETRACT_PSEUDO = ("@extent", "@event_key")

#: D13.21a. Exactly the two effects whose reach is unbounded (D13.7a) — which
#: is what stops the carve arm from flattening the contract: a scope whose `P`
#: names neither is untouched by it.
_CARVE_PSEUDO = ("@recut", "@version")

#: The `ingest_events` node arm. It is emitted **unconditionally**, even when
#: no uid is new, because the builder reads only the log and cannot know which
#: uids existed. Widening, therefore sound.
_INGEST_NODE_PSEUDO = ("@identity", "@extent", "@event_key")

IdField = Union[_Top, tuple[str, ...]]


def _coarsen(values: Iterable[str]) -> IdField:
    """A deduplicated, deterministically ordered set — or `"*"` past the
    threshold. Never a scalar: see this module's docstring on CO-8."""
    out = tuple(sorted(set(values)))
    return TOP if len(out) > COARSEN_ABOVE else out


@dataclass(frozen=True, slots=True)
class Identity:
    """D13.20's `identity`: `{uid}` for a node, `{eid, src, dst, rel_type,
    disc}` for an edge. Every field is a set (or `"*"`), including the ones a
    single-identity op fills with one value.

    `multi` records whether the op *coarsened* — whether the wire form is a
    list because the op named many, or a scalar because it named one. It is a
    serialization concern only: `intersects` reads the tuples and never asks.
    """

    uid: IdField | None = None
    eid: IdField | None = None
    src: IdField | None = None
    dst: IdField | None = None
    rel_type: IdField | None = None
    disc: IdField | None = None
    multi: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("uid", "eid", "src", "dst", "rel_type", "disc"):
            value = getattr(self, name)
            if value is None:
                continue
            if value is TOP:
                out[name] = TOP_JSON
            elif self.multi:
                out[name] = list(value)
            else:
                # a single-identity op: D13.20's specimen and §13.6's witnesses
                # both render `{"uid": "A"}`, not `{"uid": ["A"]}`
                out[name] = value[0] if len(value) == 1 else list(value)
        return out


@dataclass(frozen=True, slots=True)
class OpFootprint:
    """One arm of one logged op. An op with a carve arm produces two of these,
    and D13.23 property 4 matches a scope term against **each separately** — a
    merged pseudo-footprint would either lose the carve arm's reach or lose the
    value arm's precision."""

    seq: int
    arm: Arm
    kind: str
    cls: str
    entity_kind: EntityKind
    identity: Identity
    rel_type: RelTypeSet
    vt: VtSet
    props: PropSet

    @property
    def eids(self) -> IdField | None:
        """The set an `edges`-arm target is tested against (D13.23, E-4(b)).
        `None` means the footprint carries no `eid` field at all, which E-4(b)
        makes a **wildcard** rather than a non-match."""
        return self.identity.eid

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise InvalidArgError(f"unknown op kind: {self.kind!r}", allowed=list(KINDS))
        if self.arm not in ("value", "carve"):
            raise InvalidArgError(f"unknown arm: {self.arm!r}")
        if self.entity_kind not in ("node", "edge"):
            raise InvalidArgError(f"unknown entity kind: {self.entity_kind!r}")
        if self.props is not TOP:
            bad = [p for p in self.props if p.startswith("@") and p not in PSEUDO_PROPS]
            if bad:
                raise InvalidArgError(f"unknown pseudo-property key(s): {bad}",
                                      allowed=list(PSEUDO_PROPS))

    def to_json(self) -> dict[str, Any]:
        out = {
            "seq": self.seq,
            "arm": self.arm,
            "kind": self.kind,
            "class": self.cls,
            "entity_kind": self.entity_kind,
            "identity": self.identity.to_json(),
            "vt": TOP_JSON if self.vt is TOP else [list(iv) for iv in self.vt],
            "props": TOP_JSON if self.props is TOP else list(self.props),
        }
        return out


@dataclass(frozen=True, slots=True)
class BatchFootprint:
    """D13.20's batch object. `tt` is the batch's transaction time — the
    coordinate `check` filters the suffix on — and `batch_id` is what makes a
    witness *checkable against the log* rather than trusted (D13.27)."""

    batch_id: str
    tt: int
    ops: tuple[OpFootprint, ...]

    def to_json(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "tt": self.tt,
                "ops": [o.to_json() for o in self.ops]}


# ---------------------------------------------------------------------------
# the builders
# ---------------------------------------------------------------------------

def footprints_of_batch(batch: dict[str, Any]) -> BatchFootprint:
    """One logged batch record — `{"batch_id", "tt", "ops"}` — to its
    footprints, in `seq` order, both arms."""
    ops: list[OpFootprint] = []
    for seq, op in enumerate(batch.get("ops", ())):
        ops.extend(footprints_of_op(op, seq))
    return BatchFootprint(batch_id=str(batch["batch_id"]), tt=int(batch["tt"]),
                          ops=tuple(ops))


def footprints_of_op(op: dict[str, Any], seq: int = 0) -> tuple[OpFootprint, ...]:
    """D13.22's table, one row per logged op kind.

    **The argument defaults here must match `apply_ops` exactly.** A builder
    that reads `op["vt_e"]` and raises, or defaults it differently, describes an
    op that was applied with different bounds — and a footprint narrower than
    the write it describes is a false-freshness source. The defaults are read
    off `tgms/storage/base.py`'s five appliers and are named at each site.
    """
    kind = op.get("op")
    if kind == "assert_node":
        return _assert_node(op, seq)
    if kind == "assert_edge":
        return _assert_edge(op, seq)
    if kind == "correct":
        return _correct(op, seq)
    if kind == "retract":
        return _retract(op, seq)
    if kind == "ingest_events":
        return _ingest_events(op, seq)
    raise InvalidArgError(f"unknown op kind: {kind!r}", allowed=list(KINDS))


def _carve_arm(value: OpFootprint) -> OpFootprint:
    """D13.21a: same `kind`, `entity_kind`, `identity` and `rel_type`;
    `vt = "*"`; `props = {@recut, @version}`.

    Emitted without knowing whether the op carved, because the log cannot say.
    Widening, therefore sound (D13.1).
    """
    return OpFootprint(seq=value.seq, arm="carve", kind=value.kind, cls=value.cls,
                       entity_kind=value.entity_kind, identity=value.identity,
                       rel_type=value.rel_type, vt=vt_carve(), props=_CARVE_PSEUDO)


def _vt_e_of(op: dict[str, Any]) -> int:
    """`_assert_node` / `_assert_edge` / `_correct` all read
    `op.get("vt_e", OPEN_END)`. Matching that default exactly is obligation 1."""
    value = op.get("vt_e", OPEN_END)
    return OPEN_END if value is None else int(value)


def _assert_node(op: dict[str, Any], seq: int) -> tuple[OpFootprint, ...]:
    """`props: "*"` on the value arm, **not** the asserted keys: an overwriting
    assert replaces a whole version, so keys the new props *omit* also change.
    `"*"` subsumes `@label`, `@identity`, `@extent` and `@event_key`; it does
    not thereby cover `@recut`/`@version` at an unbounded location — that is
    the carve arm's job, and they are two footprints precisely so the `vt`
    conjunct can differ."""
    value = OpFootprint(
        seq=seq, arm="value", kind="assert_node", cls=CLASS_ASSERT, entity_kind="node",
        identity=Identity(uid=(str(op["uid"]),)),
        # a node write has no relation type; `intersects` guards the conjunct on
        # entity kind and never reads this. TOP rather than () so that if the
        # guard were ever dropped the failure would widen, not narrow.
        rel_type=TOP,
        vt=vt_closed(int(op["vt_s"]), _vt_e_of(op)), props=TOP)
    return (value, _carve_arm(value))


def _assert_edge(op: dict[str, Any], seq: int) -> tuple[OpFootprint, ...]:
    """`eid` is **derived, not read**: `_ref_json` writes an edge ref as
    `{kind, src, dst, rel_type, disc}` and carries no `eid` at all. It is
    `edge_eid(src, dst, rel_type, disc)`, a pure function of four logged
    fields — which is the one place where "derivable from the log alone" needs
    a sentence rather than an assertion.

    `disc` defaults to `""` (`_assert_edge`), not to a generated value: that is
    `_ingest_events`' rule and using it here would key a different edge."""
    src, dst, rel_type = str(op["src"]), str(op["dst"]), str(op["rel_type"])
    disc = str(op.get("disc", ""))
    value = OpFootprint(
        seq=seq, arm="value", kind="assert_edge", cls=CLASS_ASSERT, entity_kind="edge",
        identity=Identity(eid=(edge_eid(src, dst, rel_type, disc),), src=(src,),
                          dst=(dst,), rel_type=(rel_type,), disc=(disc,)),
        rel_type=(rel_type,),
        vt=vt_closed(int(op["vt_s"]), _vt_e_of(op)), props=TOP)
    return (value, _carve_arm(value))


def _identity_of_ref(ref: dict[str, Any]) -> tuple[EntityKind, Identity, RelTypeSet]:
    """`retract` and `correct` both key off `op["ref"]`, and both take their
    entity kind from it — `_ref_from_op` reads `disc` with a `""` default."""
    kind = ref.get("kind")
    if kind == "node":
        return "node", Identity(uid=(str(ref["uid"]),)), TOP
    if kind == "edge":
        src, dst = str(ref["src"]), str(ref["dst"])
        rel_type, disc = str(ref["rel_type"]), str(ref.get("disc", ""))
        return "edge", Identity(eid=(edge_eid(src, dst, rel_type, disc),), src=(src,),
                                dst=(dst,), rel_type=(rel_type,), disc=(disc,)), (rel_type,)
    raise InvalidArgError(f"unknown ref kind: {kind!r}", allowed=["node", "edge"])


def _props_union(keys: Iterable[str], pseudo: tuple[str, ...]) -> tuple[str, ...]:
    """Real keys sorted, then the pseudo-keys in D13.22's own order. Order is
    presentation only — every consumer treats this as a set — but a
    deterministic one keeps a footprint dump diffable."""
    real = sorted({str(k) for k in keys})
    return tuple(real) + tuple(p for p in pseudo if p not in real)


def _correct(op: dict[str, Any], seq: int) -> tuple[OpFootprint, ...]:
    """A node `correct` emits `@label`, an edge `correct` does not.

    By L2.2 a multi-hit `correct` can change a label; omitting `@label` here
    would be a false-freshness source for every label-sensitive operator. Both
    emit `@event_key` (CE-5's channel) and `@extent` (coverage: the new version
    changes what is believed over its own interval).

    An event-keyed scope must **not** be narrowed to the properties it reads —
    which is why `keys(new_props)` is a *union* with the pseudo-keys, never a
    replacement for them."""
    entity_kind, identity, rel_type = _identity_of_ref(op["ref"])
    pseudo = _CORRECT_NODE_PSEUDO if entity_kind == "node" else _CORRECT_EDGE_PSEUDO
    value = OpFootprint(
        seq=seq, arm="value", kind="correct", cls="C", entity_kind=entity_kind,
        identity=identity, rel_type=rel_type,
        vt=vt_closed(int(op["vt_s"]), _vt_e_of(op)),
        props=_props_union(op.get("props", {}).keys(), pseudo))
    return (value, _carve_arm(value))


def _retract(op: dict[str, Any], seq: int) -> tuple[OpFootprint, ...]:
    """`vt_from(op["t"])` — `[t, OPEN_END)`, the footprint `Store.retract`
    already passes to `EvolutionMemory.mark_stale`.

    The value arm carries `{@extent, @event_key}` and **not** `"*"`: that is
    the narrowing D13.22 deliberately made when `@version` moved to the carve
    arm. A retract removes coverage over `[t, ∞)` and re-keys events; it does
    not rewrite arbitrary property values."""
    entity_kind, identity, rel_type = _identity_of_ref(op["ref"])
    value = OpFootprint(
        seq=seq, arm="value", kind="retract", cls="D", entity_kind=entity_kind,
        identity=identity, rel_type=rel_type,
        vt=vt_from(int(op["t"])), props=_RETRACT_PSEUDO)
    return (value, _carve_arm(value))


def _ingest_events(op: dict[str, Any], seq: int) -> tuple[OpFootprint, ...]:
    """Two footprints, **no carve arm**: `ingest_events` supersedes nothing,
    because every event without an explicit `disc` gets its batch offset as
    discriminator and is therefore its own logical edge (D2.1).

    The defaults are `_ingest_events`' own and are the fiddliest in the file:
    `vt_e` is `ev.get("vt_e") or vt_s + 1` — note **`or`**, so a falsy `0` also
    falls back — and `disc` is `ev.get("disc", f"#{op.get('offset', 0) + i}")`,
    which is why the enumeration index and the op's `offset` both matter.

    **The `eid` set is `FRESHNESS_SEMANTICS.md` erratum E-1**, which amends
    D13.22 and is marked BLOCKING. D13.22's table coarsens this arm's identity
    to endpoint sets and carries no `eid`, so a term whose `targets` has only
    an `edges` arm cannot match it — an absent arm is ∅ under D13.5, so the
    answer is *false*, and a false negative is the one direction D1.13 forbids.
    An event carrying an **explicit** `disc` addresses the logical edge that
    `disc` names, which may already exist, so the gap is reachable. No current
    derivation is eid-narrow, so it is latent rather than live — and E-1 says
    in as many words that latency is not a defence.

    E-1's ruled fix is what is implemented: `identity.eid` is the **set** of
    `edge_eid(...)` over the batch's events, coarsening to `"*"` above a size
    threshold. It only ever *adds* matches, so it is a widening (D13.1), and
    `meets(T.edges, "*")` holds, so the coarsened form is sound too.

    E-1 also records a real qualification of D13.20 that this code embodies:
    deriving `eid` needs the engine's **identity rule**, not just the log. That
    is why `edge_eid` is imported from `tgms.core.model` and not reimplemented
    here — a checker that disagreed with the engine about `eid` would produce
    false negatives silently.
    """
    events: list[dict[str, Any]] = list(op.get("events", ()))
    if not events:
        # `_ingest_events` over an empty list writes nothing at all, so there is
        # nothing to describe. Returning () rather than a vacuous footprint
        # keeps `intersects` from matching on `"*"`-shaped emptiness.
        return ()
    base = int(op.get("offset", 0))
    eids: list[str] = []
    srcs: list[str] = []
    dsts: list[str] = []
    rel_types: list[str] = []
    vt_s_min: int | None = None
    vt_e_max: int | None = None
    first_seen: dict[str, int] = {}
    for i, ev in enumerate(events):
        src, dst, rel_type = str(ev["src"]), str(ev["dst"]), str(ev["rel_type"])
        vt_s = int(ev["vt_s"])
        vt_e = int(ev.get("vt_e") or vt_s + 1)
        disc = str(ev.get("disc", f"#{base + i}"))
        eids.append(edge_eid(src, dst, rel_type, disc))
        srcs.append(src)
        dsts.append(dst)
        rel_types.append(rel_type)
        vt_s_min = vt_s if vt_s_min is None else min(vt_s_min, vt_s)
        vt_e_max = vt_e if vt_e_max is None else max(vt_e_max, vt_e)
        for u in (src, dst):
            if u not in first_seen or vt_s < first_seen[u]:
                first_seen[u] = vt_s

    rel_set = _coarsen(rel_types)
    edge_arm = OpFootprint(
        seq=seq, arm="value", kind="ingest_events", cls="A", entity_kind="edge",
        identity=Identity(eid=_coarsen(eids), src=_coarsen(srcs), dst=_coarsen(dsts),
                          rel_type=rel_set, multi=True),
        rel_type=rel_set,
        # the hull. Hulling a 50,000-event batch is widening, and D13.22 says
        # so. A log carrying an event whose explicit `vt_e` is at or below its
        # `vt_s` has no interval to hull — `_ingest_events` applies it anyway,
        # so refusing here would make `check` raise on a log the store accepted.
        # `"*"` is the widening that keeps the checker total.
        vt=(vt_closed(vt_s_min, vt_e_max)  # type: ignore[arg-type]
            if vt_s_min is not None and vt_e_max is not None and vt_s_min < vt_e_max
            else TOP),
        props=TOP)
    node_arm = OpFootprint(
        seq=seq, arm="value", kind="ingest_events", cls="A", entity_kind="node",
        identity=Identity(uid=_coarsen(first_seen), multi=True),
        rel_type=TOP,
        vt=vt_closed(min(first_seen.values()), OPEN_END),
        props=_INGEST_NODE_PSEUDO)
    return (edge_arm, node_arm)


__all__ = [
    "Arm", "BatchFootprint", "CLASS_ASSERT", "COARSEN_ABOVE", "EntityKind",
    "Identity", "OpFootprint", "footprints_of_batch", "footprints_of_op",
]
