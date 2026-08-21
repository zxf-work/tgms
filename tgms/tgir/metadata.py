"""The result-metadata tuple `R` minus `value`/`schema` (TGIR_SPEC §5).

```
R = (value, schema, T_v, T_b, completeness, exactness, provenance, dependency)
```

carried at **every** node, not only at the plan's root. `dependency` and `tt_q`
are **digest-excluded** (§5.4's coordinator ruling, FRESHNESS_SEMANTICS D13.16):
a digest compares *values*, and freshness metadata is bookkeeping — including
either would make two byte-identical pinned results digest differently purely
because they ran at different moments, breaking §3.6 outright.

The completeness order of §5.2.1 is the only non-obvious thing here, and it is
the reason this module exists rather than a dict of strings: §5.3's rule 3 takes
a **meet**, and the meet of two of the four middle values must be `unknown`
rather than an arbitrary winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.core.model import digest
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.types import Schema, Sigma


class Completeness(str, Enum):
    """§5.2's enum, ordered by §5.2.1's lattice:

    ```
                           complete                    ⊤  fully certified
                              |
          +--------+----------+----------+--------+
          |        |          |          |        |
       top-k   paginated   sampled   timeout-truncated   pairwise incomparable
          |        |          |          |
          +--------+----------+----------+--------+
                              |
                           unknown                    asserts no certification
                              |
                           refused                    ⊥  no result at all
    ```

    `unknown` **asserts the absence of certification, never positive
    incompleteness**, which is why it sits above `refused` and below every
    positive claim. `refused` is a real node-level state because refusals happen
    *mid-plan*: §2.8's `left_outer`/`anti` and §2.10's `Aggregate` refuse on an
    input that is not execution-complete, and truncation is not knowable before
    execution. Without it, a trace step for a refused node could not be written
    — and such a node still contributes its dependency scope.
    """

    COMPLETE = "complete"
    TOP_K = "top-k"
    PAGINATED = "paginated"
    SAMPLED = "sampled"
    TIMEOUT_TRUNCATED = "timeout-truncated"
    UNKNOWN = "unknown"
    REFUSED = "refused"

    def __str__(self) -> str:  # pragma: no cover - repr sugar
        return self.value


#: The four partial certifications. They are **pairwise incomparable**: a
#: `paginated` result is not better or worse than a `top-k` one — they are
#: certified along different axes (delivery vs domain). Forcing a total order
#: here would manufacture a certification no input supports.
MIDDLE: frozenset[Completeness] = frozenset({
    Completeness.TOP_K, Completeness.PAGINATED,
    Completeness.SAMPLED, Completeness.TIMEOUT_TRUNCATED,
})

#: The certification-layer surjection of §5.1. EVIDENCE_MODEL v1.0.1 stays
#: frozen; TGIR's enum is the internal propagation vocabulary, and this is the
#: map from the first to the second, recorded so no implementation invents it.
CERTIFICATION_LAYER: dict[Completeness, str] = {
    Completeness.COMPLETE: "certified-complete",
    Completeness.TOP_K: "uncertified",
    Completeness.PAGINATED: "uncertified",
    Completeness.SAMPLED: "uncertified",
    Completeness.TIMEOUT_TRUNCATED: "uncertified",
    Completeness.UNKNOWN: "uncertified",
    Completeness.REFUSED: "none",  # verify() is defined only on Executed
}


def le(a: Completeness, b: Completeness) -> bool:
    """The partial order: `a ≤ b` ("a certifies no more than b"). `refused` is
    below everything; `unknown` below everything but `refused`; the four middle
    values are below `complete` and incomparable with each other."""
    if a is b:
        return True
    if a is Completeness.REFUSED:
        return True
    if b is Completeness.REFUSED:
        return False
    if a is Completeness.UNKNOWN:
        return True
    if b is Completeness.UNKNOWN:
        return False
    if b is Completeness.COMPLETE:
        return True
    return False  # a is complete, or two distinct middles


def comparable(a: Completeness, b: Completeness) -> bool:
    return le(a, b) or le(b, a)


def meet(a: Completeness, b: Completeness) -> Completeness:
    """The greatest lower bound — §5.3 rule 3's operation.

    **No operator's completeness is ever stronger than its inputs'**, with the
    single stated exception of an `Aggregate` whose declared domain is its
    input's narrowed domain (a domain fall, not an enum raise). `refused` is ⊥
    and **absorbing**: any operator with a refused input is itself refused,
    since there is nothing to compute over. Two distinct middle values meet at
    `unknown`, which is the honest answer — an operator combining a truncated
    page with a top-k selection can certify neither property.
    """
    if a is b:
        return a
    if a is Completeness.REFUSED or b is Completeness.REFUSED:
        return Completeness.REFUSED
    if a is Completeness.COMPLETE:
        return b
    if b is Completeness.COMPLETE:
        return a
    return Completeness.UNKNOWN


def meet_all(values: Any) -> Completeness:
    """The meet over a sequence. Empty meets at `complete`, the lattice's ⊤ —
    the identity, and the right answer for a *leaf*, which has no inputs and no
    meet to take (§5.3's two scan rows)."""
    out = Completeness.COMPLETE
    for v in values:
        out = meet(out, v)
    return out


class Exactness(str, Enum):
    """§5.2: `exact` above the three reserved non-exact constructors.

    **Exactness in v1 is `exact` or refused.** `approximate(bound, confidence)`,
    `sampled(rate)` and `bounded(lo, hi)` are *reserved* constructors so an
    AQP-style backend can join without a model change; the implementation
    encodes only `exact`.
    """

    EXACT = "exact"
    APPROXIMATE = "approximate"
    SAMPLED = "sampled"
    BOUNDED = "bounded"

    def __str__(self) -> str:  # pragma: no cover - repr sugar
        return self.value


def meet_exactness(a: Exactness, b: Exactness) -> Exactness:
    """"An operator's output is `exact` iff every input is `exact` and the
    operator introduces no approximation" (§5.2).

    The order §5.2.1 gives is `exact` above the three reserved constructors and
    says nothing about how two *distinct* reserved constructors compare. Since
    v1 encodes only `exact`, that case is unreachable, and this raises rather
    than inventing a winner — the same discipline `Completeness` gets from
    `unknown`, which the exactness enum has no counterpart for.
    """
    if a is b:
        return a
    if a is Exactness.EXACT:
        return b
    if b is Exactness.EXACT:
        return a
    raise InvalidArgError(
        "the meet of two distinct non-exact constructors is under-determined by §5.2.1; "
        "v1 encodes only `exact`, so this combination should be unreachable",
        left=a.value, right=b.value)


# ---------------------------------------------------------------------------
# §5.4 — provenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScanDescriptor:
    """`(kind, rel_types, Σ, belief mode, endpoint restriction)` — the
    descriptor form of `source_versions` for anything that is not a point read,
    because a whole-store scan's vid set is unbounded (§5.4)."""

    kind: str
    sigma: Sigma
    rel_types: tuple[str, ...] | None = None
    belief: str = "current"
    endpoints: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "sigma": self.sigma.to_json(),
                "rel_types": list(self.rel_types) if self.rel_types else None,
                "belief": self.belief, "endpoints": self.endpoints}


@dataclass(frozen=True, slots=True)
class VidSet:
    """Exact `vid`s — v1 records these **for point reads** (a bounded `uids`
    scan, an anchored `NodeScan`) and a `ScanDescriptor` otherwise."""

    vids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"vids": list(self.vids)}


@dataclass(frozen=True, slots=True)
class Provenance:
    """`(node_digest, op, canonical_args, input_digests, source_versions,
    semantic_identity)` (§5.4).

    **Provenance and dependency are independent concerns** (C3 ruling): the
    dependency scope is *not* derived from `source_versions` and needs no vid
    sets. Provenance describes rows that were **actually read**; a scope
    describes a region **future writes might land in**, and must cover regions
    containing no rows at all — an empty result has an empty vid set and a
    non-empty scope. Sharpening provenance would not sharpen freshness.
    """

    node_digest: str
    op: str
    canonical_args: dict[str, Any] = field(default_factory=dict)
    input_digests: tuple[str, ...] = ()
    source_versions: tuple[Any, ...] = ()
    semantic_identity: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "node_digest": self.node_digest,
            "op": self.op,
            "canonical_args": self.canonical_args,
            "input_digests": list(self.input_digests),
            "source_versions": [s.to_json() if hasattr(s, "to_json") else s
                                for s in self.source_versions],
            "semantic_identity": self.semantic_identity,
        }


def compute_node_digest(op: str, canonical_args: dict[str, Any], sigma: Sigma,
                        input_digests: tuple[str, ...] = ()) -> str:
    """§5.4's Merkle digest of the plan subtree: a content digest over `(op,
    canonical args with parameters bound, Σ, the input nodes' digests)`. Every
    component is data — which is why an opaque leaf may not hold a kernel
    callable."""
    return digest({"op": op, "args": canonical_args, "sigma": sigma.to_json(),
                   "inputs": list(input_digests)})


@dataclass(frozen=True, slots=True)
class ResultMeta:
    """`R` minus `value` and `schema` — plus `schema` itself, which is cheap to
    carry and is what makes a step record self-describing.

    `completeness` and `exactness` are tracked **independently**: a result may
    be `top-k` and `exact`, or `complete` and `approximate`.
    """

    sigma: Sigma
    completeness: Completeness = Completeness.UNKNOWN
    exactness: Exactness = Exactness.EXACT
    provenance: Provenance | None = None
    dependency: DependencyScope | None = None
    schema: Schema | None = None

    @property
    def t_v(self) -> list[list[int]]:
        return self.sigma.vt_json()

    @property
    def t_b(self) -> int:
        return self.sigma.t_b

    @property
    def certification(self) -> str:
        return CERTIFICATION_LAYER[self.completeness]

    def to_json(self) -> dict[str, Any]:
        """The envelope projection. Every key here is **digest-excluded by
        construction** — it goes on the envelope, never into the kernel's
        `payload`, which is what `digest()` covers (§6.3 of the M2 plan)."""
        return {
            "t_v": self.t_v,
            "t_b": self.t_b,
            "completeness": self.completeness.value,
            "exactness": self.exactness.value,
            "provenance": self.provenance.to_json() if self.provenance else None,
            "dependency": self.dependency.to_json() if self.dependency else None,
            "schema": self.schema.to_json() if self.schema else None,
        }


__all__ = [
    "CERTIFICATION_LAYER", "Completeness", "Exactness", "MIDDLE", "Provenance",
    "ResultMeta", "ScanDescriptor", "VidSet", "comparable", "compute_node_digest",
    "le", "meet", "meet_all", "meet_exactness",
]
