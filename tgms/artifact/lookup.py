"""Correction -> artifact inverted lookup — M5 design memo §3.

v1 walks every registered scope per correction batch, reusing `intersects`
unchanged:

    affected(batch, registry) =
        fps = footprints_of_batch(batch)
        [ a for a in registry.current_generations()
            if any(intersects(term, fp)[0] is HIT
                   for fp in fps.ops for term in a.terms) ]

The walk direction is the good one for the log: one batch is held and every
registered artifact is tested, so the whole-log chain walk that dominates a
per-artifact check is not paid per artifact — that is the reason to invert
at all (§3.2). The replacement is a pre-filter index that may only
over-approximate (§3.3, not built here — v1 ships the walk; the index ships
"when the campaign measures the wall, not before").

**Registration stays general over dependent artifacts (§3.4).** This module
keys on a record's `store`, `basis` and `terms` only — three things every
dependent artifact has, whatever it holds, whatever a caller chose to call
it. Nothing here branches on that open-ended label, and that promise is
checked mechanically: a one-line CI grep asserts this file never spells the
four-letter word that names it (the word this sentence is, itself, carefully
not using either).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from tgms.tgir.check import Match, intersects
from tgms.tgir.footprint import footprints_of_batch

from tgms.artifact.record import ArtifactRecord
from tgms.artifact.registry import Registry


@dataclass(frozen=True, slots=True)
class LookupResult:
    """One batch's answer, plus the two instrumentation counters §3.2 names
    for the campaign record: how many `intersects` calls the walk cost, and
    how many candidates survived it."""

    affected: tuple[ArtifactRecord, ...]
    intersects_calls: int
    candidate_survivors: int


def affected(batch: dict[str, Any], registry: Registry) -> LookupResult:
    """§3.2's walk, over one logged batch and a registry's current
    generations.

    Deliberately the coarse population-level test, not the precise per-step
    verdict `tgms.artifact.witness.check_artifact` computes: `record.all_terms()`
    flattens every step's terms plus the merged fallback, with no attention to
    `tt_q` or step attribution, because the only question this function
    answers is "should `check_artifact` be asked about this one at all" —
    an over-approximation is exactly what a pre-filter is for, and every
    survivor is still adjudicated properly downstream.
    """
    fps = footprints_of_batch(batch)
    survivors: list[ArtifactRecord] = []
    calls = 0
    for record in registry.current_generations():
        terms = record.all_terms()
        hit = False
        for fp in fps.ops:
            for term in terms:
                calls += 1
                m, _ = intersects(term, fp)
                if m is Match.HIT:
                    hit = True
                    break
            if hit:
                break
        if hit:
            survivors.append(record)
    return LookupResult(tuple(survivors), calls, len(survivors))


def affected_over(batches: Iterable[dict[str, Any]],
                  registry: Registry) -> Iterable[LookupResult]:
    """The walk applied to a correction stream, one `LookupResult` per
    batch — the shape a P1.5 campaign folds `intersects_calls` and
    `candidate_survivors` out of."""
    for batch in batches:
        yield affected(batch, registry)


__all__ = ["LookupResult", "affected", "affected_over"]
