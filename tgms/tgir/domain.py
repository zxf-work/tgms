"""The **declared domain** — what a result is *about* (§5.2, §5.3, §2.12).

"The declared domain is the window, filters, predicates, seeds and ranking keys
that define what the result is *about*. Completeness claims are always relative
to it, never to the world. Every `Filter`, `PropertyPredicate`, `TypeConstraint`
and `Limit{top-k}` narrows it, **and the narrowing is recorded**."

It had no representation in the code, and three separate rules depend on it
(§9.10). The one that makes it non-optional is §5.3 rule 3's single stated
exception: an `Aggregate` whose declared domain **is** its input's narrowed
domain outputs `complete` over that narrowed domain. Without a domain record
that sentence cannot be implemented — and the gate review's RG-2 verification
turns on exactly the difference between *is* and *is contained in*:

> counting the ten rows of a `top-k` input and certifying "these are exactly the
> 10 greatest under ⟨key⟩" is sound; certifying "these are exactly the person's
> messages" is not, **and the domain is what distinguishes them**.

So the guard is domain **equality**, not implication, and `test_tgir_eval_meta`
pins the abuse case.

Shape: `(Σ, [(node_digest, kind, canonical_args)])` — append-only along the
plan, digest-excluded like the rest of the M2 envelope metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.tgir.types import Sigma


@dataclass(frozen=True, slots=True)
class Narrowing:
    """One recorded restriction of the declared domain."""

    node_digest: str
    kind: str
    canonical_args: str

    def to_json(self) -> dict[str, Any]:
        return {"node_digest": self.node_digest, "kind": self.kind,
                "args": self.canonical_args}


@dataclass(frozen=True, slots=True)
class Domain:
    """`(Σ, [narrowing])`. Two domains are equal iff they were narrowed by the
    same nodes in the same order under the same Σ — which is what makes the
    `Aggregate` exception checkable rather than assertable."""

    sigma: Sigma
    narrowings: tuple[Narrowing, ...] = ()

    @staticmethod
    def of(sigma: Sigma) -> "Domain":
        return Domain(sigma, ())

    def narrow(self, node: Any, kind: str) -> "Domain":
        """Append one narrowing. **Append-only**: a domain never widens along a
        plan, because no operator widens what an answer is about."""
        from tgms.core.model import canonical_json

        return Domain(self.sigma, self.narrowings + (
            Narrowing(node.node_digest, kind,
                      canonical_json(node.canonical_args())),))

    @property
    def is_narrowed(self) -> bool:
        return bool(self.narrowings)

    def to_json(self) -> dict[str, Any]:
        return {"sigma": self.sigma.to_json(),
                "narrowings": [n.to_json() for n in self.narrowings]}


__all__ = ["Domain", "Narrowing"]
