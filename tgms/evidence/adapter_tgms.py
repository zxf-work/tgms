"""The TGMS evidence adapter — operator envelopes become ECQRs.

The adapter side of the Gate A layering: it may use any TGMS-specific
knowledge to *produce* capabilities (here: `rows_total` is computed before
pagination by every registry operator, D-030's count discipline, so it
certifies the complete logical result's cardinality; operators execute
atomically, so a returned envelope implies execution completeness). The
generic verifier consumes only the resulting capabilities.

Capability inheritance across plan steps: a step whose inputs were
delivery-incomplete loses `delivery_complete` and any cardinality
certificate — a certificate must never be laundered through a derivation
whose inputs were partial (the executor separately refuses reducers over
truncated pages; this is the same rule at the capability layer).

Basis compatibility across plan steps (Def. basis compatibility):
`step_basis` decides, before a step runs, whether its input bases fit the
operator's declaration and which basis its output carries.

- A store-reading operator with a pinned `as_of_tt` is basis selection:
  it ignores its input bases and stamps the pinned basis it read.
- Every other step is same-state. The store-free `compute` must see one
  basis across its inputs and inherits it (a count over a pinned read is
  pinned at that read's transaction time). A store-reading operator at
  the current basis reads Current(store, token) itself, so its inputs
  must carry that same current basis.
- Declared cross-basis operators: none. The registry has no operator
  that reads two distinct bases; `compute`'s `diff` subtracts two
  scalars and is same-state, and `diff_snapshots` compares two valid
  times under one `as_of_tt`.
"""

from __future__ import annotations

from typing import Any, Iterable

import tgms
from tgms.core.model import OPEN_END
from tgms.evidence.ecqr import ECQR, Basis, Scope

#: pagination arguments are not part of the logical query domain
_NON_DOMAIN_ARGS = ("limit", "cursor")

#: registry operators that never read the store: their output describes
#: the state their inputs were read at
STORE_FREE_OPS = frozenset({"compute"})


class BasisMismatch(ValueError):
    """A step's input bases violate its operator's declaration."""

    def __init__(self, op: str, bases: list[Basis]) -> None:
        self.op = op
        self.bases = bases
        super().__init__(
            f"{op} is a same-state operator, but its inputs were read at "
            f"{len({_label(b) for b in bases})} distinct bases "
            f"({', '.join(sorted({_label(b) for b in bases}))}); read every "
            f"input at one as_of_tt, or omit as_of_tt on all of them")


def _label(b: Basis) -> str:
    return (f"Pinned(tt={b.as_of_tt})" if b.pinned
            else f"Current({b.execution_context})")


def read_basis(store_id: str, as_of: int,
               execution_context: str | None) -> Basis:
    """The basis a store read at `as_of` observes."""
    return Basis(store=store_id, as_of_tt=as_of, pinned=as_of != OPEN_END,
                 execution_context=(None if as_of != OPEN_END
                                    else execution_context))


def step_basis(op: str, args: dict[str, Any], input_bases: Iterable[Basis],
               store_id: str, execution_context: str | None) -> Basis:
    """Output basis of one plan step; raises BasisMismatch when the
    inputs violate the operator's declaration."""
    as_of = args.get("as_of_tt", OPEN_END)
    if op not in STORE_FREE_OPS and as_of != OPEN_END:
        return read_basis(store_id, as_of, execution_context)
    bases = list(input_bases)
    if op not in STORE_FREE_OPS:
        bases.append(read_basis(store_id, OPEN_END, execution_context))
    if not bases:
        return read_basis(store_id, OPEN_END, execution_context)
    if any(b != bases[0] for b in bases[1:]):
        raise BasisMismatch(op, bases)
    return bases[0]


def build_ecqr(envelope: dict[str, Any], store_id: str,
               input_ecqrs: list[ECQR] | None = None,
               execution_context: str | None = None,
               basis: Basis | None = None) -> ECQR:
    """Descriptor for one successful operator envelope. `basis`, when
    given, is the step's basis from `step_basis`; otherwise the basis is
    the envelope's own read."""
    if "error" in envelope:
        raise ValueError("failed calls produce outcome certificates, "
                         "not ECQRs")
    args = dict(envelope.get("args_echo", {}))
    as_of = args.get("as_of_tt", OPEN_END)
    domain = {"op": envelope.get("op")}
    domain.update({k: v for k, v in args.items()
                   if k not in _NON_DOMAIN_ARGS})

    inputs_complete = all(e.scope.delivery_complete
                          for e in (input_ecqrs or []))
    truncated = bool(envelope.get("truncated", False))
    rows = envelope.get("rows")
    rows_returned = len(rows) if isinstance(rows, list) else None

    cardinality = envelope.get("rows_total")
    if not isinstance(cardinality, int) or not inputs_complete:
        # no laundering: a count derived from incomplete inputs is not a
        # certificate for the logical result over the declared domain
        cardinality = None

    return ECQR(
        result_id=str(envelope.get("result_digest", "")),
        basis=(basis if basis is not None
               else read_basis(store_id, as_of, execution_context)),
        scope=Scope(domain=domain,
                    execution_complete=True,  # registry ops are atomic
                    delivery_complete=(not truncated) and inputs_complete,
                    rows_returned=rows_returned,
                    exact_cardinality=cardinality),
        exactness="exact",
        provenance={"op": envelope.get("op"),
                    "inputs": [e.result_id for e in (input_ecqrs or [])]},
        semantics={"engine": "tgms", "version": tgms.__version__},
    )
