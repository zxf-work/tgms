"""`check_artifact` — the witness / refresh API, M5 design memo §5.

`check_artifact(record, log, tt_now=OPEN_END, *, chain_cache=None,
level1=True) -> ArtifactVerdict`. It reuses `check_steps` verbatim (via
`check_trace`, which already implements exactly the per-step-production /
merged-fallback selection §5.2 describes and calls `check_steps` unchanged —
duplicating that selection here would just be a second place for the two
to drift apart); it renders through `explain.render_steps`; it returns the
affected dependency *term*, a refresh handle, and — mandatorily —
D-153's exemption receipt. It exposes `.actionable_fresh` and no `is_stale`
(§5.6 — this module must never grow one).

Every `refresh.kind` / `RefreshHandle.kind` occurrence below names §5.5's own
closed `"tgir_plan" | "operator"` vocabulary for the record's refresh
mechanism — unrelated to `ArtifactRecord.kind`, §3.4's deliberately open,
never-branched-on classification of what an artifact *is*. This module never
reads `record.kind`; the two-different-things-sharing-a-word is the memo's
own naming, not a boundary this file crosses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.core.model import OPEN_END
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import (
    WITNESS_CAP, ChainCache, StepsVerdict, UNDECIDABLE, Witness, check_trace,
)
from tgms.tgir.explain import render_steps

from tgms.artifact.record import SCHEMA_VERSION, ArtifactId, ArtifactRecord

#: §1.4's known `plan_format` values. `witness.py` cannot import
#: `tgms.tgir.loader` — it is outside `scripts/check_freshness_boundary.py`'s
#: allowlist for this module (§7.1) — so the recognized set is restated here
#: rather than imported. Duplicates `tgms/tgir/loader.py:42`'s `PLAN_FORMAT`;
#: keep the two in sync by hand until a version 2 exists to test the seam.
KNOWN_PLAN_FORMATS: frozenset[int] = frozenset({1})


@dataclass(frozen=True, slots=True)
class TermRef:
    """§5.3 — a `Witness.matched_term` index, resolved to the actual term.

    `level` disaggregates a Level-0 witness from a scan-region-derived one.
    `tgms.tgir.level1` (P1.3) does not exist in this tree yet, so every
    `TermRef` this module produces is `"level-0"` — see `check_artifact`'s
    `level1` parameter below.
    """

    step_id: str | None
    index: int
    term: dict[str, Any]
    matched_on: tuple[str, ...]
    level: str

    def to_json(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "index": self.index, "term": self.term,
                "matched_on": list(self.matched_on), "level": self.level}


@dataclass(frozen=True, slots=True)
class ExemptReceipt:
    """D-153's mandatory receipt, one per step that exempted at least one
    batch (`check.py`'s `Verdict.exempt`, carried through verbatim)."""

    step_id: str | None
    basis: int | None
    batches: int
    tt_range: tuple[int | None, int | None]
    theorem: str

    def to_json(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "basis": self.basis, "batches": self.batches,
                "tt_range": list(self.tt_range), "theorem": self.theorem}


@dataclass(frozen=True, slots=True)
class RefreshHandle:
    """§5.5 — constructed and returned, never executed. `check_artifact`
    does not call it, does not import `tgms.artifact.refresh`, and does not
    open a store."""

    artifact: ArtifactId
    kind: str            # "tgir_plan" | "operator" — §1.4/§5.5's vocabulary
    ref: str
    plan_format: int | None
    basis_policy: str

    def to_json(self) -> dict[str, Any]:
        return {"artifact": self.artifact.to_json(), "kind": self.kind, "ref": self.ref,
                "plan_format": self.plan_format, "basis_policy": self.basis_policy}


@dataclass(frozen=True, slots=True)
class ArtifactVerdict:
    """§5.2's shape."""

    artifact: ArtifactId
    steps: StepsVerdict
    terms: tuple[TermRef, ...]
    exempt: tuple[ExemptReceipt, ...]
    refresh: RefreshHandle | None

    @property
    def actionable_fresh(self) -> bool:
        """Mirrors `check.py:381-385` / `StepsVerdict.actionable_fresh`. The
        **only** affirmative question — this type grows no `is_stale`."""
        return self.steps.actionable_fresh

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"artifact": self.artifact.to_json(), **self.steps.to_json()}
        if self.terms:
            out["terms"] = [t.to_json() for t in self.terms]
        # §5.4: printed even when empty would be misleading either way, so
        # the mandatory receipt is emitted whenever it is non-empty and the
        # verdict is otherwise silent about it — the CLI (§5.4 point 2)
        # is what prints it unconditionally, including on FRESH.
        if self.exempt:
            out["exempt"] = [e.to_json() for e in self.exempt]
        if self.refresh is not None:
            out["refresh"] = self.refresh.to_json()
        return out


def _term_map(record: ArtifactRecord) -> dict[str | None, tuple[dict[str, Any], ...]]:
    """The same per-step-production/merged-fallback selection `check_trace`
    (`check.py:852-864`) makes, restated only far enough to know which
    term list a given `step_id` resolves against — `check_trace` itself
    does not hand back that mapping, and rebuilding the verdict just to get
    it would be wasteful."""
    if record.steps:
        return {s.step_id: tuple(s.dependency.to_json()["terms"]) for s in record.steps}
    if record.dependency is not None:
        return {"plan": tuple(record.dependency.to_json()["terms"])}
    return {}


def _resolve(term_map: dict[str | None, tuple[dict[str, Any], ...]], w: Witness) -> TermRef:
    terms = term_map.get(w.step_id, ())
    term = terms[w.matched_term] if 0 <= w.matched_term < len(terms) else {}
    # Every term this module resolves came straight off the stored scope —
    # no `tgms.tgir.level1` refinement has run (see `check_artifact` below).
    return TermRef(step_id=w.step_id, index=w.matched_term, term=term,
                   matched_on=w.matched_on, level="level-0")


def _refresh_handle(record: ArtifactRecord) -> RefreshHandle | None:
    """§1.4 / §5.5: an unrecognized `plan_format` refuses the refresh and
    leaves the verdict untouched — `handle` is `None`, nothing else changes."""
    refresh_kind = record.refresh["kind"]
    ref = record.refresh["ref"]
    basis_policy = record.refresh.get("basis_policy", "open")
    if refresh_kind == "operator":
        # an opaque-leaf artifact's (op, bound_args) document — no loader.py
        # version concept applies (§1.4).
        return RefreshHandle(record.id, refresh_kind, ref, None, basis_policy)
    plan_format = record.plan.get("plan_format")
    if plan_format not in KNOWN_PLAN_FORMATS:
        return None
    return RefreshHandle(record.id, refresh_kind, ref, int(plan_format), basis_policy)


def check_artifact(record: ArtifactRecord, log: EventLog, tt_now: int = OPEN_END, *,
                   chain_cache: ChainCache | None = None, level1: bool = True,
                   witness_cap: int = WITNESS_CAP) -> ArtifactVerdict:
    """§5.1. Reuses `check_steps` verbatim (through `check_trace`); renders
    through `explain.render_steps`; returns the affected dependency terms, a
    non-executing refresh handle, and D-153's exemption receipt — mandatory,
    present-but-empty when nothing was exempted.

    **`level1`, sequencing fail-safe (flagged — analogous to §5.4's D-153
    fail-safe, but for P1.3).** §5.1 decides the signature takes a `level1`
    flag; P1.3's `tgms.tgir.level1` module (§6.2) does not exist in this
    tree. Rather than import a module that is not there, or guess at an API
    no written proof has fixed yet, this parameter is accepted for forward
    compatibility and is currently a no-op: every witness and term this
    function returns is Level-0. §4.3 licenses exactly this direction —
    "an absent scan region means no narrowing" — so refining nothing is
    always sound, never false-fresh, regardless of what the caller passed.
    The moment `tgms.tgir.level1` lands, this is the one seam that needs to
    change.

    A record whose own schema `version` is unrecognized is refused outright
    (§1.3's "a reader that does not recognize the version must refuse, never
    report fresh"), mirroring `check.py`'s own step 1 rather than
    `ArtifactRecord`'s constructor — exactly where `DependencyScope`'s
    version gate lives too.
    """
    if record.version != SCHEMA_VERSION:
        steps_verdict = StepsVerdict((("record", UNDECIDABLE("scope-version")),))
    else:
        steps_verdict = check_trace(record.to_json(), log, tt_now,
                                    chain_cache=chain_cache, witness_cap=witness_cap)

    term_map = _term_map(record) if record.version == SCHEMA_VERSION else {}
    terms = tuple(_resolve(term_map, w) for w in steps_verdict.witnesses)
    exempt = tuple(
        ExemptReceipt(step_id=sid, basis=v.exempt["basis"], batches=v.exempt["batches"],
                     tt_range=(v.exempt["tt_range"][0], v.exempt["tt_range"][1]),
                     theorem=v.exempt["theorem"])
        for sid, v in steps_verdict.per_step if v.exempt is not None
    )
    refresh = _refresh_handle(record) if record.version == SCHEMA_VERSION else None
    return ArtifactVerdict(record.id, steps_verdict, terms, exempt, refresh)


def render_verdict(verdict: ArtifactVerdict, *, produced_tt: int | None = None) -> str:
    """§5.6: `render_steps` reused unchanged, plus one line naming the
    artifact and its generation, plus one line per exemption receipt."""
    lines = [f"{verdict.artifact.name}@{verdict.artifact.generation}",
             render_steps(verdict.steps, produced_tt=produced_tt)]
    for e in verdict.exempt:
        step = f" (step {e.step_id})" if e.step_id else ""
        lines.append(f"exempt{step}: basis={e.basis} batches={e.batches} "
                     f"tt_range={list(e.tt_range)} theorem={e.theorem}")
    return "\n".join(lines)


__all__ = [
    "KNOWN_PLAN_FORMATS", "ArtifactVerdict", "ExemptReceipt", "RefreshHandle", "TermRef",
    "check_artifact", "render_verdict",
]
