"""F4 — concurrency faults (OSDI27 fault-matrix memo §7, Lane E task E2).

Three untested races, per the design memo
(`docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md` §7) plus two
smaller gaps the same memo calls out alongside them: the `pace_s` slow-writer
knob wired into a stress variant of `tests/test_concurrency.py`'s own
property 1 (added there, not here), and the missing one-process test for
`tgms/storage/eventlog.py`'s non-monotonic-`tt` refusal.

Test shape throughout: spawn-context multiprocess, exactly
`tests/test_concurrency.py:54-59`'s `MP = mp.get_context("spawn")` — a child
must never inherit an open store or registry handle from the parent, which
would make "a separate process" a lie on Linux's default fork and would let
an in-memory Python object (not a re-read of the file) leak the race away.

1. **Query ‖ compaction/gc across processes** (§7.1). `gc.rs`'s own module
   docstring says cross-process reader pins are not enforced — "There is no
   cross-process reader registry, deliberately" — and predicts the one
   exposure precisely: a reader lazily opening a segment it has never
   touched, after gc has removed it, fails with a *detected* IO error naming
   the file, never silently wrong data. This test does not assert which of
   "stays stable" or "fails loudly" happens — it records whichever the
   engine actually does, then asserts that whichever one happened, it did
   not silently change the reader's answer.
2. **`Registry.append` under a race** (§7.2). Two processes race to append
   generation g+1 for the same artifact name, synchronized with a
   `multiprocessing.Barrier` so both have already computed their candidate
   record — from the same, pre-race `prior` — before either calls `append`.
   Before the fix in `tgms/artifact/registry.py`, this reliably produced two
   generation-1 records on disk and a `StateError` only on the *next*
   `Registry(path)` open (recorded in the task report, not here — the
   before/after comparison was done by hand against a stashed copy of the
   unlocked code, per the task's own instructions). After the fix, exactly
   one child's `append` succeeds and the other raises `InvalidArgError`
   immediately, and a fresh `Registry(path)` reopens cleanly.
3. **Correction landing mid-refresh** (§7.3). `refresh()`'s internals give
   no natural window to land a correction in deterministically — the read
   (`run_plan`) and the publish (`registry.register`, inside `_publish`) run
   back to back with no I/O in between for a correction to race into — so
   this test monkeypatches `tgms.artifact.refresh.run_plan` (from the test
   side; `refresh.py` itself is untouched) to pause, in the refresher
   *process*, exactly between capturing the fresh execution's envelope and
   publishing it. A second, genuinely separate process lands the correction
   during that pause. The invariant probed is snapshot/belief-state
   integrity (I4): the published generation's basis must be honest about
   what it actually read, so checking it against the post-correction log
   must never say FRESH.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgms.core.errors import StateError

from .test_concurrency import (
    BATCH_ROWS,
    MP,
    _answer_hash,
    edge_count,
    seed_store,
    write_batches,
)

pytest.importorskip("tgms._engine", reason="native engine extension not built")


# --------------------------------------------------------------------------- #
# 1. query || compaction/gc across processes                                  #
# --------------------------------------------------------------------------- #


def _child_pinned_reader_tolerant(root: str, out: str, stop, ready) -> None:
    """`tests/test_concurrency.py::_child_pinned_reader`, made tolerant of
    the one failure mode property 1 alone cannot rule out here: a
    cross-process `gc` collecting a file this reader's pinned generation
    still names. Any exception during the loop is recorded, not raised past
    the process boundary, so the parent can distinguish "the answer changed"
    from "the reader could no longer answer at all, loudly" — the two
    outcomes the design memo says are both acceptable, and a changed answer
    is the only one that is not.
    """
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    s = tgms.open(root, backend="native", read_only=True)
    a = s.adapter
    gen = a.generation

    def snapshot() -> dict:
        return {
            "count": edge_count(s),
            "window": len(a.edges_columnar(vt_min=0, vt_max=10_000)["vt_s"]),
            "history": _answer_hash(call_operator(
                a, "entity_history", {"uid": "seed0", "include_edges": True})),
            "hop2": _answer_hash(call_operator(
                a, "snapshot_subgraph",
                {"seeds": ["seed0"], "hops": 2, "t_valid": 0})),
        }

    observations: list[dict] = []
    error: dict | None = None
    try:
        observations.append(snapshot())
        ready.set()
        while not stop.is_set():
            observations.append(snapshot())
    except Exception as e:  # noqa: BLE001 — the failure itself is the result
        error = {"type": type(e).__name__, "message": str(e)[:500]}
        ready.set()  # in case the very first snapshot() is what failed
    Path(out).write_text(json.dumps({
        "generation_open": gen,
        "generation_close": a.generation,
        "observations": observations,
        "distinct": sorted({json.dumps(o, sort_keys=True) for o in observations}),
        "error": error,
    }))
    try:
        s.close()
    except Exception:  # noqa: BLE001 — best-effort; the result is already written
        pass


def _child_writer_then_compact_gc(root: str, extra_batches: int, keep_last: int) -> None:
    import tgms

    write_batches(Path(root), batches=extra_batches, rows=BATCH_ROWS)
    s = tgms.open(root, backend="native")
    s.adapter.compact()
    s.adapter.gc(keep_last=keep_last)
    s.close()


def test_query_survives_or_loudly_fails_under_cross_process_compaction_gc(tmp_path: Path):
    """A reader pins one generation; a second process commits more batches
    and then runs `compact()` + `gc(keep_last=2)` while the reader is still
    mid-loop. `gc.rs` documents that a cross-process reader's pin is not
    enforced, so this is not a test that gc must leave the reader alone —
    it is a test that gc cannot make the reader silently *wrong*.
    """
    seed_store(tmp_path, base_rows=16)
    # Advance a few generations before the reader ever opens, so its pin
    # names something a subsequent gc could plausibly consider collectible
    # once the writer moves further ahead below.
    write_batches(tmp_path, batches=3, rows=BATCH_ROWS)

    out = tmp_path / "gc_race.json"
    stop, ready = MP.Event(), MP.Event()
    reader = MP.Process(target=_child_pinned_reader_tolerant,
                        args=(str(tmp_path), str(out), stop, ready))
    reader.start()
    try:
        assert ready.wait(180), "the reader never opened the store"

        writer = MP.Process(target=_child_writer_then_compact_gc,
                            args=(str(tmp_path), 6, 2))
        writer.start()
        writer.join(300)
        assert writer.exitcode == 0, f"the writer/compactor failed: exit {writer.exitcode}"
    finally:
        stop.set()
        reader.join(180)
        if reader.is_alive():
            reader.terminate()
            reader.join(30)

    got = json.loads(out.read_text())
    assert len(got["observations"]) > 0, "the reader never got to answer even once"

    if got["error"] is None:
        # Outcome A: answer-stable. The reader's pinned generation kept
        # answering identically across the whole compact()+gc() run — the
        # files it had already touched (open fds / mmaps survive an
        # unlink on POSIX) stayed valid even though gc ran underneath it.
        assert len(got["distinct"]) == 1, (
            f"a pinned reader saw {len(got['distinct'])} DIFFERENT answers across "
            f"a cross-process compact()+gc() — a changed answer, which is the one "
            f"outcome this test must never see: {got['distinct']}")
    else:
        # Outcome B: loud failure. Acceptable only if it is loud and named —
        # an exception identifying what went missing, never a silent wrong
        # answer and never the reader process just vanishing unexplained.
        assert reader.exitcode != 0
        msg = got["error"]["message"]
        assert msg, "the reader failed but recorded no message — not a NAMED failure"


# --------------------------------------------------------------------------- #
# 2. Registry.append under a race                                             #
# --------------------------------------------------------------------------- #


def _child_append_race(store: str, name: str, generation: int, supersedes_gen: int | None,
                       tt_q: int, barrier, out: str) -> None:
    from tgms.artifact.record import ArtifactId, ArtifactRecord, StepDependency
    from tgms.artifact.registry import Registry
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    store_path = Path(store)
    log = EventLog(store_path / "eventlog.jsonl")
    identity = store_identity(log.header(), log.first_batch())
    scope = DependencyScope(store=identity, tt_q=tt_q,
                            terms=(ScopeTerm(targets=Targets(nodes=("A",))),))
    supersedes = ArtifactId(name, supersedes_gen) if supersedes_gen is not None else None
    record = ArtifactRecord(
        name=name, generation=generation, kind="query_result", store=identity,
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": tt_q, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=(StepDependency("s1", scope),), supersedes=supersedes,
    )
    # Each child opens its own `Registry` — the whole point: two independent
    # in-memory folds, exactly as two independent processes would have.
    reg = Registry(store_path)
    barrier.wait()  # both children have now built `record` from the same prior
    try:
        reg.append(record)
        result: dict = {"outcome": "ok"}
    except Exception as e:  # noqa: BLE001 — the error IS the result being tested
        result = {"outcome": "error", "type": type(e).__name__, "message": str(e)[:500]}
    Path(out).write_text(json.dumps(result))


def test_registry_append_race_exactly_one_writer_wins(tmp_path: Path):
    """Two processes race to append generation 1 of the same artifact name
    on one registry file, synchronized so both have already computed their
    candidate record from the *same* generation-0 `prior` before either
    calls `append` — precisely the unlocked check-then-act the design memo
    names at `registry.py:219-233` (now the locked `_append_locked`).
    Exactly one must succeed; the other must raise, immediately, in its own
    `append` call — not silently write a corrupt second generation-1 record
    that only a later `Registry(path)` open discovers.
    """
    from tgms.artifact.record import StepDependency
    from tgms.artifact.registry import Registry
    from tgms.storage.eventlog import EventLog
    from tgms.storage.base import make_op
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    node_a = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(10, [node_a])
    identity = store_identity(log.header(), log.first_batch())
    scope0 = DependencyScope(store=identity, tt_q=10,
                             terms=(ScopeTerm(targets=Targets(nodes=("A",))),))

    reg = Registry(tmp_path)
    gen0 = reg.register(
        name="racer", kind="query_result", store=identity,
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": 10, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=(StepDependency("s1", scope0),),
    )
    assert gen0.generation == 0

    barrier = MP.Barrier(2)
    out_a, out_b = tmp_path / "racer_a.json", tmp_path / "racer_b.json"
    pa = MP.Process(target=_child_append_race,
                    args=(str(tmp_path), "racer", 1, 0, 20, barrier, str(out_a)))
    pb = MP.Process(target=_child_append_race,
                    args=(str(tmp_path), "racer", 1, 0, 20, barrier, str(out_b)))
    pa.start()
    pb.start()
    pa.join(60)
    pb.join(60)
    assert pa.exitcode == 0 and pb.exitcode == 0, (pa.exitcode, pb.exitcode)

    ra, rb = json.loads(out_a.read_text()), json.loads(out_b.read_text())
    outcomes = sorted([ra["outcome"], rb["outcome"]])
    assert outcomes == ["error", "ok"], (
        f"expected exactly one winner and one loser, got: a={ra} b={rb}")
    loser = ra if ra["outcome"] == "error" else rb
    assert loser["type"] == "InvalidArgError", loser
    assert "another writer already appended it" in loser["message"], loser

    # the registry must reopen clean, with a single, valid generation-1 line
    reg2 = Registry(tmp_path)
    assert reg2.current("racer") is not None
    assert reg2.current("racer").generation == 1
    assert [r.generation for r in reg2.history("racer")] == [0, 1]


# --------------------------------------------------------------------------- #
# 3. correction landing mid-refresh                                           #
# --------------------------------------------------------------------------- #


def _child_paced_refresher(store_dir: str, name: str, out: str, ready_evt, go_evt) -> None:
    """Run `refresh()` on `name`'s current generation, pausing — via a
    monkeypatch of `tgms.artifact.refresh.run_plan`, applied from this test
    process only, never inside `refresh.py` itself — right after the fresh
    execution's envelope is captured (the "read") and before `_publish`
    writes the new generation (the "publish"). Signals `ready_evt` at that
    exact point and waits on `go_evt` before letting the real `_publish`
    run, so a correction from a different process can be made to land
    inside the window deterministically, with no sleep-based guessing.
    """
    import tgms
    import tgms.artifact.refresh as refresh_mod
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.storage.eventlog import EventLog

    original_run_plan = refresh_mod.run_plan

    def paced_run_plan(*a, **kw):
        env = original_run_plan(*a, **kw)
        ready_evt.set()
        go_evt.wait(60)
        return env

    refresh_mod.run_plan = paced_run_plan
    try:
        store = tgms.open(store_dir, backend="native")
        registry = Registry(store_dir)
        gen0 = registry.current(name)
        verdict = check_artifact(gen0, EventLog(Path(store_dir) / "eventlog.jsonl"))
        gen1 = refresh_mod.refresh(gen0, verdict.refresh, store, registry)
        Path(out).write_text(json.dumps({
            "generation": gen1.generation,
            "tt_q": gen1.basis["tt_q"],
        }))
        store.close()
    finally:
        refresh_mod.run_plan = original_run_plan
        ready_evt.set()  # in case something above raised before pausing


def _child_corrector(store_dir: str, uid: str, props: dict) -> None:
    import tgms
    from tgms.core.model import EntityRef

    s = tgms.open(store_dir, backend="native")
    s.correct(EntityRef(kind="node", uid=uid), props)
    s.close()


def test_correction_landing_mid_refresh_never_reports_fresh_against_it(tmp_path: Path):
    """A correction lands, from a genuinely separate process, in the window
    between `refresh()`'s read (`run_plan`) and its publish
    (`registry.register`, inside `_publish`). The published generation's
    basis is fixed by what `run_plan` actually saw — this test's job is to
    confirm that holds under the race, not to assume it: checking the
    published generation against the post-correction log must report
    `POSSIBLY_STALE` (I4's own words: never `FRESH` contradicted by
    recomputation), with a witness naming the correction.
    """
    import tgms
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.depscope import DependencyScope
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import dump
    from tgms.tgir.node import NodeScan
    from tgms.tgir.plan import Plan

    store_dir = tmp_path
    store = tgms.open(store_dir, backend="native")
    store.assert_node("A", "N", {})

    scan = NodeScan("p", uids=("A",))
    env = run_plan(Plan(scan), store.adapter, tt_source=store)
    tgir = env["tgir"]
    (store_dir / "plans").mkdir()
    (store_dir / "plans" / f"{tgir['plan_digest']}.json").write_text(json.dumps(dump(scan)))

    registry = Registry(store_dir)
    dependency = DependencyScope.from_json(env["dependency"])
    registry.register(
        name="mid-refresh", kind="query_result",
        plan={"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
              "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
               "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": tgir.get("completeness", "unknown"),
               "exactness": tgir.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                "basis_policy": "open"},
        dependency=dependency,
    )
    store.close()

    out = tmp_path / "refresh_result.json"
    ready, go = MP.Event(), MP.Event()
    refresher = MP.Process(target=_child_paced_refresher,
                          args=(str(store_dir), "mid-refresh", str(out), ready, go))
    refresher.start()
    try:
        assert ready.wait(60), "refresh() never reached the paced read"
        corrector = MP.Process(target=_child_corrector, args=(str(store_dir), "A", {"x": 1}))
        corrector.start()
        corrector.join(60)
        assert corrector.exitcode == 0, "the correction failed to land"
    finally:
        go.set()
        refresher.join(60)
    assert refresher.exitcode == 0, "the refresher process failed"

    result = json.loads(out.read_text())
    assert result["generation"] == 1

    registry2 = Registry(store_dir)
    gen1 = registry2.at("mid-refresh", 1)
    assert gen1 is not None
    verdict = check_artifact(gen1, EventLog(store_dir / "eventlog.jsonl"))
    assert not verdict.actionable_fresh, (
        "the published generation reported FRESH even though a correction "
        "landed inside its own read-to-publish window — I4 contradicted by "
        "recomputation")
    assert verdict.steps.witnesses, "POSSIBLY_STALE must name at least one witness"


# --------------------------------------------------------------------------- #
# 5. the event log's non-monotonic-tt refusal (one process)                   #
# --------------------------------------------------------------------------- #


def test_replay_refuses_non_monotonic_tt(tmp_path: Path):
    """`tgms/storage/eventlog.py`'s `replay()` (~line 247) raises `StateError`
    if a batch's `tt` does not strictly increase over the previous one.
    `EventLog.append` itself does not enforce ordering — it write-aheads
    whatever `tt` it is handed — so a log that somehow ends up with a
    non-monotonic `tt` (a hand-rolled writer, a bug upstream of the clock)
    must be refused at replay, never silently applied as if time ran
    backwards. This was previously untested."""
    from tgms.storage.base import make_op
    from tgms.storage.eventlog import EventLog, replay
    from tgms.storage.native import NativeAdapter

    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(20, [make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)])
    log.append(10, [make_op("assert_node", uid="B", label="N", props={}, vt_s=0, vt_e=100)])

    adapter = NativeAdapter(tmp_path / "native_target")
    try:
        with pytest.raises(StateError, match="non-monotonic tt"):
            replay(tmp_path / "eventlog.jsonl", adapter)
    finally:
        adapter.close()
