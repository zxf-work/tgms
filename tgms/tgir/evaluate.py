"""Evaluating a TGIR node — M2.2's opaque-leaf path.

For an `OpaqueLeaf` this calls **exactly what runs today**:
`REGISTRY[op].fn(adapter, bound_args)`. The kernel is not wrapped, not
subclassed, not proxied, and not edited — M2 rule 1.3 forbids touching an
operator kernel body, and this module is the reason it does not need to. What
the leaf adds is *around* the call: the `∅`-kernel guard before it, and the
result-metadata tuple after it.

The compositional core has no evaluator here. `NodeScan`, `Expand` and the rest
are M3's; asking for one is a loud `NotImplementedError` rather than a silent
empty relation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.core.errors import InternalError
from tgms.tgir.guard import adapter_for
from tgms.tgir.metadata import (
    Completeness, Exactness, Provenance, ResultMeta, ScanDescriptor,
)
from tgms.tgir.node import Node, OpaqueLeaf

#: §5.4's `semantic_identity` (`ν`): operator, registry, engine and
#: canonicalization versions. "v1 encoding: one package version pins the whole
#: vector."
def semantic_identity() -> str:
    from tgms import __version__

    return f"tgms/{__version__}"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A node's result: the kernel's own payload, and the node that produced it.

    **The metadata is computed on request, never eagerly.** `node_digest` is a
    content digest over the bound arguments, so building provenance for a
    `compute` call carrying 20 000 input rows canonical-JSONs and hashes all
    20 000 — measured at 3.3 ms → 30.8 ms for exactly that call when it was
    built eagerly. Every operator call goes through here, and most callers
    never look at the metadata, so the work belongs behind the accessor. (M2
    plan §9.8 names this hazard: "construct the scope lazily and serialize
    once".)
    """

    payload: dict[str, Any]
    leaf: OpaqueLeaf

    def meta(self, dependency: Any = None) -> ResultMeta:
        return leaf_meta(self.leaf, self.payload, dependency)

    def meta_json(self) -> dict[str, Any]:
        return meta_json(self.leaf, self.payload)


def evaluate(node: Node, adapter: Any, **kwargs: Any) -> Evaluation:
    """Dispatch an **opaque leaf** to its runtime.

    The compositional core evaluates to a `Relation` rather than to a payload
    envelope, so it has its own entry point: `tgms.tgir.eval.evaluate_core`.
    Keeping them separate is deliberate — a leaf's result is the kernel's own
    payload dict (C4 makes its field names the contract), while a core plan's
    result is an ordered bag of typed rows that only becomes a payload at the
    plan's output boundary.
    """
    if isinstance(node, OpaqueLeaf):
        return evaluate_leaf(node, adapter, **kwargs)
    from tgms.tgir.eval import PENDING

    if node.op in PENDING:
        raise NotImplementedError(
            f"{node.op} has no evaluator yet — it is {PENDING[node.op]}'s "
            f"(docs/design/M3_IMPLEMENTATION_PLAN.md §4.1)")
    raise NotImplementedError(
        f"{node.op} is a compositional core node: evaluate it with "
        f"`tgms.tgir.eval.evaluate_core`, which returns a Relation")


def evaluate_leaf(leaf: OpaqueLeaf, adapter: Any) -> Evaluation:
    """Run one opaque leaf.

    The `∅`-kernel guard goes live here: a leaf classified `∅` receives a
    `NullAdapter`, so the classification is a **checkable property** rather than
    a comment — a misclassified kernel fails loudly at its first read instead of
    rotting into silent unsoundness (§2.0's obligation 6).
    """
    from tgms.temporal.algebra import REGISTRY  # local import: avoid cycle

    spec = REGISTRY.get(leaf.op)
    if spec is None:
        raise InternalError(f"no registry entry for opaque leaf {leaf.op!r}")
    payload = spec.fn(adapter_for(leaf, adapter), leaf.args)
    return Evaluation(payload, leaf)


def meta_json(leaf: OpaqueLeaf, payload: dict[str, Any]) -> dict[str, Any]:
    """The rest of `R` (§5) as one JSON sub-object: `completeness`,
    `exactness`, `provenance`, `schema` and `(T_v, T_b)`, plus the plan digest.

    `dependency` is dropped here because it is carried flat beside `tt_q`
    (D13.16's placement) and one copy is enough; `tt_q` itself is likewise flat.
    """
    out = leaf_meta(leaf, payload).to_json()
    out.pop("dependency", None)
    # a single-leaf plan is its leaf: the plan digest and the node digest
    # coincide, and both are content-addressed over (op, args, Σ, inputs)
    out["node_digest"] = leaf.node_digest
    out["plan_digest"] = leaf.node_digest
    return out


def meta_for(op: str, filled_args: dict[str, Any], payload: dict[str, Any],
             out_fields: tuple[str, ...]) -> dict[str, Any]:
    """`meta_json` for a call whose leaf was not kept — the executor's path,
    which reconstructs the leaf from the envelope's own `args_echo` (the filled
    args) rather than re-validating them."""
    from tgms.tgir.leaf import build_leaf

    return meta_json(build_leaf(op, filled_args, out_fields), payload)


def leaf_meta(leaf: OpaqueLeaf, payload: dict[str, Any],
              dependency: Any = None) -> ResultMeta:
    return ResultMeta(
        sigma=leaf.sigma,
        completeness=leaf_completeness(leaf, payload),
        exactness=Exactness.EXACT,
        provenance=leaf_provenance(leaf),
        dependency=dependency,
        schema=leaf.out_schema,
    )


def leaf_completeness(leaf: OpaqueLeaf, payload: dict[str, Any]) -> Completeness:
    """§11.11's ruling: **`unknown` for every leaf**, except where the
    operator's own envelope already proves better.

    `unknown` asserts the *absence of certification*, never positive
    incompleteness (§5.2) — and it is the only honest value here, because
    mapping `truncated = False` to `complete` would manufacture a certification
    no leaf currently supports: none of the fifteen has been audited for
    *execution* completeness, and they carry only `truncated` plus `*_total`
    counts today.

    The one upgrade the envelope does prove is **`paginated`**: a truncated
    result that hands back a cursor has incomplete *delivery* and complete
    execution, which is exactly what `paginated` says. Three operators truncate
    with **no cursor to recover from it** — `diff_snapshots`,
    `neighborhood_evolution`, and `snapshot_subgraph`'s node list — and
    `paginated` does not describe those, so they stay `unknown` rather than
    borrowing a certification about recoverability that is not true of them.
    """
    if payload.get("truncated") and payload.get("cursor") is not None:
        return Completeness.PAGINATED
    return Completeness.UNKNOWN


def leaf_provenance(leaf: OpaqueLeaf) -> Provenance:
    """§5.4 at **descriptor granularity**, which is the ruled level.

    A whole-store scan's vid set is unbounded, so v1 records exact `vid`s only
    for point reads and a scan descriptor `(kind, rel_types, Σ, belief mode,
    endpoint restriction)` otherwise. Provenance and dependency stay
    independent concerns: provenance describes rows that were **actually
    read**, a scope describes a region **future writes might land in**, so
    sharpening one would not sharpen the other.
    """
    return Provenance(
        node_digest=leaf.node_digest,
        op=leaf.op,
        canonical_args=leaf.canonical_args()["bound_args"],
        input_digests=(),          # a leaf has no inputs
        source_versions=_source_versions(leaf),
        semantic_identity=semantic_identity(),
    )


def _source_versions(leaf: OpaqueLeaf) -> tuple[Any, ...]:
    """One scan descriptor per leaf — or none at all for an `∅` leaf, which
    reads no store versions by classification."""
    if not leaf.reads_store:
        return ()
    args = leaf.args
    rel_types = args.get("rel_types")
    if isinstance(rel_types, str):
        rel_types = (rel_types,)
    elif isinstance(rel_types, list):
        rel_types = tuple(rel_types)
    else:
        rel_types = None
    return (ScanDescriptor(
        kind="opaque",              # the leaf does not disclose its scan mix
        sigma=leaf.sigma,
        rel_types=rel_types,
        belief="current",           # every leaf reads believed versions at T_b
        endpoints=_endpoints(args),
    ),)


def _endpoints(args: dict[str, Any]) -> dict[str, Any] | None:
    """The endpoint restriction a leaf's bound args pin, when they pin one.

    Deliberately syntactic: it reports what the *arguments* restrict, not what
    the kernel does with them, because a descriptor that guessed at kernel
    behaviour would be a claim this layer cannot support.
    """
    out: dict[str, Any] = {}
    for key in ("uid", "src", "dst", "seeds", "endpoint_filter"):
        value = args.get(key)
        if value is not None:
            out[key] = value
    return out or None


__all__ = [
    "Evaluation", "evaluate", "evaluate_leaf", "leaf_completeness", "leaf_meta",
    "leaf_provenance", "meta_for", "meta_json", "semantic_identity",
]
