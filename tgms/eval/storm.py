"""The Correction Storm driver — Lane C, tasks C1 ("perf_counter"
instrumentation + a timed global-recompute arm) and C3 (the two coarse
invalidation controls), per the frozen design memo
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`.

This module builds a small artifact population over a store (§3), replays
correction batches from the existing M4/M5 generator
(`tgms.eval.corrections`, taxonomy/placement axes only — a `mix` hook is the
C2 extension point), and scores six arms against the oracle every batch
(§5): `global-recompute`, two new naive baselines (`entity-touch`,
`window-overlap`), the M4 `row-touch` baseline, and the two real TGMS arms
(`tgms-L0` static, `tgms-L1` dynamic/carve). C2's rate/age axes and C5's DAG
generator are **not** built here (§9 says they come later); the `mix` hook on
`Storm.__init__` and the `parents=()` default on artifact registration are
the extension points those tasks land on without changing this file's shape.

**Timings never enter any digest.** `tgms/artifact/{record,registry,refresh,
lookup,witness}.py` are untouched by this module — §6 of the design says
"`perf_counter` goes in the benchmark", and every timing value below is
computed by wrapping an unmodified call from *outside* it
(`time.perf_counter()` before and after `affected()`, `check_artifact()`,
`refresh()`) and returned in this module's own dataclasses
(`ArmOutcome`/`BatchResult`), never written onto an `ArtifactRecord` or into
the registry. Since nothing in the artifact package changed, the record
digest chain is trivially identical whether or not a caller collects timings
— `tests/test_storm.py::test_timing_does_not_affect_digest_chain` asserts it
directly rather than assuming it from "we didn't touch those files".

**One execution pass per batch, not `len(arms)` of them.** A batch could in
principle be scored by asking each arm to independently refresh only the
artifacts it nominates, timing each arm's own refresh calls separately. At
N up to 10^4 that is `O(arms * N)` re-executions per batch for a number that
`O(N)` already answers: `refresh()` on an `"operator"`-kind record IS an
independent full re-execution (`refresh._run_operator` re-invokes the same
`ToolRouter`), so one pass that refreshes **every** registered artifact
(§5's mandatory `global-recompute` arm) simultaneously (a) times the
global-recompute arm for real, (b) supplies the oracle's `changed` verdict
per artifact (compare the freshly published `result_digest` against the
value this harness last observed for that name), and (c) supplies a real,
per-artifact refresh wall-clock cost. Every other arm's nominated set is
then a **subset selection** over that same per-artifact cost table — the
wall-clock cost of refreshing artifact X does not depend on which policy
decided to do it, only on X's own op — so `Σ refresh ms` for a coarse or
`tgms-*` arm is the sum of the *same* measured per-artifact costs over only
the names that arm flagged. This is a deliberate, documented modeling choice
(flagged again in the module's own report) that keeps the harness at
`O(N)` re-executions per batch, the scale-up §7 requires.

**Time-to-fresh (§5).** Measured from the instant `store._write` returns to
the instant the last oracle-changed artifact has been refreshed and
verified fresh. This harness runs single-threaded and serially, so that
duration is exactly `check_wall_ms(arm) + Σ refresh_ms(arm)` over the
artifacts the arm nominates — no separate "verified fresh" step is needed
for the nominated set because a `refresh()` publish IS a fresh execution.
If an arm has a false-fresh in this batch (an oracle-changed artifact it
did *not* nominate), that artifact is never reached under this arm's
policy at all, and TTF is recorded as `None` rather than a number that
would misrepresent an unreached artifact as "restored" (§5's falsification
discipline, (d)).

**The naive baselines vs the real arms.** `entity-touch` and
`window-overlap` are, like `row-touch`, deliberately naive: they read only
what *this harness itself* recorded about a registration's own query
args (which uids it named, what window it read) — never `DependencyScope`.
The two `tgms-*` arms are the only ones that go through the real
`tgms.artifact.lookup.affected()` pre-filter and
`tgms.artifact.witness.check_artifact()` — the mechanism under test.
"""

from __future__ import annotations

import fractions
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import tgms
from tgms.core.errors import TgmsError
from tgms.core.model import OPEN_END, canonical_json
from tgms.eval.corrections import (
    Correction, GENERATORS, Substrate, Target, generate as generate_corrections,
    probe_substrate,
    # `_believed_nodes`/`_version_for` are private to `corrections.py`, and
    # imported here deliberately rather than restated: the C2 design memo
    # (`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md` §2, §4's opening)
    # names `_version_for` by name as the mechanism the age axis is meant to
    # build over, because `generate()`'s own outside-window `_interval` caps
    # Δvt at one `step` (`rng.randrange(step)`) and cannot express "days" or
    # "deep-historical" distances at all — see `_age_correction` below.
    _believed_nodes, _version_for,
)
from tgms.storage.base import make_op
from tgms.storage.eventlog import extend_chain
from tgms.temporal.algebra import ENVELOPE_META_FIELDS, ensure_all_registered
from tgms.tgir.depscope import DependencyScope

from tgms.artifact.lookup import affected
from tgms.artifact.record import ArtifactId
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.artifact.witness import RefreshHandle, check_artifact

ensure_all_registered()

#: §5's six arms, in the frozen order.
ARMS: tuple[str, ...] = (
    "global-recompute", "entity-touch", "window-overlap", "row-touch",
    "tgms-L0", "tgms-L1",
)

#: §3's "four window fractions of the substrate span".
WINDOW_FRACTIONS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5)

#: `resolve_entities` is excluded from the M4/M5 workload by ruling
#: (§13.8.1, restated at `scripts/bench_freshness.py:79-92`) — its only
#: appearance anywhere is a constructed counterexample (CE-6), not a
#: workload sample. Continuity, not a new decision: the storm population
#: reuses the same 13 measured operators plus the `compute` ∅-scope control,
#: 14 of the memo's 15-operator catalogue (§3).
OperatorTemplate = Callable[[Substrate, str, str, str, float, random.Random],
                            tuple[str, dict[str, Any], "frozenset[str]", "tuple[int, int] | None"]]


def _frac_window(sub: Substrate, frac: float, rng: random.Random) -> tuple[int, int]:
    lo, hi = sub.vt_lo, sub.vt_hi
    span = max(1, hi - lo)
    width = max(2, int(span * frac))
    start = lo + rng.randrange(max(1, span - width))
    return start, min(hi, start + width)


def _tmpl_entity_history(sub, uid, uid2, rel, frac, rng):
    return ("entity_history", {"uid": uid, "include_edges": True},
            frozenset({uid}), None)


def _tmpl_version_history(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("version_history", {"kind": "node", "window": {"t_a": a, "t_b": b}},
            frozenset(), (a, b))


def _tmpl_snapshot_subgraph(sub, uid, uid2, rel, frac, rng):
    t_valid = sub.vt_lo + int((sub.vt_hi - sub.vt_lo) * frac)
    return ("snapshot_subgraph", {"seeds": [uid], "t_valid": t_valid, "hops": 1},
            frozenset({uid}), (t_valid, t_valid + 1))


def _tmpl_diff_snapshots(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("diff_snapshots", {"t1": a, "t2": max(a + 1, b)}, frozenset(), (a, b))


def _tmpl_neighborhood_evolution(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("neighborhood_evolution",
            {"uid": uid, "t1": a, "t2": b, "stride": max(1, (b - a) // 8)},
            frozenset({uid}), (a, b))


def _tmpl_aggregate_events(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("aggregate_events",
            {"group_by": [{"dim": "endpoint", "role": "src"}],
             "aggregates": [{"agg": "count"}], "window": {"t_a": a, "t_b": b}},
            frozenset(), (a, b))


def _tmpl_graph_metric_timeseries(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("graph_metric_timeseries",
            {"metric": "edge_event_count", "window": {"t_a": a, "t_b": b},
             "stride": max(1, (b - a) // 8)}, frozenset(), (a, b))


def _tmpl_burst_detection(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("burst_detection",
            {"target": {"kind": "node_activity", "uid": uid}, "window": {"t_a": a, "t_b": b},
             "stride": max(1, (b - a) // 16)}, frozenset({uid}), (a, b))


def _tmpl_co_active(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("co_active",
            {"a_spec": {"rel_type": rel}, "b_spec": {"rel_type": rel},
             "allen_relation": {"relation": "before", "gap": max(1, (b - a) // 20)},
             "limit": 50}, frozenset(), None)


def _tmpl_count_temporal_motifs(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("count_temporal_motifs",
            {"motif": "M_2node_pingpong", "window": {"t_a": a, "t_b": b},
             "delta": max(1, (b - a) // 8)}, frozenset(), (a, b))


def _tmpl_find_temporal_motif_instances(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("find_temporal_motif_instances",
            {"motif": "M_2node_pingpong", "window": {"t_a": a, "t_b": b},
             "delta": max(1, (b - a) // 8), "limit": 25}, frozenset(), (a, b))


def _tmpl_temporal_reachability(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("temporal_reachability", {"src": uid, "window": {"t_a": a, "t_b": b}},
            frozenset({uid}), (a, b))


def _tmpl_temporal_paths(sub, uid, uid2, rel, frac, rng):
    a, b = _frac_window(sub, frac, rng)
    return ("temporal_paths", {"src": uid, "dst": uid2, "window": {"t_a": a, "t_b": b}, "k": 2},
            frozenset({uid, uid2}), (a, b))


def _tmpl_compute(sub, uid, uid2, rel, frac, rng):
    #: The (empty)-scope control (M4 tradition, `bench_freshness.py` Cell
    #: "literal-count"): a `compute` over literals is FRESH forever. Not a
    #: bug that it never appears in `oracle_changed` — it is the harness's
    #: own true-negative anchor.
    return ("compute", {"fn": "count", "input": [{"x": 1}, {"x": 2}]}, frozenset(), None)


TEMPLATES: tuple[OperatorTemplate, ...] = (
    _tmpl_entity_history, _tmpl_version_history, _tmpl_snapshot_subgraph,
    _tmpl_diff_snapshots, _tmpl_neighborhood_evolution, _tmpl_aggregate_events,
    _tmpl_graph_metric_timeseries, _tmpl_burst_detection, _tmpl_co_active,
    _tmpl_count_temporal_motifs, _tmpl_find_temporal_motif_instances,
    _tmpl_temporal_reachability, _tmpl_temporal_paths, _tmpl_compute,
)


# ---------------------------------------------------------------------------
# bookkeeping the harness keeps *outside* the registry (§6: never a record
# field, never digested)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ArtifactMeta:
    """What this harness itself knows about one registration's own query —
    the naive baselines' whole "scope". Never touches `DependencyScope`."""

    op: str
    args: dict[str, Any]
    entities: "frozenset[str]"
    window: tuple[int, int] | None


@dataclass
class RegisteredArtifact:
    name: str
    meta: ArtifactMeta
    #: The last envelope this harness observed for this artifact — at
    #: registration, and after every refresh. Used for the oracle's
    #: `changed` compare and for `row-touch`'s payload scan. Mirrors
    #: `bench_m5.py::propagation_sweep`'s own `last_payload` bookkeeping
    #: (registered directly, never through `refresh._publish`, so
    #: `record.payload` is not reliable here either).
    last_env: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArmOutcome:
    """One arm's answer for one batch, scored against the oracle."""

    arm: str
    invalidated: tuple[str, ...]
    check_wall_ms: float
    refresh_wall_ms: float
    #: `None` when this arm had a false-fresh this batch: an oracle-changed
    #: artifact it never nominated is never reached, so there is no honest
    #: "time to fresh" to report (§5's falsification discipline).
    ttf_ms: float | None
    false_fresh: tuple[str, ...]
    false_stale: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm, "invalidated_count": len(self.invalidated),
            "check_wall_ms": self.check_wall_ms, "refresh_wall_ms": self.refresh_wall_ms,
            "ttf_ms": self.ttf_ms,
            "false_fresh_count": len(self.false_fresh), "false_fresh": list(self.false_fresh),
            "false_stale_count": len(self.false_stale),
        }


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One correction batch, every arm's outcome, and the E-2 check-cost
    point (`log_bytes`, `log_records`) for this batch."""

    batch_index: int
    correction_class: str
    correction_generator: str
    correction_placement: str
    n_registered: int
    intersects_calls: int
    candidate_survivors: int
    lookup_wall_ms: float
    global_recompute_wall_ms: float
    oracle_changed: tuple[str, ...]
    refused: tuple[str, ...]
    arms: dict[str, ArmOutcome]
    log_bytes: int
    log_records: int
    registry_bytes: int
    #: §5/§9's TTF measurement mode this batch used ("sum" or "end-to-end") —
    #: `TTF_MODES`.
    ttf_mode: str = "sum"
    #: §2's SNAP caveat, restated per cell (`Storm.interval_vt`): `None` when
    #: the age axis was not in play this run.
    age_vt_meaningful: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index, "correction_class": self.correction_class,
            "correction_generator": self.correction_generator,
            "correction_placement": self.correction_placement,
            "n_registered": self.n_registered, "intersects_calls": self.intersects_calls,
            "candidate_survivors": self.candidate_survivors,
            "lookup_wall_ms": self.lookup_wall_ms,
            "global_recompute_wall_ms": self.global_recompute_wall_ms,
            "changed_count": len(self.oracle_changed), "changed": list(self.oracle_changed),
            "refused_count": len(self.refused),
            "arms": {k: v.to_json() for k, v in self.arms.items()},
            "ttf_mode": self.ttf_mode, "age_vt_meaningful": self.age_vt_meaningful,
            "log_bytes": self.log_bytes, "log_records": self.log_records,
            "registry_bytes": self.registry_bytes,
        }


# ---------------------------------------------------------------------------
# the naive baselines (§5 arms 2, 3; row-touch is arm 4, reused from M4)
# ---------------------------------------------------------------------------

def _payload_of(env: dict[str, Any]) -> dict[str, Any]:
    """Restated verbatim from `bench_freshness.py`/`bench_m5.py` rather than
    imported — this module must stand alone against harnesses that may
    themselves move (the same reasoning `bench_m5.py:389-392` gives for its
    own copy)."""
    return {k: v for k, v in env.items()
            if k not in ENVELOPE_META_FIELDS and k != "result_digest"}


def _entity_touch(artifacts: dict[str, RegisteredArtifact], touched: set[str]) -> tuple[str, ...]:
    """**Control — entity-touch.** Invalidate any artifact whose own query
    named a uid the correction touched."""
    if not touched:
        return ()
    return tuple(n for n, ra in artifacts.items() if touched & ra.meta.entities)


def _window_overlap(artifacts: dict[str, RegisteredArtifact],
                    interval: tuple[int, int] | None) -> tuple[str, ...]:
    """**Control — time-window overlap.** Invalidate any artifact whose own
    query named a `vt` window overlapping the correction's interval. An
    artifact whose query took no window (e.g. `entity_history`) cannot be
    invalidated by this arm at all — recorded as such, not silently folded
    into "no overlap" (mirrors `corrections.py::_outside`'s discipline for
    an operator with no window)."""
    if interval is None:
        return ()
    a, b = interval
    out = []
    for n, ra in artifacts.items():
        w = ra.meta.window
        if w is not None and w[0] < b and a < w[1]:
            out.append(n)
    return tuple(out)


def _row_touch(last_env_before: dict[str, dict[str, Any]],
              correction: Correction) -> tuple[str, ...]:
    """**Control 1, reused from M4/M5** (`bench_freshness.py:331-344`,
    `bench_m5.py:433-439`): did the correction touch an identity that
    appears, by string identity, in the artifact's own last-known payload?"""
    touched = set(correction.identities)
    if not touched:
        return ()
    out = []
    for n, env in last_env_before.items():
        if env is None:
            continue
        blob = canonical_json(_payload_of(env))
        if any(f'"{u}"' in blob for u in touched):
            out.append(n)
    return tuple(out)


def _correction_interval(ops: Sequence[dict[str, Any]]) -> tuple[int, int] | None:
    """The union `vt` interval a correction batch's own ops carry — the
    naive `window-overlap` control's only input. `retract` carries no
    `vt_e`; per `corrections.py`'s own note (`_retract`'s docstring), a
    retraction removes belief over `[t, ∞)`, so its right edge is
    `OPEN_END`. `ingest_events` carries no `vt_e` either — an appended event
    is instantaneous at `vt_s` for this purpose."""
    lo: int | None = None
    hi: int | None = None
    for op in ops:
        kind = op.get("op")
        a: int | None
        b: int | None
        if kind in ("assert_node", "assert_edge", "correct"):
            a, b = op.get("vt_s"), op.get("vt_e")
        elif kind == "retract":
            a, b = op.get("t"), OPEN_END
        elif kind == "ingest_events":
            events = op.get("events") or []
            if not events:
                continue
            a = min(e.get("vt_s", 0) for e in events)
            b = a + 1
        else:
            continue
        if a is None:
            continue
        b = a + 1 if b is None else b
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    return None if lo is None else (lo, min(hi, OPEN_END))


# ---------------------------------------------------------------------------
# C2 — rate/age/degree/range-width axes (§2 of the design memo)
#
# `corrections.generate()` stays taxonomy/placement-only (§9's own rule: the
# axes go "around `corrections.generate()`, never inside it"). Everything
# below is a `mix` callable — `Storm`'s existing, frozen extension point —
# that reweights and, for the age/burst axes, directly reuses
# `_believed_nodes`/`_version_for` to reach Δvt magnitudes `generate()`'s own
# single-step `_interval` cannot. Every corrections.Correction this section
# builds still carries a real class letter (§2's taxonomy is per-op, not
# per-mix), but `generator` is set to a synthetic name (`age_*`, `c4_burst`)
# outside the frozen 8-generator catalogue so a storm record is never
# mistaken for one of `corrections.GENERATORS`' own cells — `summarize()`
# and every test here treat `correction_generator` as free-form accounting,
# exactly as the module docstring already says of `correction_class`/
# `correction_placement`.
# ---------------------------------------------------------------------------

#: §2's four correction-rate mixes. C4's own ratio (steady state, between
#: bursts) is not specified by the design beyond "burst of 10,000" — treated
#: here as C3's 80/20, since C4's distinguishing feature is the burst event,
#: not a fifth steady-state ratio.
MIXES: tuple[str, ...] = ("c1", "c2", "c3", "c4")
MIX_APPEND_FRAC: dict[str, float] = {"c1": 0.99, "c2": 0.95, "c3": 0.80, "c4": 0.80}

#: §2's four correction-age bands.
AGE_BANDS: tuple[str, ...] = ("recent", "hours", "days", "deep")

#: §2's degree quantile buckets and range-width buckets (orthogonal
#: selectors, "recorded per trial, not a new class").
DEGREE_BUCKETS: tuple[str, ...] = ("low", "mid", "high")
RANGE_WIDTHS: tuple[str, ...] = ("narrow", "mid", "wide")

#: §5/§9's two TTF measurement modes — `sum` is the storm-core lane's
#: existing "Σ per-artifact costs" model (module docstring, "One execution
#: pass per batch"); `end-to-end` is this task's addition (see `Storm.
#: run_batch`'s own note below).
TTF_MODES: tuple[str, ...] = ("sum", "end-to-end")

#: Age-band Δvt targets as a multiple of `corrections.py`'s own
#: `step = max(2, sub.span // 20)` unit (`corrections.py:188`) — the
#: "valid-time distance... as a fraction of the substrate span" the design
#: asks for (§2's table), expressed in `step` units so it composes with the
#: same unit `corrections.py`'s own window-fraction axis already uses.
#: `recent` is drawn straight from `generate()`'s `in-window-read` cell (Δvt
#: ~= 0 by construction); the rest are built directly, below.
_AGE_STEP_RANGE: dict[str, tuple[int, int]] = {
    "recent": (0, 1), "hours": (1, 3), "days": (3, 10), "deep": (10, 40),
}


def _ratio_weights(frac: float, *, max_denominator: int = 100) -> tuple[int, int]:
    """`frac` (e.g. 0.95) as a small integer ratio `(w_a, w_other)` — 95/5 ->
    (19, 1) — so the mix's weighted candidate list stays small (at most
    `max_denominator` candidates per pool) rather than literally replicating
    to hundred-element lists for every ratio."""
    f = fractions.Fraction(frac).limit_denominator(max_denominator)
    return f.numerator, f.denominator - f.numerator if f.denominator > f.numerator else 1


def _has_interval_vt(store: Any, *, limit: int = 200) -> bool:
    """§2's SNAP caveat: is this store's valid time a genuine interval (some
    node version has a closed `vt_e`), or purely event-instant (every
    version `[first_seen, OPEN_END)`, the SNAP event-stream shape,
    `corrections.py:228`)? A bounded scan, not a full one — `probe_substrate`
    already pays the real scan cost and this only needs one `True`."""
    n = 0
    for v in store.adapter.all_node_versions():
        if v.vt_e < OPEN_END:
            return True
        n += 1
        if n >= limit:
            break
    return False


def _degree_map(store: Any, *, limit: int = 20_000) -> dict[str, int]:
    """Undirected degree by edge-endpoint count — the substrate probe's own
    scan idiom (`corrections.py::probe_substrate`), bounded the same way."""
    deg: dict[str, int] = {}
    n = 0
    for e in store.adapter.all_edge_versions():
        deg[e.src] = deg.get(e.src, 0) + 1
        deg[e.dst] = deg.get(e.dst, 0) + 1
        n += 1
        if n >= limit:
            break
    return deg


def _degree_bucket_set(uids: Sequence[str], deg_map: dict[str, int], bucket: str) -> set[str]:
    """The quantile bucket of `uids` by `deg_map` (missing uids score 0
    degree) — §2's "quantile bucket of the target's degree"."""
    if not uids:
        return set()
    ordered = sorted(uids, key=lambda u: deg_map.get(u, 0))
    n = len(ordered)
    lo, hi = {
        "low": (0, max(1, n // 3)),
        "mid": (n // 3, max(n // 3 + 1, (2 * n) // 3)),
        "high": (max(1, (2 * n) // 3), n),
    }[bucket]
    return set(ordered[lo:hi])


def _age_correction(store: Any, sub: Substrate, rng: random.Random, age: str,
                    *, recency_frac: float = 0.15) -> Correction | None:
    """One correction placed at `age`'s Δvt band.

    `Storm.target` (the generic per-batch `Target` the default mix uses) is
    always built with `window=None` (§2 selects placement per *artifact*,
    but a `mix` callable only ever sees the storm-wide `Target`) — and
    `corrections.py::_outside` requires a real window to call anything
    "outside" at all. So this function synthesizes its own small local
    window — the substrate's own last `recency_frac` of span, standing in
    for "the current query's recency window" — genuinely non-`None`, which
    is what makes `_version_for`'s outside-window "reaching" filter apply
    for real rather than degenerating to the in-window fallback
    (`corrections.py::_interval`'s own documented fallback for a `None`
    window). This is the C2 lane's own deviation, reported in the module
    report.
    """
    if not sub.uids:
        return None
    lo = sub.vt_lo + int(sub.span * (1 - recency_frac))
    hi = sub.vt_hi
    local_target = Target(read_uids=sub.uids, window=(lo, hi))
    if age == "recent":
        pool = generate_corrections(store, sub, local_target, rng=rng,
                                    placements=["in-window-read"])
        if not pool:
            return None
        c = pool[rng.randrange(len(pool))]
        return Correction(c.cls, "age_recent", c.placement, c.ops,
                          note=f"age band recent: {c.note}", identities=c.identities)

    step = max(2, sub.span // 20)
    lo_mult, hi_mult = _AGE_STEP_RANGE[age]
    uid = sub.uids[rng.randrange(len(sub.uids))]
    believed = _believed_nodes(store, uid)
    if not believed:
        return None
    dist = step * rng.randint(lo_mult, max(lo_mult, hi_mult - 1))
    vt_s = hi + dist
    vt_e = vt_s + step
    v = _version_for(believed, local_target, "outside-window-read", vt_s, vt_e, rng)
    if v is None:
        return None
    corrected_lo = max(vt_s, v.vt_s + 1)
    corrected_hi = max(corrected_lo + 1, min(vt_e, v.vt_e if v.vt_e < OPEN_END else vt_e))
    op = make_op("correct", ref={"kind": "node", "uid": uid},
                props={"injected": "age", "band": age}, vt_s=corrected_lo, vt_e=corrected_hi,
                source="inject", provenance_ref=None)
    return Correction(
        "C", f"age_{age}", "outside-window-read", (op,),
        note=(f"age band {age}: Δvt={dist} ({dist / step:.1f} steps past the "
             f"synthesized recency window)"),
        identities=(uid,))


def _build_burst(store: Any, sub: Substrate, rng: random.Random, burst_size: int,
                 *, max_attempts_factor: int = 4) -> Correction | None:
    """C4's burst: `burst_size` historical (deep, outside-window) corrections
    bundled into **one** batch (one `Correction`, many `ops`) — "a burst of
    10,000 historical corrections in one interval" (§2's own phrasing: one
    interval, i.e. one batch). Built by calling `age`'s `deep` path
    repeatedly with the shared `rng`, so each op is independently drawn
    (different uid/interval) but every one is genuinely outside a window and
    genuinely deep, never a synthetic stand-in."""
    ops: list[dict[str, Any]] = []
    identities: list[str] = []
    attempts = 0
    cap = max(1, burst_size) * max_attempts_factor
    while len(ops) < burst_size and attempts < cap:
        attempts += 1
        c = _age_correction(store, sub, rng, "deep")
        if c is not None:
            ops.extend(c.ops)
            identities.extend(c.identities)
    if not ops:
        return None
    return Correction(
        "D", "c4_burst", "outside-window-read", tuple(ops),
        note=f"C4 burst: {len(ops)}/{burst_size} historical corrections in one interval "
             f"({attempts} draws)",
        identities=tuple(identities))


class Mix:
    """A `Storm(mix=...)` callable implementing one C2 cell: a rate mix
    (`--mix`), optionally an age band (`--age`), a degree bucket
    (`--degree`), and a range-width bucket (`--range-width`).

    Never reimplements `corrections.generate()`'s taxonomy — every
    non-age-banded correction still comes from `generate()` unmodified; this
    class only reweights the A-vs-BCDE draw (§2's rate axis) and, for `c4`,
    schedules one burst batch. `degree`/`range-width` are best-effort
    accept/reject filters over `generate()`'s own output: when nothing in a
    pool survives the filter, the **unfiltered** pool is used instead of an
    empty list, so `Storm.run`'s bounded-attempt loop is never starved by a
    selector that happens to match nothing this batch — a documented
    deviation, since §2 describes these as "recorded per trial", not as a
    hard constraint on realizability.
    """

    def __init__(self, name: str, *, burst_size: int = 0, burst_after: int = 0,
                age: str | None = None, degree: str | None = None,
                range_width: str | None = None) -> None:
        if name not in MIXES:
            raise ValueError(f"unknown mix: {name!r}; choose from {MIXES}")
        if age is not None and age not in AGE_BANDS:
            raise ValueError(f"unknown age band: {age!r}; choose from {AGE_BANDS}")
        if degree is not None and degree not in DEGREE_BUCKETS:
            raise ValueError(f"unknown degree bucket: {degree!r}; choose from {DEGREE_BUCKETS}")
        if range_width is not None and range_width not in RANGE_WIDTHS:
            raise ValueError(f"unknown range-width bucket: {range_width!r}; "
                             f"choose from {RANGE_WIDTHS}")
        self.name = name
        self.append_frac = MIX_APPEND_FRAC[name]
        self.burst_size = burst_size if name == "c4" else 0
        self.burst_after = burst_after
        self.age = age
        self.degree = degree
        self.range_width = range_width
        self._calls = 0
        self._degree_map: dict[str, int] | None = None

    def _filter_degree(self, store: Any, sub: Substrate,
                       pool: list[Correction]) -> list[Correction]:
        if self._degree_map is None:
            self._degree_map = _degree_map(store)
        bucket = _degree_bucket_set(sub.uids, self._degree_map, self.degree)
        return [c for c in pool if set(c.identities) & bucket]

    def _filter_range_width(self, sub: Substrate, pool: list[Correction]) -> list[Correction]:
        step = max(2, sub.span // 20)
        out = []
        for c in pool:
            interval = _correction_interval(c.ops)
            if interval is None:
                continue
            mult = (interval[1] - interval[0]) / step
            ok = {"narrow": mult <= 1.5, "mid": 1.5 < mult <= 6, "wide": mult > 6}[
                self.range_width]
            if ok:
                out.append(c)
        return out

    def __call__(self, store: Any, sub: Substrate, target: Target,
                 rng: random.Random) -> list[Correction]:
        self._calls += 1
        if self.burst_size and self._calls == self.burst_after + 1:
            burst = _build_burst(store, sub, rng, self.burst_size)
            if burst is not None:
                return [burst]

        a_gens = [g for g, c in GENERATORS.items() if c == "A"]
        other_gens = [g for g, c in GENERATORS.items() if c != "A"]
        pool_a = generate_corrections(store, sub, target, rng=rng, generators=a_gens)
        if self.age is not None:
            pool_other = [c for c in (
                _age_correction(store, sub, rng, self.age) for _ in range(6)) if c is not None]
        else:
            pool_other = generate_corrections(store, sub, target, rng=rng, generators=other_gens)

        if self.degree is not None:
            pool_a = self._filter_degree(store, sub, pool_a) or pool_a
            pool_other = self._filter_degree(store, sub, pool_other) or pool_other
        if self.range_width is not None:
            pool_a = self._filter_range_width(sub, pool_a) or pool_a
            pool_other = self._filter_range_width(sub, pool_other) or pool_other

        w_a, w_o = _ratio_weights(self.append_frac)
        weighted = pool_a * w_a + pool_other * w_o
        return weighted or pool_a or pool_other


def build_mix(name: str, **kwargs: Any) -> Mix:
    """The CLI's own factory — `scripts/bench_correction_storm.py --mix ...
    --age ... --degree ... --range-width ... --burst-size ... --burst-after
    ...` all land here, as a single `Mix` handed to `Storm(mix=...)`."""
    return Mix(name, **kwargs)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

class Storm:
    """One correction-storm run against one store directory.

    `mix` is C2's extension point: a callable `(store, substrate, target,
    rng) -> list[Correction]` that replaces the default (every realizable
    correction from `tgms.eval.corrections.generate`, taxonomy/placement
    axes only). `Storm.run_batch` draws one correction from that list per
    batch with the harness's own seeded RNG, so a `mix` that biases the list
    toward a rate/age band composes without this file changing.
    """

    def __init__(self, store_dir: str | Path, *, n_artifacts: int = 100, seed: int = 0,
                backend: str | None = None, arms: Sequence[str] = ARMS,
                mix: Callable[[Any, Substrate, Target, random.Random],
                             list[Correction]] | None = None,
                name_prefix: str = "storm", collect_timing: bool = True,
                measure_ttf: str = "sum") -> None:
        if measure_ttf not in TTF_MODES:
            raise ValueError(f"unknown measure_ttf mode: {measure_ttf!r}; "
                             f"choose from {TTF_MODES}")
        self.measure_ttf = measure_ttf
        self.store_dir = Path(store_dir)
        self.backend = backend
        self.store = tgms.open(self.store_dir, backend=backend)
        self.registry = Registry(self.store_dir)
        self.seed = seed
        self.rng = random.Random(seed)
        self.sub = probe_substrate(self.store, rng=random.Random(seed))
        self.target = Target(read_uids=tuple(self.sub.uids), window=None)
        #: §2's SNAP caveat: whether this store's valid time is a genuine
        #: interval (so `hours`/`days` bands mean a real wall-clock-shaped
        #: distance) or purely event-instant (so they degrade to
        #: log-distance-only). Recorded on every `BatchResult`, never
        #: silently assumed either way.
        self.interval_vt = _has_interval_vt(self.store)
        self.mix = mix or (
            lambda store, sub, target, rng: generate_corrections(store, sub, target, rng=rng))
        self.arms = tuple(arms)
        #: §6's replay-contract test toggle: when `False`, every
        #: `time.perf_counter()` call this driver would otherwise make is
        #: skipped and every timing field reports `0.0` — the underlying
        #: calls (`affected`, `check_artifact`, `refresh`) and their order
        #: are identical either way, so this flag is the mechanical proof
        #: that timing collection cannot perturb what gets written to the
        #: registry or the event log (`tests/test_storm.py::
        #: test_timing_does_not_affect_digest_chain`).
        self.collect_timing = collect_timing
        #: Deterministic, wall-clock-independent `tt` (G-S4): two runs of the
        #: same seed must produce the same `tt` sequence, which `Store._write`'s
        #: `HybridLogicalClock` (wall-clock-seeded) cannot promise across two
        #: independent processes. Mirrors `tests/test_artifact_refresh.py`'s
        #: own `_apply` helper, restated here as this harness's write path.
        self._tt = self.store.eventlog.last_tt()
        self.artifacts: dict[str, RegisteredArtifact] = {}
        self.name_prefix = name_prefix
        self.n_registration_skipped = 0
        self.n_batches = 0
        self._register_population(n_artifacts)

    # -- population (§3) ----------------------------------------------------

    def _register_population(self, n: int) -> None:
        uids = list(self.sub.uids) or ["n0"]
        rel_types = list(self.sub.rel_types) or ["R"]
        for i in range(n):
            template = TEMPLATES[self.rng.randrange(len(TEMPLATES))]
            frac = WINDOW_FRACTIONS[self.rng.randrange(len(WINDOW_FRACTIONS))]
            uid = uids[self.rng.randrange(len(uids))]
            uid2 = uids[self.rng.randrange(len(uids))]
            rel = rel_types[self.rng.randrange(len(rel_types))]
            op, args, entities, window = template(self.sub, uid, uid2, rel, frac, self.rng)
            name = f"{self.name_prefix}-{i:06d}"
            try:
                _record, env = self._register_operator_artifact(name, op, args)
            except TgmsError:
                self.n_registration_skipped += 1
                continue
            self.artifacts[name] = RegisteredArtifact(
                name=name, meta=ArtifactMeta(op=op, args=args, entities=entities, window=window),
                last_env={"result_digest": env.get("result_digest")})

    def _register_operator_artifact(self, name: str, op: str, args: dict[str, Any], *,
                                    parents: tuple[ArtifactId, ...] = (),
                                    ) -> tuple[Any, dict[str, Any]]:
        """The `"operator"`-kind registration idiom
        (`scripts/bench_m5.py:1697-1727`,
        `tests/test_artifact_refresh.py::test_operator_kind_refresh_end_to_end`):
        a `ToolRouter` call, a `{"op", "args"}` blob under `ops/`, and a
        registration built from that call's own envelope. `parents` defaults
        to `()` — a leaf-only population; C5's DAG generator is the future
        caller that passes a non-empty tuple.
        """
        from tgms.tools.server import ToolRouter

        router = ToolRouter(self.store.adapter, tt_source=self.store)
        env = router.call(op, args)
        if "error" in env:
            raise TgmsError(env.get("message", "operator call refused"))
        meta = router.leaf_meta(op, env)
        ops_dir = self.store_dir / "ops"
        ops_dir.mkdir(exist_ok=True)
        ref = f"ops/{name}.json"
        (self.store_dir / ref).write_text(json.dumps({"op": op, "args": args}))
        dependency = DependencyScope.from_json(env["dependency"])
        record = self.registry.register(
            name=name, kind="query_result",
            plan={"plan_digest": meta.get("plan_digest"), "node_digest": meta.get("node_digest"),
                 "plan_format": None},
            basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
                  "tt_q_verified": dependency.tt_q_verified},
            state={"completeness": meta.get("completeness", "unknown"),
                  "exactness": meta.get("exactness", "exact"), "refusal": None},
            refresh={"kind": "operator", "ref": ref, "basis_policy": "open"},
            dependency=dependency, parents=parents,
        )
        return record, env

    # -- writes (deterministic, G-S4) ---------------------------------------

    def _next_tt(self) -> int:
        self._tt += 1
        return self._tt

    def _timed(self, fn: Callable[[], Any]) -> tuple[Any, float]:
        """Call `fn`, returning `(result, elapsed_ms)`. `elapsed_ms` is
        `0.0` when `self.collect_timing` is `False` and `fn` is still called
        exactly the same way — see the flag's own docstring."""
        if not self.collect_timing:
            return fn(), 0.0
        t0 = time.perf_counter()
        result = fn()
        return result, (time.perf_counter() - t0) * 1000

    def _write(self, tt: int, ops: list[dict[str, Any]]) -> dict[str, Any]:
        log = self.store.eventlog
        batch_id, end_offset, record = log.append(tt, ops)
        note_cursor = getattr(self.store.adapter, "note_event_cursor", None)
        if note_cursor is not None:
            if self.store._chain is None:
                self.store._chain = log.chain_of_prefix(end_offset - len(record))
            self.store._chain = extend_chain(self.store._chain, record)
        self.store.adapter.begin()
        try:
            self.store.adapter.apply_ops(ops, tt)
        except Exception:
            self.store.adapter.rollback()
            raise
        if note_cursor is not None:
            note_cursor(end_offset, self.store._chain)
        self.store.adapter.commit()
        return {"batch_id": batch_id, "tt": tt, "ops": ops}

    # -- one batch (§5, §6) --------------------------------------------------

    def run_batch(self, batch_index: int) -> BatchResult | None:
        """Inject one correction batch and score every configured arm
        against the oracle. Returns `None` if no correction was realizable
        or the injection itself was refused — the caller's `run()` retries
        with a fresh draw rather than counting a null attempt as a batch."""
        corrections = self.mix(self.store, self.sub, self.target, self.rng)
        if not corrections:
            return None
        correction = corrections[self.rng.randrange(len(corrections))]
        tt = self._next_tt()
        try:
            batch = self._write(tt, list(correction.ops))
        except TgmsError:
            return None
        self.n_batches += 1

        touched = set(correction.identities)
        interval = _correction_interval(correction.ops)
        last_env_before = {name: ra.last_env for name, ra in self.artifacts.items()}

        # the real pre-filter, shared by both tgms arms (§3.2 / R-18's own
        # instrument: `intersects_calls`, `candidate_survivors`)
        lr, lookup_wall_ms = self._timed(lambda: affected(batch, self.registry))
        candidates = {r.name for r in lr.affected}

        tgms_invalidated: dict[str, set[str]] = {"tgms-L0": set(), "tgms-L1": set()}
        tgms_check_ms: dict[str, float] = {"tgms-L0": 0.0, "tgms-L1": 0.0}
        for name in candidates:
            record = self.registry.current(name)
            if record is None:
                continue
            for arm_name, level1 in (("tgms-L0", False), ("tgms-L1", True)):
                verdict, dt = self._timed(
                    lambda r=record, l1=level1: check_artifact(r, self.store.eventlog, level1=l1))
                tgms_check_ms[arm_name] += dt
                if not verdict.actionable_fresh:
                    tgms_invalidated[arm_name].add(name)

        # C2's end-to-end TTF mode (in addition to the sum-of-parts mode
        # above, which is unaffected by this block): for the tgms arms only,
        # actually run check -> refresh over that arm's own nominated set as
        # one continuous timed interval, rather than reporting
        # `check_wall_ms + Σ refresh_wall_ms` computed from the shared
        # per-artifact table the oracle pass below builds. This necessarily
        # calls `refresh()` a second time this batch for any name both
        # passes touch (harmless: `refresh()` is idempotent in content when
        # nothing changed between the two calls, and every oracle-relevant
        # decision below — `oracle_changed`, `refused`, every arm's
        # `invalidated`/`false_fresh`/`false_stale` — is computed from the
        # *oracle* pass alone, never from this one, so the two TTF modes
        # produce byte-identical non-timing records; only `ttf_ms` differs).
        end_to_end_ms: dict[str, float] = {}
        if self.measure_ttf == "end-to-end":
            for arm_name, level1 in (("tgms-L0", False), ("tgms-L1", True)):
                if arm_name not in self.arms:
                    continue
                nominated = sorted(tgms_invalidated[arm_name])

                def _run_e2e(names=nominated, l1=level1) -> None:
                    for name in names:
                        record = self.registry.current(name)
                        if record is None:
                            continue
                        verdict = check_artifact(record, self.store.eventlog, level1=l1)
                        if not verdict.actionable_fresh and verdict.refresh is not None:
                            try:
                                refresh(record, verdict.refresh, self.store, self.registry)
                            except TgmsError:
                                pass

                _unused, dt = self._timed(_run_e2e)
                end_to_end_ms[arm_name] = dt

        entity_inv = set(_entity_touch(self.artifacts, touched))
        window_inv = set(_window_overlap(self.artifacts, interval))
        row_inv = set(_row_touch(last_env_before, correction))

        # oracle + global-recompute, one execution pass (module docstring)
        oracle_changed: set[str] = set()
        refresh_ms: dict[str, float] = {}
        refused: list[str] = []
        for name in list(self.artifacts):
            record = self.registry.current(name)
            if record is None:
                continue
            handle = RefreshHandle(record.id, record.refresh["kind"], record.refresh["ref"],
                                   record.plan.get("plan_format"),
                                   record.refresh.get("basis_policy", "open"))
            try:
                new_record, dt = self._timed(
                    lambda r=record, h=handle: refresh(r, h, self.store, self.registry))
            except TgmsError:
                refused.append(name)
                continue
            refresh_ms[name] = dt
            new_digest = (new_record.payload or {}).get("result_digest")
            ra = self.artifacts[name]
            if new_digest != ra.last_env.get("result_digest"):
                oracle_changed.add(name)
            ra.last_env = {"result_digest": new_digest}

        scored = set(self.artifacts) - set(refused)
        invalidated_by_arm = {
            "global-recompute": scored,
            "entity-touch": entity_inv,
            "window-overlap": window_inv,
            "row-touch": row_inv,
            "tgms-L0": tgms_invalidated["tgms-L0"],
            "tgms-L1": tgms_invalidated["tgms-L1"],
        }
        check_ms_by_arm = {"tgms-L0": tgms_check_ms["tgms-L0"], "tgms-L1": tgms_check_ms["tgms-L1"]}

        arms_out: dict[str, ArmOutcome] = {}
        for arm in self.arms:
            inv = invalidated_by_arm.get(arm, set()) & scored
            false_fresh = tuple(sorted(n for n in scored if n in oracle_changed and n not in inv))
            false_stale = tuple(sorted(n for n in scored if n not in oracle_changed and n in inv))
            check_ms = check_ms_by_arm.get(arm, 0.0)
            r_ms = sum(refresh_ms.get(n, 0.0) for n in inv)
            if self.measure_ttf == "end-to-end" and arm in end_to_end_ms:
                ttf_ms = None if false_fresh else end_to_end_ms[arm]
            else:
                ttf_ms = None if false_fresh else check_ms + r_ms
            arms_out[arm] = ArmOutcome(
                arm=arm, invalidated=tuple(sorted(inv)), check_wall_ms=check_ms,
                refresh_wall_ms=r_ms, ttf_ms=ttf_ms, false_fresh=false_fresh,
                false_stale=false_stale)

        return BatchResult(
            batch_index=batch_index, correction_class=correction.cls,
            correction_generator=correction.generator, correction_placement=correction.placement,
            n_registered=len(self.artifacts), intersects_calls=lr.intersects_calls,
            candidate_survivors=lr.candidate_survivors, lookup_wall_ms=lookup_wall_ms,
            global_recompute_wall_ms=sum(refresh_ms.values()),
            oracle_changed=tuple(sorted(oracle_changed)), refused=tuple(refused), arms=arms_out,
            log_bytes=self.store.eventlog.size(), log_records=self.n_batches,
            registry_bytes=self.registry.path.stat().st_size,
            ttf_mode=self.measure_ttf,
            age_vt_meaningful=(self.interval_vt if isinstance(self.mix, Mix)
                              and self.mix.age is not None else None))

    def run(self, n_batches: int, *, max_attempts_factor: int = 4) -> list[BatchResult]:
        """Run until `n_batches` batches have been realized, or give up
        after `max_attempts_factor * n_batches` draws — the same "not every
        draw is realizable" discipline `tgms.eval.corrections.generate`
        itself follows (a cell that cannot be realized returns nothing
        rather than a degraded substitute)."""
        results: list[BatchResult] = []
        attempts = 0
        cap = max(1, n_batches) * max_attempts_factor
        while len(results) < n_batches and attempts < cap:
            attempts += 1
            r = self.run_batch(len(results))
            if r is not None:
                results.append(r)
        return results

    def close(self) -> None:
        self.store.close()


# ---------------------------------------------------------------------------
# summary (§5, §6, §8's "reported, not passed" figures)
# ---------------------------------------------------------------------------

def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def summarize(results: Sequence[BatchResult]) -> dict[str, Any]:
    """The per-arm summary table (§5, §8): false-fresh (must be 0 for tgms
    arms), false-stale, invalidated count, avoided recomputation in both
    senses, TTF p50/p95 — plus the E-2 check-cost-vs-log-size curve as a
    first-class output, never folded into a single flattering number."""
    arms = sorted({a for r in results for a in r.arms})
    per_arm: dict[str, Any] = {}
    for arm in arms:
        outcomes = [r.arms[arm] for r in results if arm in r.arms]
        ttfs = [o.ttf_ms for o in outcomes if o.ttf_ms is not None]
        false_fresh_total = sum(len(o.false_fresh) for o in outcomes)
        false_stale_total = sum(len(o.false_stale) for o in outcomes)
        invalidated_total = sum(len(o.invalidated) for o in outcomes)
        registered_total = sum(r.n_registered for r in results)
        refresh_ms_total = sum(o.refresh_wall_ms for o in outcomes)
        global_ms_total = sum(r.global_recompute_wall_ms for r in results)
        per_arm[arm] = {
            "false_fresh": false_fresh_total,
            "false_stale": false_stale_total,
            "invalidated": invalidated_total,
            "avoided_recompute_decision": (
                1 - (invalidated_total / registered_total) if registered_total else None),
            "avoided_recompute_wall": (
                1 - (refresh_ms_total / global_ms_total) if global_ms_total else None),
            "ttf_p50_ms": _percentile(ttfs, 0.5),
            "ttf_p95_ms": _percentile(ttfs, 0.95),
            "ttf_incomplete_batches": sum(1 for o in outcomes if o.ttf_ms is None),
        }
    check_cost_curve = [
        {"batch_index": r.batch_index, "log_bytes": r.log_bytes, "log_records": r.log_records,
         "check_ms_tgms_L0": r.arms["tgms-L0"].check_wall_ms if "tgms-L0" in r.arms else None,
         "check_ms_tgms_L1": r.arms["tgms-L1"].check_wall_ms if "tgms-L1" in r.arms else None,
         "registry_bytes": r.registry_bytes}
        for r in results
    ]
    return {"arms": per_arm, "check_cost_curve": check_cost_curve, "batches": len(results)}


__all__ = [
    "ARMS", "WINDOW_FRACTIONS", "TEMPLATES", "ArmOutcome", "ArtifactMeta", "BatchResult",
    "RegisteredArtifact", "Storm", "summarize",
    # C2
    "MIXES", "MIX_APPEND_FRAC", "AGE_BANDS", "DEGREE_BUCKETS", "RANGE_WIDTHS", "TTF_MODES",
    "Mix", "build_mix",
]
