"""The ECQR descriptor — an Evidence-Carrying Query Result's conditions.

Field names are neutral (basis / scope / exactness / provenance /
semantics); the acronym appears only in the schema identifier, so a later
renaming is cosmetic (plan v4 §2 resolution 4). A descriptor records the
conditions under which a result was produced; whether those conditions
support a particular claim is the verifier's question, never a field here
— capabilities, not taints (Gate A constraint 5).

Known v1 limitation, stated: an unpinned basis (`as_of_tt` = the
current-beliefs sentinel) identifies "the store as of execution", not a
replayable snapshot id; the engine does not yet expose its generation id
in operator envelopes. Historical-basis obligations therefore require a
*pinned* basis, and the M7 live-basis experiments will need the
generation id surfaced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "ECQR"
VERSION = "0.1"


@dataclass
class Basis:
    store: str
    as_of_tt: int
    pinned: bool  # False = current-beliefs sentinel: identified, not replayable


@dataclass
class Scope:
    domain: dict[str, Any]          # the logical query: op + non-pagination args
    execution_complete: bool
    delivery_complete: bool
    rows_returned: int | None = None
    #: backend-certified cardinality of the COMPLETE logical result over
    #: `domain` — the load-bearing capability: delivery incompleteness never
    #: invalidates it, execution incompleteness must prevent its issuance
    #: (Gate A constraint 1). None = no certificate.
    exact_cardinality: int | None = None


@dataclass
class ECQR:
    result_id: str                   # content digest over canonical result bytes
    basis: Basis
    scope: Scope
    exactness: str = "exact"         # v1 backends are exact or refused
    provenance: dict[str, Any] = field(default_factory=dict)
    semantics: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema"], d["version"] = SCHEMA, VERSION
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ECQR:
        return cls(result_id=d["result_id"],
                   basis=Basis(**d["basis"]), scope=Scope(**d["scope"]),
                   exactness=d.get("exactness", "exact"),
                   provenance=d.get("provenance", {}),
                   semantics=d.get("semantics", {}))
