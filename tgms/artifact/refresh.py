"""P2.1's refresh executor (M5 design memo §2.2, §8 P1.2-g;
`docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §5 P2.1).

This file exists so the lane boundary is real from day one (`refresh.py`'s
own module docstring, unchanged below). Refresh **must** open a store and
run a kernel — the opposite of every other module in this package, which
reads only a `DependencyScope` and an `EventLog` (D13.20's boundary,
`scripts/check_freshness_boundary.py`). Putting refresh here, deliberately
outside the guarded allowlist (§7.1), is what lets the boundary be drawn
*through* `tgms/artifact/` rather than around it: the four checking modules
keep the "runs against a log it did not produce" property, and this one —
which cannot have that property, because refreshing means recomputing —
visibly does not claim it.

**What this module does, in the memo's own words (P2.1 execution-plan
brief).** Rerun *only* the invalidated artifact's plan, publish a new
artifact generation, preserve the old one untouched (auditability), and
re-register the new generation's `DependencyScope` from the fresh
execution's envelope — never copied from the old record. Deterministic:
replaying the same event log plus the same corrections and refresh calls
into a fresh store directory yields identical registry `record_digest`
chains (no wall clock anywhere).

**Selectivity is the caller's, not this module's** (§5.5, §5.6). A
`RefreshHandle` normally comes from `tgms.artifact.witness.check_artifact`,
called by the caller — `refresh()` below never imports `check_artifact` and
never decides *whether* a record is stale; it is handed a `handle` and a
`record` and it either runs the handle or refuses, by name, why it would
not. Level 1 explains threat and never proves staleness (§5.6); refresh
inherits that posture by construction, simply by not asking the question.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tgms.core.errors import InvalidArgError, TgmsError
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import load as load_plan
from tgms.tgir.plan import Plan

from tgms.artifact.record import ArtifactId, ArtifactRecord
from tgms.artifact.registry import Registry
from tgms.artifact.witness import KNOWN_PLAN_FORMATS, RefreshHandle

#: refresh.py's own closed refusal taxonomy — independent of `check.py`'s
#: `REASONS` (that one names why a *verdict* is undecidable; this one names
#: why a *refresh* did not run). Stated as a closed tuple, the same
#: discipline `check.py:66-70` uses for its own `REASONS`, so a new refusal
#: is a deliberate addition here rather than a string that quietly appears.
REFUSAL_REASONS: tuple[str, ...] = (
    "not-found",            # the named artifact has no registration at all
    "generation-mismatch",  # --generation named something other than current
    "no-refresh-handle",    # handle is None — plan_format was unrecognized (§1.4)
    "handle-mismatch",      # handle.artifact != record.id — caller wired the wrong pair
    "unknown-refresh-kind", # neither "tgir_plan" nor "operator" (defensive only —
                            # ArtifactRecord.__post_init__ already closes this off
                            # for any record that made it into a live registry)
    "ref-not-found",        # the blob refresh.ref names is missing or unreadable
    "unknown-plan-format",  # the blob's own plan_format disagrees with what is known
    "execution-refused",    # the re-execution itself raised or returned an error
                            # (D-155 admission/budget refusal, or an operator error)
    "parent-vanished",      # a name in record.parents has no registration at all in
                            # the registry's current fold — advancing it to "current
                            # generation" is not answerable, so publish refuses rather
                            # than silently carrying the stale entry forward
)


class RefreshRefused(TgmsError):
    """A refresh that did not run. `reason` is always one of
    `REFUSAL_REASONS`; `.to_payload()` (inherited from `TgmsError`) is what
    the CLI prints and what a caller scripting a campaign can branch on
    without parsing prose."""

    code = "E_REFRESH_REFUSED"

    def __init__(self, message: str, *, reason: str, **details: Any) -> None:
        if reason not in REFUSAL_REASONS:
            raise InvalidArgError(f"unknown refresh refusal reason: {reason!r}",
                                  known=list(REFUSAL_REASONS))
        super().__init__(message, reason=reason, **details)
        self.reason = reason


def resolve_current(registry: Registry, name: str,
                    generation: int | None) -> ArtifactRecord:
    """The artifact refresh is being asked to act on: `name`'s current
    (latest) generation, refusing a name that is not registered at all and a
    `generation` that names anything but the current one.

    Refresh only ever *extends* the latest generation — `Registry.register`
    always writes `g+1` on top of `current(name)` (§1.1) — so there is
    nothing else a `--generation` argument could legitimately mean here
    except "the caller's belief about which generation is current, checked
    against the registry's own". This is a lookup, not a freshness
    judgement: it never opens the event log and never asks whether the
    result is stale.
    """
    current = registry.current(name)
    if current is None:
        raise RefreshRefused(f"no such artifact: {name!r}", reason="not-found", name=name)
    if generation is not None and generation != current.generation:
        raise RefreshRefused(
            f"{name!r} generation {generation} is not the current generation "
            f"({current.generation}) — refresh only ever extends the latest",
            reason="generation-mismatch", name=name, requested=generation,
            current=current.generation)
    return current


def refresh(record: ArtifactRecord, handle: RefreshHandle | None, store: Any,
           registry: Registry) -> ArtifactRecord:
    """Execute `handle` against `store`'s **current** basis and publish the
    result as `record.name`'s next generation, superseding `record` and
    leaving it byte-identical on disk (§1.1 — nothing here ever opens
    `artifacts.jsonl` for writing except through `Registry.register`, which
    only ever appends).

    `store` is a live `tgms.Store` (or anything with the same `.path`,
    `.adapter` and `store_identity`/`frontier_tt` surface `run_plan` and
    `ToolRouter` already consume) — this is the one function in the package
    allowed to hold one (§2.2).

    An unrecognized `plan_format` refuses and touches nothing (§1.4): not
    the old record, not a new one. Every other refusal below is the same
    shape — named, and nothing published.
    """
    if handle is None:
        raise RefreshRefused(
            "no refresh handle: the record's plan_format is unrecognized "
            "— refresh never guesses (§1.4)", reason="no-refresh-handle",
            name=record.name, generation=record.generation)
    if handle.artifact != record.id:
        raise RefreshRefused(
            "refresh handle does not name the record it was built for",
            reason="handle-mismatch", handle_artifact=list(handle.artifact.to_json()),
            record_artifact=list(record.id.to_json()))
    current = registry.current(record.name)
    if current is None or current.id != record.id:
        raise RefreshRefused(
            f"{record.name!r} generation {record.generation} is not the "
            f"current generation — refresh only ever extends the latest",
            reason="generation-mismatch", name=record.name,
            requested=record.generation,
            current=(current.generation if current is not None else None))

    if handle.kind == "tgir_plan":
        envelope, meta = _run_tgir_plan(handle, store)
    elif handle.kind == "operator":
        envelope, meta = _run_operator(handle, store)
    else:  # pragma: no cover — closed at ArtifactRecord construction
        # (`REFRESH_KINDS`); kept as the honest default for a `handle` a
        # caller assembled by hand rather than an unreachable assert.
        raise RefreshRefused(f"unknown refresh kind: {handle.kind!r}",
                             reason="unknown-refresh-kind", kind=handle.kind)

    return _publish(record, envelope, meta, registry)


# ---------------------------------------------------------------------------
# re-execution — the one place in this package that runs a kernel
# ---------------------------------------------------------------------------


def _blob(store: Any, ref: str) -> dict[str, Any]:
    path = Path(store.path) / ref
    if not path.exists():
        raise RefreshRefused(f"refresh ref not found: {ref}", reason="ref-not-found", ref=ref)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RefreshRefused(f"refresh ref is not readable JSON: {ref} ({e})",
                             reason="ref-not-found", ref=ref) from e


def _run_tgir_plan(handle: RefreshHandle, store: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """§1.4's `"tgir_plan"` mechanism: `handle.ref` names a `loader.dump()`
    document. The document's own `plan_format` is checked again here,
    independently of `handle.plan_format` — the handle was built at check
    time against whatever the blob looked like then; a blob that has since
    changed, or was never what the handle claimed, must not ride through on
    the handle's earlier say-so. Never guesses: an unrecognized or
    disagreeing `plan_format` refuses before `loader.load` is even called.
    """
    document = _blob(store, handle.ref)
    doc_format = document.get("plan_format")
    if doc_format not in KNOWN_PLAN_FORMATS or doc_format != handle.plan_format:
        raise RefreshRefused(
            f"unrecognized plan_format {doc_format!r} in {handle.ref}",
            reason="unknown-plan-format", ref=handle.ref, plan_format=doc_format)
    try:
        root = load_plan(document)
    except InvalidArgError as e:
        raise RefreshRefused(f"refresh plan document at {handle.ref} did not load: {e}",
                             reason="unknown-plan-format", ref=handle.ref) from e
    plan = Plan(root)
    try:
        # The current basis, not the old record's: `tt_source=store` is what
        # makes `run_plan` derive `tt_q` from the store's live frontier
        # rather than from anything stored on `record` — the new generation
        # runs at *now*, and that basis is recorded, never inherited.
        envelope = run_plan(plan, store.adapter, tt_source=store)
    except TgmsError as e:
        raise RefreshRefused(f"re-execution refused: {e}", reason="execution-refused",
                             ref=handle.ref, certificate=e.to_payload()) from e
    return envelope, dict(envelope.get("tgir") or {})


def _run_operator(handle: RefreshHandle, store: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """§1.4's `"operator"` mechanism, for an opaque-leaf artifact (every
    `snapshot_subgraph` registration, per `rollout.py:52-80`): `handle.ref`
    names an `{"op": ..., "args": {...}}` document instead of a plan."""
    document = _blob(store, handle.ref)
    op = document.get("op")
    if not op:
        raise RefreshRefused(f"operator refresh ref names no op: {handle.ref}",
                             reason="ref-not-found", ref=handle.ref)
    args = document.get("args") or {}
    from tgms.tools.server import ToolRouter  # local: only this function needs it

    router = ToolRouter(store.adapter, tt_source=store)
    envelope = router.call(op, args)
    if "error" in envelope:
        raise RefreshRefused(
            f"re-execution refused: {envelope.get('message')}",
            reason="execution-refused", ref=handle.ref, certificate=envelope)
    meta = router.leaf_meta(op, envelope)
    return envelope, meta


# ---------------------------------------------------------------------------
# publish — the one place in this package that writes a registration
# ---------------------------------------------------------------------------


def _publish(record: ArtifactRecord, envelope: dict[str, Any], meta: dict[str, Any],
            registry: Registry) -> ArtifactRecord:
    """Build and append the next generation from the fresh execution's own
    envelope. Every freshness-bearing field below — `basis`, `dependency`
    — comes from `envelope`; nothing is copied from `record` except the
    things that are properties of the *artifact's identity*, not of one
    execution: `kind` and `refresh` (the same blob refreshes it again next
    time). `parents` names the same set of parent *artifacts* as `record`'s
    — that set is identity too — but not the same generations: see
    `_advance_parents`, which re-reads each parent's live current generation
    from `registry` rather than copying `record.parents` verbatim.
    """
    dep_json = envelope.get("dependency")
    if dep_json is None:
        # `run_plan`/`call_operator` always emit one (TGIR_SPEC §5.5/§5.6);
        # absence means the envelope this function was handed did not come
        # from either — an internal-contract violation, not a normal
        # refusal a caller could have avoided by naming a different record.
        raise RefreshRefused("re-execution produced no dependency scope to register",
                             reason="execution-refused", name=record.name)
    dependency = DependencyScope.from_json(dep_json)

    basis: dict[str, Any] = {
        "tt_q": envelope["tt_q"],
        "pinned": bool(envelope.get("pinned", False)),
        "clamped": bool(envelope.get("clamped", False)),
        # taken off the scope actually registered, so the two can never
        # disagree with each other the way a hand-copied flag could.
        "tt_q_verified": dependency.tt_q_verified,
    }
    if dependency.as_of_tt is not None:
        basis["as_of_tt"] = dependency.as_of_tt

    # The plan/operator identity is structural (content-addressed over op +
    # bound args), not a property of when it ran — re-derived from the fresh
    # envelope's own `tgir` block when present, and falls back to the old
    # record's copy only for the fields that block does not carry (e.g. an
    # operator call with plan-path telemetry off — see `_run_operator`).
    plan_field = dict(record.plan)
    if meta.get("plan_digest") is not None:
        plan_field["plan_digest"] = meta["plan_digest"]
    if meta.get("node_digest") is not None:
        plan_field["node_digest"] = meta["node_digest"]

    state_field = {
        "completeness": meta.get("completeness", record.state.get("completeness")),
        "exactness": meta.get("exactness", record.state.get("exactness")),
        "refusal": None,
    }

    payload_field = None
    if envelope.get("result_digest") is not None:
        from tgms.agent.executor import ResultStore  # local: only this branch needs it
        result_store = ResultStore(_results_dir(registry))
        d = result_store.put(envelope)
        payload_field = {"result_digest": d, "result_ref": f"results/{d}.json"}

    # `provenance` is deliberately omitted, not merely left empty: §1.3 rule
    # 5 excludes it from `record_digest`, but the raw JSONL *line* still
    # carries whatever is put there, and the determinism obligation (§2.4)
    # is about the registry **chain**, which is computed over those raw
    # bytes (`extend_chain`, `eventlog.py:37-46`) — a host/pid/wall-clock
    # value here would make two replays of the same history chain
    # differently even though every `record_digest` still matched. Nothing
    # non-deterministic is recorded, full stop, rather than recorded and
    # then hoped to be harmless.
    return registry.register(
        name=record.name, kind=record.kind, plan=plan_field, basis=basis,
        state=state_field, refresh=dict(record.refresh), dependency=dependency,
        parents=_advance_parents(record, registry), payload=payload_field,
    )


def _advance_parents(record: ArtifactRecord, registry: Registry) -> tuple[ArtifactId, ...]:
    """§1.3's `parents` names the generation each parent stood at when *this*
    registration happened — never a fixed fact about the child, but a
    snapshot taken fresh each time a generation of it is written. Copying
    `record.parents` verbatim (the old generation's snapshot) forward into a
    refresh would misdate that snapshot: a refresh that runs after a parent
    has advanced is honestly described only by the parent's generation *at
    the moment this refresh executes*, read from `registry`'s live fold —
    the same fold `propagate.parent_recheck` reads back later, which is what
    keeps a refreshed child from being re-flagged forever (`parent_recheck`
    compares against `registry.current(parent.name)`, not against whatever
    generation the child happened to record before).

    A parent name no longer present in the registry at all cannot be
    advanced to anything — refused as `"parent-vanished"` rather than
    silently carrying the stale entry forward, which is the same discipline
    every other refusal in this module follows: named, and nothing
    published."""
    advanced: list[ArtifactId] = []
    for parent in record.parents:
        current = registry.current(parent.name)
        if current is None:
            raise RefreshRefused(
                f"refresh cannot advance parent {parent.name!r}: it is no longer "
                f"registered at all",
                reason="parent-vanished", name=record.name, parent=parent.name)
        advanced.append(current.id)
    return tuple(advanced)


def _results_dir(registry: Registry) -> Path:
    return registry.store_dir / "results"


__all__ = [
    "REFUSAL_REASONS", "RefreshRefused", "refresh", "resolve_current",
]
