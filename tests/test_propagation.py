"""P2.2 — the two-hop propagation demo (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md`
§5 P2.2; Gate C, §10). Two halves, split the same way the P1.2/P2.1 test
files are split:

1. `tgms.artifact.propagate.parent_recheck` in isolation, against hand-built
   registrations -- the `tests/test_artifact_registry.py` fixture idiom
   (`_fields`/`_scope`, no live store needed: `parent_recheck` never opens
   one). Fast, and pins the walk's own contract independently of the demo
   arc: registry-chain-only, one level, no cascade, no adjudication.
2. `scripts/demo_propagation.py` run end to end, twice, as a subprocess --
   the `tests/test_artifact_refresh.py::test_boundary_script_is_green` /
   `test_deterministic_replay_identical_registry_bytes` idiom combined: the
   script's own exit code discharges the full arc's assertions, and a
   byte-identical `artifacts.jsonl`/`eventlog.jsonl` pair across two fresh
   `--store-dir`s discharges §2.4's determinism obligation for this arc.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tgms.core.model import canonical_json
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

from tgms.artifact.propagate import REASON_PARENT_GENERATION_ADVANCED, parent_recheck
from tgms.artifact.record import ArtifactId, StepDependency
from tgms.artifact.registry import Registry

ROOT = Path(__file__).resolve().parent.parent
NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)

# ---------------------------------------------------------------------------
# fixture helpers -- tests/test_artifact_registry.py's idiom, reused verbatim
# ---------------------------------------------------------------------------


def _store() -> Path:
    return Path(tempfile.mkdtemp())


def _log(store: Path, *batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(store / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 1000) -> DependencyScope:
    return DependencyScope(store=_identity(log), tt_q=tt_q, terms=terms)


def _fields(name: str, log: EventLog, scope: DependencyScope,
           **overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = dict(
        name=name, kind="query_result", store=_identity(log),
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": scope.tt_q, "pinned": False, "clamped": False,
               "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope)],
    )
    out.update(overrides)
    return out


def _some_scope(log: EventLog) -> DependencyScope:
    return _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)


# ---------------------------------------------------------------------------
# 1 -- parent_recheck in isolation
# ---------------------------------------------------------------------------


def test_flags_registrant_when_recorded_parent_generation_is_behind_current() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    b0 = reg.register(**_fields("B", log, scope, parents=[a0.id]))
    a1 = reg.register(**_fields("A", log, scope))  # A's generation 1

    result = parent_recheck(a1.id, reg)
    assert [c.record.id for c in result.candidates] == [b0.id]
    threat = result.candidates[0].threats[0]
    assert threat.parent == a0.id
    assert threat.parent_current == a1.id
    assert threat.reason == REASON_PARENT_GENERATION_ADVANCED
    assert bool(result) is True
    assert list(result) == list(result.candidates)


def test_does_not_flag_when_recorded_parent_generation_matches_current() -> None:
    """A registrant whose `parents` already names the *current* generation
    of its parent is not a candidate -- there is nothing for it to have
    missed."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    reg.register(**_fields("B", log, scope, parents=[a0.id]))  # names A@0, A is at 0

    result = parent_recheck(a0.id, reg)
    assert result.candidates == ()
    assert bool(result) is False


def test_no_parents_never_a_candidate() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    reg.register(**_fields("C", log, scope))  # no parents at all
    reg.register(**_fields("A", log, scope))  # A -> generation 1

    result = parent_recheck(a0.id, reg)
    assert result.candidates == ()


def test_unrelated_parent_name_is_not_flagged() -> None:
    """A registrant depending on some other name entirely is untouched by a
    walk of `"A"`, even if that other name has also moved."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    z0 = reg.register(**_fields("Z", log, scope))
    reg.register(**_fields("D", log, scope, parents=[z0.id]))
    reg.register(**_fields("Z", log, scope))  # Z -> generation 1
    reg.register(**_fields("A", log, scope))  # A -> generation 1

    result = parent_recheck(a0.id, reg)
    assert result.candidates == ()  # D depends on Z, not A


def test_walks_one_level_only_no_cascade() -> None:
    """B depends on A, C depends on B. Refreshing A (only) flags B but not
    C -- C's own `parents` still names B's generation faithfully, since B
    has not itself moved. This is the "no autonomous cascade" contract: a
    caller wanting C reconsidered must call `parent_recheck` again with B's
    (refreshed) id, once B has actually moved."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    b0 = reg.register(**_fields("B", log, scope, parents=[a0.id]))
    reg.register(**_fields("C", log, scope, parents=[b0.id]))
    a1 = reg.register(**_fields("A", log, scope))  # A -> generation 1; B, C untouched

    result = parent_recheck(a1.id, reg)
    assert [c.record.id for c in result.candidates] == [b0.id]

    # once B is (hypothetically) refreshed, a second, independent call over
    # B's new id is what would find C -- never implied by the first call.
    b1 = reg.register(**_fields("B", log, scope, parents=[a0.id]))  # B -> generation 1
    result2 = parent_recheck(b1.id, reg)
    assert [c.record.id for c in result2.candidates] == [reg.current("C").id]


def test_multiple_parents_only_the_advanced_ones_are_named() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    x0 = reg.register(**_fields("X", log, scope))
    reg.register(**_fields("E", log, scope, parents=[a0.id, x0.id]))
    a1 = reg.register(**_fields("A", log, scope))  # only A moves

    result = parent_recheck(a1.id, reg)
    assert len(result.candidates) == 1
    threats = result.candidates[0].threats
    assert len(threats) == 1
    assert threats[0].parent == a0.id


def test_the_walked_generation_argument_is_not_trusted_the_live_fold_is() -> None:
    """§'s own point (module docstring, "where the answer comes from"): the
    `generation` on the `refreshed` id passed in is never read for the
    comparison -- only `.name` selects the edge, and the live
    `registry.current(...)` decides. Passing a stale `ArtifactId` (an old
    generation of A, not even the current one) still finds every registrant
    behind the registry's *actual* current generation."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    b0 = reg.register(**_fields("B", log, scope, parents=[a0.id]))
    reg.register(**_fields("A", log, scope))  # A -> generation 1
    a2 = reg.register(**_fields("A", log, scope))  # A -> generation 2

    # pass generation 0 -- itself already stale -- and still get the right answer
    result = parent_recheck(ArtifactId("A", 0), reg)
    assert [c.record.id for c in result.candidates] == [b0.id]
    assert result.candidates[0].threats[0].parent_current == a2.id


def test_empty_registry() -> None:
    store = _store()
    _log(store, (10, [NODE_A]))
    reg = Registry(store)
    result = parent_recheck(ArtifactId("nope", 0), reg)
    assert result.candidates == ()


def test_to_json_shape() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _some_scope(log)
    reg = Registry(store)

    a0 = reg.register(**_fields("A", log, scope))
    reg.register(**_fields("B", log, scope, parents=[a0.id]))
    a1 = reg.register(**_fields("A", log, scope))

    result = parent_recheck(a1.id, reg)
    doc = result.to_json()
    assert doc == {
        "candidates": [{
            "artifact": ["B", 0],
            "threats": [{
                "parent": ["A", 0], "parent_current": ["A", 1],
                "reason": "parent-generation-advanced",
            }],
        }],
    }
    # round-trips through json.dumps cleanly (canonical_json too)
    assert json.loads(json.dumps(doc)) == doc
    assert canonical_json(doc)


# ---------------------------------------------------------------------------
# 2 -- the demo script, end to end and deterministic
# ---------------------------------------------------------------------------


def _run_demo(store_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, TGMS_TEST_BACKEND="native")
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_propagation.py"),
         "--store-dir", str(store_dir)],
        cwd=ROOT, capture_output=True, text=True, env=env)


def test_demo_script_runs_the_full_arc_and_exits_zero() -> None:
    store_dir = _store()
    result = _run_demo(store_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert "all propagation-arc assertions passed" in result.stdout
    # the receipted steps are all in the transcript
    for marker in ("step 0:", "step 1:", "step 2:", "step 3:", "step 4:", "step 5:"):
        assert marker in result.stdout


def test_demo_script_is_deterministic_across_fresh_directories() -> None:
    """§2.4's obligation, applied to this arc: running the script twice into
    two independent fresh directories produces byte-identical
    `artifacts.jsonl` and `eventlog.jsonl`, and therefore identical
    `record_digest` chains -- the same idiom as
    `tests/test_artifact_refresh.py::test_deterministic_replay_identical_registry_bytes`.
    """
    d1, d2 = _store(), _store()
    r1, r2 = _run_demo(d1), _run_demo(d2)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert r2.returncode == 0, r2.stdout + r2.stderr

    eventlog1 = (d1 / "eventlog.jsonl").read_bytes()
    eventlog2 = (d2 / "eventlog.jsonl").read_bytes()
    assert eventlog1 == eventlog2  # sanity: the inputs really did match

    artifacts1 = (d1 / "artifacts.jsonl").read_bytes()
    artifacts2 = (d2 / "artifacts.jsonl").read_bytes()
    assert artifacts1 == artifacts2

    reg1, reg2 = Registry(d1), Registry(d2)
    assert reg1.checkpoint() == reg2.checkpoint()
    for name in reg1.names():
        for rec1, rec2 in zip(reg1.history(name), reg2.history(name)):
            assert rec1.record_digest == rec2.record_digest


def test_boundary_script_is_green() -> None:
    """Re-asserted here too (already covered by
    `tests/test_artifact_check.py`) since this is the file that would catch
    `tgms/artifact/propagate.py` accidentally growing an import off the
    guarded allowlist."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_freshness_boundary.py")],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tgms/artifact/propagate.py" in result.stdout


def test_propagate_module_is_on_the_guarded_allowlist() -> None:
    """The mirror image of
    `tests/test_artifact_refresh.py::test_refresh_module_is_not_on_the_guarded_allowlist`:
    `propagate.py` reads only the registry's own fold, opens no store, and
    belongs on the allowlist (unlike `refresh.py`)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_freshness_boundary", ROOT / "scripts" / "check_freshness_boundary.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "tgms/artifact/propagate.py" in mod.GUARDED
