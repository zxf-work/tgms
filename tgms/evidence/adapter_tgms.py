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
"""

from __future__ import annotations

from typing import Any

import tgms
from tgms.core.model import OPEN_END
from tgms.evidence.ecqr import ECQR, Basis, Scope

#: pagination arguments are not part of the logical query domain
_NON_DOMAIN_ARGS = ("limit", "cursor")


def build_ecqr(envelope: dict[str, Any], store_id: str,
               input_ecqrs: list[ECQR] | None = None,
               execution_context: str | None = None) -> ECQR:
    """Descriptor for one successful operator envelope."""
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
        basis=Basis(store=store_id, as_of_tt=as_of,
                    pinned=as_of != OPEN_END,
                    execution_context=(None if as_of != OPEN_END
                                       else execution_context)),
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
