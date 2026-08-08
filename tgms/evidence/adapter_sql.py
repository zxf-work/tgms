"""The SQL evidence adapter — direct SQL executions become ECQRs.

The portability half of the Gate A layering: a SQL backend produces the
same capabilities through its own mechanisms. An unlimited `COUNT(*)`
over the same predicate certifies `@ExactCardinality` the way a TGMS
operator's pre-pagination `rows_total` does; a query executed without
`LIMIT` — or whose delivered page equals the certified count — is
delivery-complete. The generic verifier cannot tell which backend
produced a descriptor; that is the point (the M3 exit gate), and trust
assumption A2 (the adapter truthfully constructs what it emits) is
carried by the caller supplying honest inputs: `total_count` must come
from an unlimited count over the *same* predicate and basis, never from
a page.
"""

from __future__ import annotations

from typing import Any

from tgms.core.model import OPEN_END, canonical_json, sha256_hex
from tgms.evidence.ecqr import ECQR, Basis, Scope


def build_sql_ecqr(*, rows: list[Any], sql: str,
                   params: list[Any] | None = None,
                   store_id: str, as_of_tt: int = OPEN_END,
                   engine: str = "duckdb", engine_version: str = "",
                   total_count: int | None = None,
                   limited: bool = False,
                   input_ecqrs: list[ECQR] | None = None) -> ECQR:
    """Descriptor for one completed SQL query execution."""
    inputs_complete = all(e.scope.delivery_complete
                          for e in (input_ecqrs or []))
    delivered = len(rows)
    delivery_complete = inputs_complete and (
        not limited or (total_count is not None and delivered == total_count))
    cardinality = total_count if (isinstance(total_count, int)
                                  and inputs_complete) else None
    return ECQR(
        result_id=sha256_hex(canonical_json(
            {"rows": rows, "sql": sql, "params": params or []})),
        basis=Basis(store=store_id, as_of_tt=as_of_tt,
                    pinned=as_of_tt != OPEN_END),
        scope=Scope(domain={"sql": " ".join(sql.split()),
                            "params": params or []},
                    execution_complete=True,  # a completed statement
                    delivery_complete=delivery_complete,
                    rows_returned=delivered,
                    exact_cardinality=cardinality),
        exactness="exact",
        provenance={"engine": engine,
                    "inputs": [e.result_id for e in (input_ecqrs or [])]},
        semantics={"engine": engine, "version": engine_version,
                   "canonicalization": "tgms-canonical-json-1"},
    )
