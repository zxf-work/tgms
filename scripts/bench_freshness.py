"""M4.5/M4.6 — the correction-injection harness and its two metrics (D6.1, D6.2).

The question this measures is not *"does the check work"* but *"does it ever
say `FRESH` about an answer that changed"*. One such event falsifies the
mechanism; the second metric — how often it says `POSSIBLY_STALE` about an
answer that did not change — has no target and is reported, not passed.

**The protocol (D6.1), per `(Q, A)` per injected batch:**

1. execute at `tt_q`; record the payload digest, the **outcome class**, and
   the scope;
2. inject **one** controlled correction batch at `tt_c > tt_q`;
3. recompute with the cost guardrail **bypassed** (`skip_cost_check=True`) and
   compare per D1.8 → `changed` / `unchanged` / an outcome class;
4. compute the verdict `V` from the **recorded** scope and the log — never
   from a re-derived scope, or the harness measures a scope the result never
   carried;
5. cross-tabulate.

**Isolation.** Each trial injects into a *copy* of the substrate, or the
corrections compound and D6.1's "one controlled batch" quietly becomes "the
accumulated history of every earlier trial" (§8.6: isolation bugs are
false-fresh factories, in both directions). The cheaper alternative — inject,
measure, then retract back — is rejected outright: an undo is itself a Class
B/C/D op, it changes the log, and the next trial's suffix then contains it.

**Replayability.** A trial is `(store_digest_before, scope_digest,
injected_batch_id, verdict, changed)` and is reconstructible from those five
fields alone; every artifact also embeds the config, machine, seed and counts.

    uv run python scripts/bench_freshness.py --profile ci
    uv run python scripts/bench_freshness.py --profile full --out benchmarks/freshness-v1
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import canonical_json  # noqa: E402
from tgms.eval.corrections import (  # noqa: E402
    Correction, Substrate, Target, generate, probe_substrate,
)
from tgms.temporal.algebra import call_operator, ensure_all_registered  # noqa: E402
from tgms.tgir.check import ChainCache, check  # noqa: E402
from tgms.tgir.depscope import TOP_TERM, DependencyScope  # noqa: E402

# The registry is populated lazily — `ToolRouter` does this for the agent
# surfaces. This harness calls `call_operator` directly, so it must too, or
# every cell reports "unknown operator" and the sweep silently measures
# nothing at all.
ensure_all_registered()

# ---------------------------------------------------------------------------
# the population (plan §4.2, as adjudicated)
# ---------------------------------------------------------------------------

#: `compute` has ∅ scope intrinsically (D5.3) and is carried **only** as a
#: control that ∅ ⇒ `FRESH` forever. It is not in any precision denominator.
CONTROL_OPS: tuple[str, ...] = ("compute",)

#: **`resolve_entities` is excluded from the M4 workload by §13.8.1** — the
#: exclusion is from the measurement, not from the contract. It is named in
#: every ratio table with its reason rather than silently absent, per the
#: EVIDENCE_MODEL §7 discipline this project applies to every filtered
#: denominator.
#:
#: The soundness suite uses it for CE-6, which is correct there and must **not**
#: leak into this population: a scenario written to exhibit a counterexample is
#: not a sample from a workload. The exclusion is wired here, not assumed.
EXCLUDED_OPS: dict[str, str] = {
    "resolve_entities": "§13.8.1 — excluded from the M4 workload by ruling; its "
                        "only M4 appearance is the soundness suite's CE-6, which "
                        "is a constructed counterexample, not a workload sample",
}

#: The 13 measured operators: fifteen, minus the ∅ control, minus the exclusion.
MEASURED_OPS: tuple[str, ...] = (
    "entity_history", "version_history", "snapshot_subgraph", "diff_snapshots",
    "neighborhood_evolution", "aggregate_events", "graph_metric_timeseries",
    "burst_detection", "co_active", "count_temporal_motifs",
    "find_temporal_motif_instances", "temporal_reachability", "temporal_paths",
)


@dataclass(frozen=True, slots=True)
class Cell:
    """One `(Q, A)`: an operator and one scope-shaping argument form."""

    op: str
    form: str
    args: dict[str, Any]
    tier: str = "T2"

    @property
    def key(self) -> str:
        return f"{self.op}/{self.form}"


def _window(lo: int, hi: int) -> dict[str, int]:
    return {"t_a": lo, "t_b": hi}


def cells_for(sub: Substrate, rel: str) -> list[Cell]:
    """~3 scope-shaping forms per operator (plan §4.2).

    The scope *shape*, not the operator, is what is under test, and the forms
    below are the ones a frozen document singles out by name — `include_edges`
    (T1b/T1c appear only with edges), `aggregate_events` with and without
    `of: "duration"` (the RG-1 pair: identical call, one carve-reachable and
    one not), a `label` group_by dim (FF-3's node-kinded term), and `rel_types`
    set versus null on every operator that takes one (`T` narrowing).
    """
    lo, hi = sub.vt_lo, sub.vt_hi
    mid = lo + (hi - lo) // 2
    w = _window(lo, hi)
    half = _window(lo, mid)
    uid = sub.uids[0] if sub.uids else "n0"
    uid2 = sub.uids[1] if len(sub.uids) > 1 else uid
    out: list[Cell] = [
        Cell("entity_history", "nodes-only", {"uid": uid}),
        Cell("entity_history", "with-edges", {"uid": uid, "include_edges": True}),
        Cell("entity_history", "with-edges-alt",
             {"uid": uid2, "include_edges": True}),
        Cell("version_history", "node-window", {"kind": "node", "window": w}),
        Cell("version_history", "edge-window", {"kind": "edge", "window": w}),
        Cell("version_history", "edge-window-typed",
             {"kind": "edge", "window": w, "rel_types": [rel]}),
        Cell("version_history", "node-half", {"kind": "node", "window": half}),
        Cell("snapshot_subgraph", "seed-1hop",
             {"seeds": [uid], "t_valid": mid, "hops": 1}),
        Cell("snapshot_subgraph", "seed-1hop-typed",
             {"seeds": [uid], "t_valid": mid, "hops": 1, "rel_types": [rel]}),
        Cell("snapshot_subgraph", "seed-0hop",
             {"seeds": [uid, uid2], "t_valid": mid, "hops": 0}),
        Cell("diff_snapshots", "lo-mid", {"t1": lo + 1, "t2": mid}),
        Cell("diff_snapshots", "mid-hi", {"t1": mid, "t2": max(mid + 1, hi - 1)}),
        Cell("diff_snapshots", "scoped",
             {"t1": lo + 1, "t2": mid, "scope": {"seeds": [uid], "hops": 1}}),
        Cell("neighborhood_evolution", "one-uid",
             {"uid": uid, "t1": lo, "t2": hi, "stride": max(1, (hi - lo) // 8)}),
        Cell("neighborhood_evolution", "one-uid-fine",
             {"uid": uid, "t1": lo, "t2": mid, "stride": max(1, (hi - lo) // 16)}),
        Cell("neighborhood_evolution", "other-uid",
             {"uid": uid2, "t1": lo, "t2": hi, "stride": max(1, (hi - lo) // 8)}),
        # the RG-1 pair: identical call, identical window, and one of them is
        # carve-reachable and loses `V`. The single cleanest A/B in the matrix.
        Cell("aggregate_events", "count-endpoint",
             {"group_by": [{"dim": "endpoint", "role": "src"}],
              "aggregates": [{"agg": "count"}], "window": w}),
        Cell("aggregate_events", "max-duration",
             {"group_by": [{"dim": "endpoint", "role": "src"}],
              "aggregates": [{"agg": "max", "of": "duration"}], "window": w}),
        Cell("aggregate_events", "count-endpoint-typed",
             {"group_by": [{"dim": "endpoint", "role": "src"}],
              "aggregates": [{"agg": "count"}], "window": w, "rel_types": [rel]}),
        Cell("aggregate_events", "count-label",
             {"group_by": [{"dim": "label", "role": "src"}],
              "aggregates": [{"agg": "count"}], "window": w}),
        Cell("graph_metric_timeseries", "edge-events",
             {"metric": "edge_event_count", "window": w,
              "stride": max(1, (hi - lo) // 8)}),
        Cell("graph_metric_timeseries", "active-edges",
             {"metric": "active_edge_count", "window": w,
              "stride": max(1, (hi - lo) // 8)}),
        Cell("graph_metric_timeseries", "node-count",
             {"metric": "node_count", "window": w,
              "stride": max(1, (hi - lo) // 8)}),
        Cell("burst_detection", "edge-rate",
             {"target": {"kind": "edge_event_rate"}, "window": w,
              "stride": max(1, (hi - lo) // 16)}),
        Cell("burst_detection", "edge-rate-typed",
             {"target": {"kind": "edge_event_rate", "rel_type": rel}, "window": w,
              "stride": max(1, (hi - lo) // 16)}),
        Cell("burst_detection", "node-activity",
             {"target": {"kind": "node_activity", "uid": uid}, "window": w,
              "stride": max(1, (hi - lo) // 16)}),
        Cell("co_active", "before",
             {"a_spec": {"rel_type": rel}, "b_spec": {"rel_type": rel},
              "allen_relation": {"relation": "before", "gap": max(1, (hi - lo) // 20)},
              "limit": 200}),
        Cell("co_active", "overlaps",
             {"a_spec": {"rel_type": rel}, "b_spec": {"rel_type": rel},
              "allen_relation": {"relation": "overlaps"}, "limit": 200}),
        Cell("count_temporal_motifs", "pingpong",
             {"motif": "M_2node_pingpong", "window": w,
              "delta": max(1, (hi - lo) // 8)}),
        Cell("count_temporal_motifs", "path3",
             {"motif": "M_path_3", "window": w, "delta": max(1, (hi - lo) // 16)}),
        Cell("find_temporal_motif_instances", "pingpong",
             {"motif": "M_2node_pingpong", "window": w,
              "delta": max(1, (hi - lo) // 8), "limit": 50}),
        Cell("temporal_reachability", "out",
             {"src": uid, "window": w}),
        Cell("temporal_reachability", "in",
             {"src": uid, "window": w, "direction": "in"}),
        Cell("temporal_reachability", "out-half",
             {"src": uid2, "window": half}),
        Cell("temporal_paths", "uid-to-uid",
             {"src": uid, "dst": uid2, "window": w, "k": 3}),
        Cell("temporal_paths", "uid-to-uid-shallow",
             {"src": uid, "dst": uid2, "window": w, "k": 1, "max_hops": 2}),
        # the empty-scope control: `compute` over literals is FRESH forever
        Cell("compute", "literal-count",
             {"fn": "count", "input": [{"x": 1}, {"x": 2}]}, tier="control"),
    ]
    return [c for c in out if c.op not in EXCLUDED_OPS]


# ---------------------------------------------------------------------------
# outcome classification (§1.6, D1.8)
# ---------------------------------------------------------------------------

OUTCOME_REFUSED = "REFUSED_ON_RECOMPUTE"
OUTCOME_ERRORED = "ERRORED"
OUTCOME_OK = "OK"


@dataclass
class Trial:
    """The row. Replayable from `store_digest_before`, `scope_digest`,
    `injected_batch_id`, `verdict` and `changed` alone (§1.6 of the plan)."""

    tier: str
    store: str
    cell: str
    op: str
    form: str
    cls: str
    generator: str
    placement: str
    # the five replay fields
    store_digest_before: str = ""
    scope_digest: str = ""
    injected_batch_id: str = ""
    verdict: str = ""
    changed: bool = False
    # the disaggregations D6.2 and §4.6 need
    value_changed: bool = False
    digest_only_changed: bool = False
    outcome: str = OUTCOME_OK
    reason: str | None = None
    degraded: tuple[str, ...] = ()
    arms: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    matched_on: tuple[str, ...] = ()
    witnesses: int = 0
    scope_bytes: int = 0
    payload_bytes: int = 0
    terms: int = 0
    check_ms: float = 0.0
    # the two required controls (D6.4, §4.7)
    rowtouch_verdict: str = ""
    top_verdict: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("degraded", "arms", "kinds", "matched_on"):
            d[k] = list(d[k])
        return d


def _payload_of(env: dict[str, Any]) -> dict[str, Any]:
    from tgms.temporal.algebra import ENVELOPE_META_FIELDS
    return {k: v for k, v in env.items()
            if k not in ENVELOPE_META_FIELDS and k != "result_digest"}


def _value_of(env: dict[str, Any]) -> str:
    """The answer with **version identity stripped**.

    Under D1.8 a `vid` change *is* a change, and D8.5 freezes `vid` into the
    legacy operators' output — so a Class B/C/D correction changes the digest
    of a result whose every value is identical. Those trials are trivially easy
    to mark stale, so a `changed` column dominated by them makes "false-fresh
    = 0" weak evidence (§8.7). D-M4g's ruling: report both denominators. This
    is the second one.
    """
    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: strip(v) for k, v in sorted(x.items())
                    if k not in ("vid", "tt_s", "tt_e", "provenance_ref")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return canonical_json(strip(_payload_of(env)))


def _execute(store: Any, cell: Cell, *, bypass: bool) -> tuple[dict[str, Any] | None,
                                                               str, str | None]:
    """Run one cell. Returns `(envelope, outcome, error)`.

    Recompute always bypasses the cost guardrail (§1.6, §8.8): admission is a
    function of *current* statistics, and `co_active`/`temporal_paths` also
    refuse *inside* the kernel on realized work. A refusal is recorded as its
    own outcome class and never folded into either metric.
    """
    try:
        env = call_operator(store.adapter, cell.op, dict(cell.args),
                            skip_cost_check=bypass, tt_source=store)
        return env, OUTCOME_OK, None
    except TgmsError as e:
        code = getattr(e, "code", type(e).__name__)
        outcome = (OUTCOME_REFUSED if "COST" in str(code).upper()
                   or "BUDGET" in str(code).upper() else OUTCOME_ERRORED)
        return None, outcome, f"{code}: {e}"


# ---------------------------------------------------------------------------
# the two required controls (D6.4, §4.7)
# ---------------------------------------------------------------------------

def _row_touch_verdict(env: dict[str, Any], correction: Correction) -> str:
    """**Control 1 — the naive row-touch rule (D6.4, required).**

    *"Did the correction touch a row that appears in the stored result?"* —
    scored by identity, over the result's own rows. §3's counterexamples predict
    a non-zero false-fresh rate and §13.6 predicts it fails on a two-op batch
    over a five-node store. **Publishing that number is what turns memo §15
    from an assertion into a measurement.**
    """
    touched = set(correction.identities)
    if not touched:
        return "fresh"
    blob = canonical_json(_payload_of(env))
    return "possibly-stale" if any(f'"{u}"' in blob for u in touched) else "fresh"


def _top_scope(scope: DependencyScope) -> DependencyScope:
    """**Control 2 — the all-`"*"` scope.** Sound by construction; its precision
    is the floor. The distance between it and the real derivations is what the
    three Level-0 derivations bought, and it costs one config flag."""
    return scope.with_terms([TOP_TERM])


# ---------------------------------------------------------------------------
# one trial
# ---------------------------------------------------------------------------

def run_trial(pristine: Path, cell: Cell, before_env: dict[str, Any],
              scope: DependencyScope, correction: Correction, *,
              tier: str, store_name: str, backend: str,
              want_controls: bool) -> Trial | None:
    """Copy, inject, recompute, classify. The copy is the isolation (§4.1)."""
    t = Trial(tier=tier, store=store_name, cell=cell.key, op=cell.op,
              form=cell.form, cls=correction.cls, generator=correction.generator,
              placement=correction.placement)
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    try:
        store = tgms.open(work, backend=backend)
    except Exception as e:  # pragma: no cover - a copy that will not open
        shutil.rmtree(work.parent, ignore_errors=True)
        t.outcome, t.note = OUTCOME_ERRORED, f"open failed: {e}"
        return t
    try:
        t.store_digest_before = store.digest()
        t.scope_digest = scope.digest()
        t.scope_bytes = len(scope.canonical())
        t.payload_bytes = len(canonical_json(_payload_of(before_env)))
        t.terms = len(scope.terms)

        try:
            store._write(list(correction.ops))
        except TgmsError as e:
            # a generator whose op the write path refuses is NOT a trial: it
            # injected nothing, so there is nothing to be fresh or stale about.
            # Recorded so the matrix's realized shape is auditable.
            t.note = f"injection refused: {type(e).__name__}: {e}"
            t.outcome = "NOT_INJECTED"
            return t
        t.injected_batch_id = _last_batch_id(store)

        after_env, outcome, err = _execute(store, cell, bypass=True)
        t.outcome = outcome
        if after_env is None:
            t.note = err or ""
            return t

        t.changed = after_env["result_digest"] != before_env["result_digest"]
        t.value_changed = _value_of(after_env) != _value_of(before_env)
        t.digest_only_changed = t.changed and not t.value_changed

        t0 = time.perf_counter()
        verdict = check(scope, store.eventlog)
        t.check_ms = (time.perf_counter() - t0) * 1000
        t.verdict = verdict.state
        t.reason = verdict.reason
        t.degraded = tuple(verdict.degraded)
        t.witnesses = verdict.total
        t.arms = tuple(sorted({w.arm for w in verdict.witnesses}))
        t.kinds = tuple(sorted({w.kind for w in verdict.witnesses}))
        t.matched_on = tuple(sorted({c for w in verdict.witnesses
                                     for c in w.matched_on}))
        if want_controls:
            t.rowtouch_verdict = _row_touch_verdict(before_env, correction)
            t.top_verdict = check(_top_scope(scope), store.eventlog).state
        return t
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass
        shutil.rmtree(work.parent, ignore_errors=True)


def _last_batch_id(store: Any) -> str:
    last = ""
    for batch in store.eventlog.batches():
        last = batch["batch_id"]
    return last


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name: str
    stores: tuple[tuple[str, str], ...]     # (label, path)
    max_cells: int
    max_corrections: int
    controls: bool = True
    seed: int = 20260822


PROFILES: dict[str, Profile] = {
    # a smoke profile that runs in CI budget: the shape of the whole matrix on
    # the small fixture, so the harness itself is regression-tested
    "ci": Profile("ci", (("ldbc-fixture", "stores/ldbc-fixture"),), 8, 8),
    # soundness-only tier: the 22-entity fixture cannot support an honest
    # precision number (§4.2), and it is reported as soundness-only rather than
    # dropped, because size does not matter to a false-fresh event
    "fixture": Profile("fixture", (("ldbc-fixture", "stores/ldbc-fixture"),), 40, 40),
    # the headline: precision is measured only here
    "full": Profile("full", (("bitcoinotc", "stores/bitcoinotc"),
                             ("collegemsg", "stores/collegemsg"),
                             ("ldbc-fixture", "stores/ldbc-fixture")), 40, 40),
}

#: Which substrates carry the precision headline (§4.2's tier table). The
#: fixture is soundness-only and says so in every table it appears in.
PRECISION_STORES: frozenset[str] = frozenset({"bitcoinotc", "collegemsg"})


def sweep(profile: Profile, *, backend: str | None = None,
          ops: Sequence[str] | None = None) -> dict[str, Any]:
    rng = random.Random(profile.seed)
    trials: list[Trial] = []
    stores_seen: list[dict[str, Any]] = []
    for label, rel in profile.stores:
        path = ROOT / rel
        if not path.exists():
            print(f"--   {label}: no store at {path}; skipped")
            continue
        trials.extend(_sweep_store(label, path, profile, rng, backend, ops,
                                   stores_seen))
    return {"trials": trials, "stores": stores_seen}


def _sweep_store(label: str, path: Path, profile: Profile, rng: random.Random,
                 backend: str | None, ops: Sequence[str] | None,
                 stores_seen: list[dict[str, Any]]) -> list[Trial]:
    store = tgms.open(path, backend=backend)
    backend = store.backend
    sub = probe_substrate(store, rng=rng)
    rel = sub.rel_types[0]
    pristine_digest = store.digest()
    cells = [c for c in cells_for(sub, rel) if ops is None or c.op in ops]
    cells = cells[:profile.max_cells]

    # execute each cell ONCE on the pristine store: the `tt_q` execution is
    # identical across that cell's injections, and doing it per trial would
    # cost 20x for no information
    baseline: dict[str, tuple[dict[str, Any], DependencyScope, Target]] = {}
    for cell in cells:
        env, outcome, err = _execute(store, cell, bypass=True)
        if env is None:
            print(f"--   {label} {cell.key}: {outcome} at baseline ({err})")
            continue
        scope = DependencyScope.from_json(env["dependency"])
        win = cell.args.get("window")
        target = Target(
            read_uids=tuple(str(cell.args[k]) for k in ("uid", "src", "dst")
                            if isinstance(cell.args.get(k), str)),
            window=(win["t_a"], win["t_b"]) if isinstance(win, dict) else None)
        baseline[cell.key] = (env, scope, target)
    stores_seen.append({"label": label, "backend": backend,
                        "digest": pristine_digest, "cells": len(baseline),
                        "uids_sampled": len(sub.uids),
                        "precision_tier": label in PRECISION_STORES})
    store.close()

    out: list[Trial] = []
    for cell in cells:
        if cell.key not in baseline:
            continue
        env, scope, target = baseline[cell.key]
        probe = tgms.open(path, backend=backend)
        corrections = generate(probe, sub, target, rng=rng)
        probe.close()
        for correction in corrections[:profile.max_corrections]:
            trial = run_trial(path, cell, env, scope, correction,
                              tier=cell.tier, store_name=label, backend=backend,
                              want_controls=profile.controls)
            if trial is not None:
                out.append(trial)
        print(f"ok   {label} {cell.key}: "
              f"{len([t for t in out if t.cell == cell.key])} trials")
    return out


# ---------------------------------------------------------------------------
# scoring (D6.2, §4.4, §4.5)
# ---------------------------------------------------------------------------

#: §4.5's exit criterion, **committed before the run** and never adjusted after
#: seeing the population. If the matrix does not produce this, the honest
#: report is "not adequately measured", not "passed".
FLOOR: dict[str, int] = {
    "changed": 300,
    "operators": 10,
    "classes": 4,
    "outside_window": 50,
    "new_identity": 50,
    "value_changed": 100,
}


def scored(trials: Sequence[Trial]) -> list[Trial]:
    """Trials that actually injected and actually recomputed. A refusal or an
    error is reported as its own column and never folded into either metric."""
    return [t for t in trials if t.outcome == OUTCOME_OK]


def summarize(trials: Sequence[Trial]) -> dict[str, Any]:
    live = scored(trials)
    changed = [t for t in live if t.changed]
    value_changed = [t for t in changed if t.value_changed]
    digest_only = [t for t in changed if t.digest_only_changed]
    unchanged = [t for t in live if not t.changed]

    # THE headline. Judged on the FULL digest-changed set (coordinator ruling),
    # and reported split so the weak half is visible.
    false_fresh = [t for t in changed if t.verdict == "fresh"]
    false_fresh_value = [t for t in false_fresh if t.value_changed]

    stale = [t for t in live if t.verdict != "fresh"]
    precision_pool = [t for t in stale if t.store in PRECISION_STORES]
    true_stale = [t for t in precision_pool if t.changed]

    floor_now = {
        "changed": len(changed),
        "operators": len({t.op for t in changed}),
        "classes": len({t.cls for t in changed}),
        "outside_window": len([t for t in changed
                               if t.placement.startswith("outside-window")]),
        "new_identity": len([t for t in changed if t.placement == "new-identity"]),
        "value_changed": len(value_changed),
    }
    met = {k: floor_now[k] >= v for k, v in FLOOR.items()}

    return {
        "trials": len(trials),
        "injected": len(live),
        "not_injected": len([t for t in trials if t.outcome == "NOT_INJECTED"]),
        "refused_or_errored": len([t for t in trials
                                   if t.outcome in (OUTCOME_REFUSED, OUTCOME_ERRORED)]),
        "changed": len(changed),
        "value_changed": len(value_changed),
        "digest_only_changed": len(digest_only),
        "unchanged": len(unchanged),
        "false_fresh": len(false_fresh),
        "false_fresh_value_changed": len(false_fresh_value),
        "undecidable": len([t for t in live if t.verdict == "undecidable"]),
        "precision": (len(true_stale) / len(precision_pool)
                      if precision_pool else None),
        "precision_denominator": len(precision_pool),
        "floor": {"required": FLOOR, "achieved": floor_now, "met": met,
                  "all_met": all(met.values())},
        "excluded_ops": EXCLUDED_OPS,
        "control_ops": list(CONTROL_OPS),
    }


def receipt(profile: Profile, extra: dict[str, Any]) -> dict[str, Any]:
    """M4 process rule 6: git SHA, config, machine, seed, counts — so a run is
    replayable rather than merely reported."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
    except Exception:  # pragma: no cover
        sha = ""
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": sha,
        "profile": profile.name,
        "seed": profile.seed,
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                    "python": platform.python_version(),
                    "cpus": os.cpu_count()},
        **extra,
    }


# ---------------------------------------------------------------------------
# overhead (§4.7)
# ---------------------------------------------------------------------------

def measure_check_latency(store_path: Path, *, backend: str | None = None,
                          repeats: int = 12) -> dict[str, Any]:
    """D13.26's cost claim, **with and without the chain cache**.

    E-2 restates the claim: a check costs `O(prefix)` to verify the checkpoints
    plus `O(suffix)` to scan the corrections. Only the second term is
    proportional to corrections-since-the-read. The cache makes repeated checks
    against one log `O(suffix)` after the first — and it is an implementation
    convenience, so the number is reported both ways rather than quietly with
    it on.
    """
    store = tgms.open(store_path, backend=backend)
    sub = probe_substrate(store, rng=random.Random(1))
    env, _outcome, _err = _execute(
        store, Cell("entity_history", "with-edges",
                    {"uid": sub.uids[0], "include_edges": True}), bypass=True)
    if env is None:  # pragma: no cover
        store.close()
        return {}
    scope = DependencyScope.from_json(env["dependency"])
    log = store.eventlog
    batches = sum(1 for _ in log.batches())

    def timed(cache: ChainCache | None) -> float:
        samples = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            check(scope, log, chain_cache=cache)
            samples.append((time.perf_counter() - t0) * 1000)
        return sorted(samples)[len(samples) // 2]

    uncached = timed(None)
    cache = ChainCache()
    check(scope, log, chain_cache=cache)          # warm
    cached = timed(cache)
    store.close()
    return {"log_batches": batches, "log_bytes": log.size(),
            "median_ms_uncached": round(uncached, 3),
            "median_ms_cached": round(cached, 3),
            "speedup": round(uncached / cached, 1) if cached else None}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="ci")
    ap.add_argument("--backend", default=None)
    ap.add_argument("--out", default="benchmarks/freshness-v1")
    ap.add_argument("--ops", nargs="*", default=None)
    ap.add_argument("--no-controls", action="store_true")
    ap.add_argument("--overhead-only", action="store_true")
    args = ap.parse_args()

    profile = PROFILES[args.profile]
    if args.no_controls:
        profile = Profile(profile.name, profile.stores, profile.max_cells,
                          profile.max_corrections, False, profile.seed)
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    overhead = {}
    for label, rel in profile.stores:
        path = ROOT / rel
        if path.exists():
            overhead[label] = measure_check_latency(path, backend=args.backend)
    if args.overhead_only:
        print(json.dumps(overhead, indent=1))
        return 0

    t0 = time.perf_counter()
    result = sweep(profile, backend=args.backend, ops=args.ops)
    wall = time.perf_counter() - t0
    trials: list[Trial] = result["trials"]
    summary = summarize(trials)

    record = {
        **receipt(profile, {"stores": result["stores"], "wall_s": round(wall, 1),
                            "trial_count": len(trials)}),
        "summary": summary,
        "overhead": overhead,
        "trials": [t.to_json() for t in trials],
    }
    path = out_dir / f"trials-{profile.name}.json"
    path.write_text(json.dumps(record, indent=1, sort_keys=False))

    print()
    print(f"wrote {path.relative_to(ROOT)}  ({len(trials)} trials in {wall:.1f}s)")
    print(f"changed {summary['changed']} "
          f"(value {summary['value_changed']}, digest-only "
          f"{summary['digest_only_changed']}); unchanged {summary['unchanged']}")
    print(f"FALSE-FRESH: {summary['false_fresh']}  "
          f"(value-changed subset: {summary['false_fresh_value_changed']})")
    if summary["precision"] is not None:
        print(f"invalidation precision: {summary['precision']:.3f} "
              f"over {summary['precision_denominator']} POSSIBLY_STALE trials")
    print(f"denominator floor met: {summary['floor']['all_met']}  "
          f"{summary['floor']['achieved']}")
    return 0 if summary["false_fresh"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
