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
    Correction, Substrate, Target, generate as generate_corrections, probe_substrate,
)
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
                name_prefix: str = "storm", collect_timing: bool = True) -> None:
        self.store_dir = Path(store_dir)
        self.backend = backend
        self.store = tgms.open(self.store_dir, backend=backend)
        self.registry = Registry(self.store_dir)
        self.seed = seed
        self.rng = random.Random(seed)
        self.sub = probe_substrate(self.store, rng=random.Random(seed))
        self.target = Target(read_uids=tuple(self.sub.uids), window=None)
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
            registry_bytes=self.registry.path.stat().st_size)

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
]
