"""`ArtifactRecord` — M5 design memo §1 (`docs/design/M5_DESIGN.md`).

An artifact's identity is the pair `(name, generation)`. `name` is a
caller-chosen logical string; `generation` is a monotone integer from 0.
Every content address — `plan_digest`, `node_digest`, `result_digest` — is a
**field inside** the record, never the key (§1.2's rejection of the
`ResultStore`/content-addressed alternative). Generations are append-only: a
refresh writes generation *g+1* carrying `supersedes: [name, g]`, and never
touches generation *g* (§1.1).

Mirrors `tgms/tgir/depscope.py:385-532`'s shape verbatim: a frozen, slotted
dataclass, `__post_init__` refusals for every structural invariant, canonical
JSON via `to_json`/`from_json`, and a schema name + version pair (§1.3
preamble — "a reader that does not recognize the version must refuse, never
report fresh"). As with `DependencyScope`, that discipline is **not**
enforced by refusing construction on an unrecognized `version` — a reader
must still be able to hold a record it disagrees with in order to refuse it
loudly later. The refusal lives in the consumer, `tgms.artifact.witness`,
exactly where `check.py:613-614` enforces `DependencyScope`'s own version
gate rather than `DependencyScope.__post_init__`.

This module is on `scripts/check_freshness_boundary.py`'s guarded allowlist
(§7.1): it may import `tgms.core.model`, `tgms.core.errors` and
`tgms.tgir.depscope`, and nothing else from `tgms`. In particular it may
**not** import `tgms.tgir.metadata`, `tgms.tgir.node`, `tgms.tgir.plan` or any
storage adapter — which is why `plan`, `basis`, `state`, `refresh` and
`payload` below are plain JSON dicts rather than the typed objects those
modules define: the record is a wire format, like `DependencyScope`, not a
consumer of the modules that produce its fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.core.model import canonical_json, digest
from tgms.tgir.depscope import DependencyScope, ScopeTerm

SCHEMA_NAME = "tgms-artifact"

#: An integer. A reader that does not recognize a record's version must
#: refuse it — enforced in `tgms.artifact.witness.check_artifact`, not here
#: (§1.3 preamble; mirrors `depscope.py:36-40`).
SCHEMA_VERSION = 1

#: §1.4's closed vocabulary for `refresh.kind` — unlike the artifact's own
#: `kind` (§3.4, deliberately open), this one is the record's own refresh
#: mechanism and the memo names exactly two values.
REFRESH_KINDS: tuple[str, ...] = ("tgir_plan", "operator")


def _check_relative_ref(where: str, ref: str) -> None:
    """§2.4 obligation 2: every `*_ref` is relative to the store root, never
    absolute. The writer refuses an absolute one rather than persisting a
    record whose `record_digest` would depend on where it happened to be
    written from."""
    if not ref:
        raise InvalidArgError(f"{where} must not be empty")
    if ref.startswith("/") or ref.startswith("\\") or (len(ref) > 1 and ref[1] == ":"):
        raise InvalidArgError(f"{where} must be relative to the store root, never absolute",
                              got=ref)


@dataclass(frozen=True, slots=True)
class ArtifactId:
    """`(name, generation)` — §1.1's key, used for `supersedes` and `parents`
    and as `ArtifactVerdict.artifact`."""

    name: str
    generation: int

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidArgError("an artifact id needs a name")
        if self.generation < 0:
            raise InvalidArgError(f"generation must be >= 0, got {self.generation}")

    def to_json(self) -> list[Any]:
        return [self.name, self.generation]

    @staticmethod
    def from_json(obj: Any) -> "ArtifactId":
        if not (isinstance(obj, (list, tuple)) and len(obj) == 2):
            raise InvalidArgError("an artifact id is a [name, generation] pair", got=obj)
        return ArtifactId(str(obj[0]), int(obj[1]))


@dataclass(frozen=True, slots=True)
class StepDependency:
    """One entry of `steps` (§1.3 rule 1): a step's own `DependencyScope`,
    plus its optional Level-1 `scan_region` (§4 — carried opaquely here;
    `tgms.artifact` does not interpret it, `tgms.tgir.level1` does)."""

    step_id: str
    dependency: DependencyScope
    scan_region: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.step_id:
            raise InvalidArgError("a step needs a step_id")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step_id": self.step_id, "dependency": self.dependency.to_json()}
        if self.scan_region is not None:
            out["scan_region"] = dict(self.scan_region)
        return out

    @staticmethod
    def from_json(obj: Any) -> "StepDependency":
        if not isinstance(obj, dict):
            raise InvalidArgError("a step must be an object", got=obj)
        sid = str(obj.get("step_id") or obj.get("id") or "")
        if not sid:
            raise InvalidArgError("a step needs a step_id")
        return StepDependency(sid, DependencyScope.from_json(obj["dependency"]),
                              obj.get("scan_region"))


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """§1.3's record. Canonical JSON, `SCHEMA_NAME = "tgms-artifact"`,
    `SCHEMA_VERSION = 1`.

    `registered_tt` is **derived**, not stored independently (§1.3 rule 5:
    "`registered_tt` is `basis.tt_q`, not a wall clock"). Storing it as a
    plain field that must merely *agree* with `basis["tt_q"]` would leave a
    disagreement reachable through `from_json` on a hand-edited record;
    deriving it instead makes the two values the same value, so there is
    nothing for a rewritten record to disagree with itself about. `to_json`
    still emits the key, so the on-disk shape matches §1.3's schema exactly.
    """

    name: str
    generation: int
    kind: str
    store: str
    plan: dict[str, Any]
    basis: dict[str, Any]
    state: dict[str, Any]
    refresh: dict[str, Any]
    steps: tuple[StepDependency, ...] = ()
    dependency: DependencyScope | None = None
    supersedes: ArtifactId | None = None
    parents: tuple[ArtifactId, ...] = ()
    payload: dict[str, Any] | None = None
    #: Genuinely non-deterministic bookkeeping (host, process) — **excluded
    #: from `record_digest`** (§1.3 rule 5), the same digest-exclusion
    #: discipline `execute.py:74`'s payload/`tgir` split already uses.
    provenance: dict[str, Any] | None = None
    schema: str = SCHEMA_NAME
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidArgError("an artifact record needs a name")
        if self.generation < 0:
            raise InvalidArgError(f"generation must be >= 0, got {self.generation}")
        if not self.kind:
            raise InvalidArgError("an artifact record needs a kind (§3.4: an open string)")
        if not self.store:
            raise InvalidArgError("an artifact record needs a store identity")

        # §1.1: generation 0 carries no `supersedes`; every later generation
        # points back to exactly the generation it replaces, on the same name.
        if self.generation == 0:
            if self.supersedes is not None:
                raise InvalidArgError("generation 0 carries no supersedes",
                                      got=self.supersedes.to_json())
        else:
            expected = ArtifactId(self.name, self.generation - 1)
            if self.supersedes != expected:
                raise InvalidArgError(
                    "supersedes must name this artifact's immediately prior generation",
                    expected=expected.to_json(),
                    got=(self.supersedes.to_json() if self.supersedes is not None else None))

        # §1.3 rule 1: steps is the production path; the merged `dependency`
        # is the fallback. At least one must be present — a record with
        # neither has no dependency scope to check at all.
        if not self.steps and self.dependency is None:
            raise InvalidArgError(
                "an artifact record needs a dependency scope: steps, the merged "
                "fallback, or both")

        # §1.3 rule 2: `store` is duplicated from the scope, and must agree —
        # refused at construction, the `⊎` precedent (depscope.py:465-468).
        for s in self.steps:
            if s.dependency.store != self.store:
                raise InvalidArgError(
                    "a step's dependency scope names a different store than the record",
                    step_id=s.step_id, record_store=self.store, step_store=s.dependency.store)
        if self.dependency is not None and self.dependency.store != self.store:
            raise InvalidArgError(
                "the merged dependency scope names a different store than the record",
                record_store=self.store, dependency_store=self.dependency.store)

        if "tt_q" not in self.basis:
            raise InvalidArgError("basis needs a tt_q — registered_tt is derived from it")

        if "completeness" not in self.state:
            raise InvalidArgError("state needs a completeness")

        refresh_kind = self.refresh.get("kind")
        if refresh_kind not in REFRESH_KINDS:
            raise InvalidArgError(f"unknown refresh.kind: {refresh_kind!r}",
                                  allowed=list(REFRESH_KINDS))
        if not self.refresh.get("ref"):
            raise InvalidArgError("refresh needs a ref")
        _check_relative_ref("refresh.ref", self.refresh["ref"])

        if self.plan.get("plan_ref"):
            _check_relative_ref("plan.plan_ref", self.plan["plan_ref"])
        if self.payload is not None and self.payload.get("result_ref"):
            _check_relative_ref("payload.result_ref", self.payload["result_ref"])

    # -- identity ----------------------------------------------------------

    @property
    def id(self) -> ArtifactId:
        return ArtifactId(self.name, self.generation)

    @property
    def registered_tt(self) -> int:
        """§1.3 rule 5 — always `basis.tt_q`, never independently stored."""
        return int(self.basis["tt_q"])

    # -- terms (the §3.2 walk's raw material) -------------------------------

    def all_terms(self) -> tuple[ScopeTerm, ...]:
        """Every `ScopeTerm` this record carries, across every step plus the
        merged fallback. This is deliberately coarser than what
        `check_artifact` evaluates (which is per-step) — it exists for
        `lookup.py`'s pre-filter, where "could this batch possibly matter to
        this artifact at all" is the only question being asked."""
        out: list[ScopeTerm] = []
        for s in self.steps:
            out.extend(s.dependency.terms)
        if self.dependency is not None:
            out.extend(self.dependency.terms)
        return tuple(out)

    # -- serialization -------------------------------------------------------

    def _canonical_fields(self) -> dict[str, Any]:
        """Every field except `record_digest` itself and `provenance`
        (§1.3 rule 5's digest-exclusion). Used both to compute the digest and
        as the base of `to_json`."""
        out: dict[str, Any] = {
            "schema": self.schema,
            "version": self.version,
            "name": self.name,
            "generation": self.generation,
            "kind": self.kind,
            "store": self.store,
            "plan": dict(self.plan),
            "basis": dict(self.basis),
            "state": dict(self.state),
            "refresh": dict(self.refresh),
            "registered_tt": self.registered_tt,
        }
        if self.supersedes is not None:
            out["supersedes"] = self.supersedes.to_json()
        if self.steps:
            out["steps"] = [s.to_json() for s in self.steps]
        if self.dependency is not None:
            out["dependency"] = self.dependency.to_json()
        if self.parents:
            out["parents"] = [p.to_json() for p in self.parents]
        if self.payload is not None:
            out["payload"] = dict(self.payload)
        return out

    @property
    def record_digest(self) -> str:
        """§1.2(c): the record's own content address, over its canonical JSON
        minus itself and minus `provenance`. A replay recomputes this and
        compares, which is what detects a rewritten generation by the same
        mechanism `check` uses for a rewritten log."""
        return digest(self._canonical_fields())

    def to_json(self) -> dict[str, Any]:
        out = self._canonical_fields()
        if self.provenance is not None:
            out["provenance"] = dict(self.provenance)
        out["record_digest"] = self.record_digest
        return out

    def canonical(self) -> str:
        return canonical_json(self.to_json())

    @staticmethod
    def from_json(obj: Any) -> "ArtifactRecord":
        if not isinstance(obj, dict):
            raise InvalidArgError("an artifact record must be an object", got=type(obj).__name__)
        if obj.get("schema") != SCHEMA_NAME:
            raise InvalidArgError(f"not a {SCHEMA_NAME} object", got=obj.get("schema"))
        steps = tuple(StepDependency.from_json(s) for s in obj.get("steps") or ())
        dependency = (DependencyScope.from_json(obj["dependency"])
                     if obj.get("dependency") is not None else None)
        supersedes = (ArtifactId.from_json(obj["supersedes"])
                     if obj.get("supersedes") is not None else None)
        parents = tuple(ArtifactId.from_json(p) for p in obj.get("parents") or ())
        payload = dict(obj["payload"]) if obj.get("payload") is not None else None
        provenance = dict(obj["provenance"]) if obj.get("provenance") is not None else None
        # `registered_tt` in `obj` is not read back — it is re-derived from
        # `basis["tt_q"]` (the `registered_tt` property above), so a
        # hand-edited disagreement between the two is simply not
        # representable in the reconstructed object.
        return ArtifactRecord(
            name=str(obj["name"]), generation=int(obj["generation"]), kind=str(obj["kind"]),
            store=str(obj["store"]), plan=dict(obj["plan"]), basis=dict(obj["basis"]),
            state=dict(obj["state"]), refresh=dict(obj["refresh"]), steps=steps,
            dependency=dependency, supersedes=supersedes, parents=parents, payload=payload,
            provenance=provenance, schema=str(obj.get("schema", SCHEMA_NAME)),
            version=int(obj.get("version", SCHEMA_VERSION)),
        )


__all__ = [
    "REFRESH_KINDS", "SCHEMA_NAME", "SCHEMA_VERSION", "ArtifactId", "ArtifactRecord",
    "StepDependency",
]
