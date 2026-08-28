"""The parent-edge recheck — `docs/design/M5_EXECUTION_PLAN_2026-08-27.md`
§5 P2.2 (the two-hop propagation demo); `docs/design/M5_DESIGN.md` §1.3's
`parents` field ("the dependency edge") and §3.4's registration generality.

M5_DESIGN.md never wrote a design for this walk — `parents` was landed as a
field on `ArtifactRecord` (§1.3) and threaded through `Registry.register`
and `refresh._publish` (P1.2/P2.1), but nothing yet reads it. P2.2's job is
that read.

**The question this module answers, and the one it does not.**
`tgms.artifact.lookup.affected` answers "does a correction batch threaten
this artifact's own base scope?" by walking `intersects` over logged ops.
This module answers a different question, one hop downstream in the
dependency graph instead of one batch wide in the log: "artifact `X` just
moved to a new generation — which *other* registrants named `X` among their
own `parents` at a generation `X` has since left behind, and therefore need
reconsidering?" Neither question subsumes the other, which is exactly §5.6's
propagation demo requirement: a registrant can be threatened via the parent
edge with **zero** batches intersecting its own scope, and it can also be
threatened by its own scope with a parent that has not moved at all. A full
recheck of a registrant asks both questions independently — this module
answers only the second, so a caller (`scripts/demo_propagation.py`, the
future CLI) is expected to also call `check_artifact` for the first.

**Where the answer comes from — the registry chain, nothing else.** A
registrant's own `parents` tuple is a snapshot: `[("A", 0)]` records that,
*at registration time*, `"A"`'s current generation was 0. Whether that
snapshot is stale is decided the same way every other supersession question
in this package is decided (§1.1's "the fold, never a stored flag"):
`registry.current("A").generation > 0`. There is no wall-clock comparison, no
reliance on registration order, and no assumption that the caller's own
`refreshed` argument is still the live current generation by the time this
runs — the comparison always re-reads `registry.current(parent.name)`, so a
`refreshed` id that has itself since been superseded again (a second,
unrelated refresh of the same name, racing this call) does not produce a
false negative: the live fold is asked fresh, every time.

**One level, caller-driven, no cascade — by design, matching `refresh.py`'s
own posture (§2.2, §5.5-5.6).** `parent_recheck` walks exactly one edge: the
direct children of the one name it is asked about. It does not recurse into
*their* children, and it does not call `check_artifact` or `refresh` on
anyone it finds. A caller wanting a second hop (B's own children, once B is
refreshed) calls this module again with B's new id — the same shape
`tgms.artifact.refresh.refresh` takes one artifact and one handle per call
rather than chasing a graph on its own account. This is the file's whole
scope: finding the *edge*, not walking it, not deciding what to do about it,
and not proving anyone stale (the honesty clause, §5.6, applies here exactly
as it does to `check_artifact`: `parent_recheck` names a **necessary**
condition for reconsideration — the parent moved — never a sufficient one).

**On the guarded allowlist (`scripts/check_freshness_boundary.py`).** This
module reads only `Registry` (an in-memory fold of `artifacts.jsonl`) and
`ArtifactRecord`/`ArtifactId`. It opens no store, no event log, and calls
none of `tgms.tgir.check`'s machinery — it is, if anything, a narrower claim
than `lookup.py`'s ("runs against a log it did not produce"): this one runs
against nothing but the registry's own fold. §7.1's per-module allowlist
pattern is followed verbatim, joining as a sibling of `record.py` /
`registry.py` / `lookup.py` / `witness.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.artifact.record import ArtifactId, ArtifactRecord
from tgms.artifact.registry import Registry

#: The one reason this module ever names. Kept as a named constant, the
#: same closed-taxonomy discipline `refresh.py`'s `REFUSAL_REASONS` and
#: `check.py`'s `REASONS` use, even though today there is exactly one member
#: — a second reason (e.g. "parent-refused") would be a deliberate addition
#: here, not a string that quietly appears.
REASON_PARENT_GENERATION_ADVANCED = "parent-generation-advanced"

REASONS: tuple[str, ...] = (REASON_PARENT_GENERATION_ADVANCED,)


@dataclass(frozen=True, slots=True)
class ParentThreat:
    """One reason a registrant needs reconsidering: it recorded `parent` (a
    `(name, generation)` pair) in its own `parents` at registration time, and
    `parent.name`'s registry-current generation, `parent_current`, has since
    moved past it — `parent_current.generation > parent.generation`, checked
    against the registry's live fold, never against `parent`'s own recorded
    generation being "the same one that was just refreshed"."""

    parent: ArtifactId
    parent_current: ArtifactId
    reason: str = REASON_PARENT_GENERATION_ADVANCED

    def to_json(self) -> dict[str, Any]:
        return {"parent": list(self.parent.to_json()),
                "parent_current": list(self.parent_current.to_json()),
                "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RecheckCandidate:
    """One registrant flagged for recheck, plus every reason it was flagged.
    `record` is that registrant's own current generation (`lookup.py`'s same
    population, `registry.current_generations()`) — the thing a caller would
    hand to `check_artifact` and, if it decides to act, to `refresh`."""

    record: ArtifactRecord
    threats: tuple[ParentThreat, ...]

    def to_json(self) -> dict[str, Any]:
        return {"artifact": list(self.record.id.to_json()),
                "threats": [t.to_json() for t in self.threats]}


@dataclass(frozen=True, slots=True)
class PropagationResult:
    """`parent_recheck`'s answer: every current-generation registrant that
    names the walked artifact among its `parents` at a generation the
    registry has since left behind, in `registry.current_generations()`
    order (deterministic — the same order `lookup.affected` iterates)."""

    candidates: tuple[RecheckCandidate, ...]

    def to_json(self) -> dict[str, Any]:
        return {"candidates": [c.to_json() for c in self.candidates]}

    def __bool__(self) -> bool:
        return bool(self.candidates)

    def __iter__(self):
        return iter(self.candidates)


def parent_recheck(refreshed: ArtifactId, registry: Registry) -> PropagationResult:
    """Walk one level: every current-generation registrant whose `parents`
    names `refreshed.name` at a generation strictly older than that name's
    live current generation in `registry`.

    `refreshed.generation` is not read — only `refreshed.name` selects which
    parent edge to walk. The generation comparison always re-reads
    `registry.current(parent.name)`, which is what makes the answer a
    property of the registry's own fold rather than of whichever generation
    the caller happened to be holding (module docstring, "where the answer
    comes from").
    """
    candidates: list[RecheckCandidate] = []
    for record in registry.current_generations():
        threats: list[ParentThreat] = []
        for p in record.parents:
            if p.name != refreshed.name:
                continue
            parent_current = registry.current(p.name)
            if parent_current is not None and parent_current.generation > p.generation:
                threats.append(ParentThreat(parent=p, parent_current=parent_current.id))
        if threats:
            candidates.append(RecheckCandidate(record, tuple(threats)))
    return PropagationResult(tuple(candidates))


__all__ = [
    "REASON_PARENT_GENERATION_ADVANCED", "REASONS", "ParentThreat", "PropagationResult",
    "RecheckCandidate", "parent_recheck",
]
