"""P1.2 — `check_artifact` and the witness/refresh API (M5 design memo §5;
`docs/design/M5_DESIGN.md`).

Discharges the verdict-shaped half of the memo's §8 test plan: the version
gate, verdict equivalence against `check_trace`, the D-153 exemption
passthrough (mandatory, present-but-empty when nothing was exempted),
refresh-handle construction, the `level1` no-op fail-safe, and the boundary
gate (including the "a deliberate store import gets caught" positive case).
Registry-shaped tests (round-trip, replay, supersession, tamper, the CLI
smoke) live in `tests/test_artifact_registry.py`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import check_trace
from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity
from tgms.artifact.record import ArtifactRecord, StepDependency
from tgms.artifact.registry import Registry
from tgms.artifact.witness import (
    KNOWN_PLAN_FORMATS, RefreshHandle, check_artifact, render_verdict,
)

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)


def _log(*batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 10, **kw: Any) -> DependencyScope:
    return DependencyScope(store=_identity(log), tt_q=tt_q, terms=terms, **kw)


def _register(log: EventLog, scope: DependencyScope, *, name: str = "wmc",
             refresh_kind: str = "tgir_plan", plan_format: int = 1) -> ArtifactRecord:
    registry = Registry(log.path.parent)
    return registry.register(
        name=name, kind="query_result", store=_identity(log),
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": plan_format,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": scope.tt_q, "pinned": scope.pinned, "clamped": scope.clamped,
               "tt_q_verified": scope.tt_q_verified,
               **({"as_of_tt": scope.as_of_tt} if scope.as_of_tt is not None else {})},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": refresh_kind, "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope)],
    )


# ---------------------------------------------------------------------------
# 5 — version gate
# ---------------------------------------------------------------------------


def test_version_gate_refuses_never_fresh() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope)
    bumped = ArtifactRecord.from_json({**record.to_json(), "version": 2})
    assert bumped.version == 2

    verdict = check_artifact(bumped, log)
    assert not verdict.actionable_fresh
    assert verdict.steps.per_step[0][1].reason == "scope-version"
    assert verdict.refresh is None
    assert verdict.terms == ()


# ---------------------------------------------------------------------------
# 6 — verdict equivalence with check_trace
# ---------------------------------------------------------------------------


def test_verdict_equivalence_with_check_trace() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope)
    log.append(20, [make_op("assert_node", uid="A", label="N", props={"x": 1},
                            vt_s=5, vt_e=50)])

    verdict = check_artifact(record, log)
    trace_verdict = check_trace(record.to_json(), log)
    assert verdict.steps == trace_verdict
    assert not verdict.actionable_fresh


def test_fresh_when_nothing_intersects() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope)
    log.append(20, [make_op("assert_node", uid="Z", label="N", props={}, vt_s=0, vt_e=10)])

    verdict = check_artifact(record, log)
    assert verdict.actionable_fresh
    assert verdict.terms == ()
    assert verdict.exempt == ()
    assert "Nothing written since" in render_verdict(verdict, produced_tt=record.registered_tt)


# ---------------------------------------------------------------------------
# 7 — exemption passthrough (D-153)
# ---------------------------------------------------------------------------


def test_exemption_passthrough_when_pinned() -> None:
    log = _log((10, [NODE_A]))
    log.append(20, [make_op("assert_node", uid="A", label="N", props={"x": 1},
                            vt_s=5, vt_e=50)])
    # a scope that read as of tt=15: the batch at tt=20 is in the checked
    # suffix (tt_q=10 < 20) but exempted by step 8a because 20 > as_of_tt=15.
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10, as_of_tt=15)
    record = _register(log, scope)

    verdict = check_artifact(record, log)
    assert verdict.actionable_fresh  # exempted, not merely intersecting
    assert len(verdict.exempt) == 1
    receipt = verdict.exempt[0]
    assert receipt.step_id == "s1"
    assert receipt.basis == 15
    assert receipt.batches == 1
    assert receipt.theorem == "T1"
    rendered = render_verdict(verdict, produced_tt=record.registered_tt)
    assert "exempt" in rendered
    assert "basis=15" in rendered


def test_exempt_present_but_empty_when_nothing_exempted() -> None:
    """§5.4's fail-safe: the field is always a tuple, never absent, and it
    is empty rather than `None` when D-153 has nothing to report — the same
    shape holds whether or not `as_of_tt` was ever set."""
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))  # no as_of_tt
    record = _register(log, scope)
    verdict = check_artifact(record, log)
    assert verdict.exempt == ()
    assert isinstance(verdict.exempt, tuple)
    assert "exempt" not in verdict.to_json()  # to_json omits an empty receipt list


# ---------------------------------------------------------------------------
# refresh handles (§1.4, §5.5)
# ---------------------------------------------------------------------------


def test_refresh_handle_for_a_recognized_plan_format() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope, plan_format=1)
    verdict = check_artifact(record, log)
    assert verdict.refresh == RefreshHandle(record.id, "tgir_plan", "plans/pd.json", 1, "open")


def test_refresh_handle_none_for_unrecognized_plan_format() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope, plan_format=99)
    assert 99 not in KNOWN_PLAN_FORMATS
    verdict = check_artifact(record, log)
    assert verdict.refresh is None
    # the verdict itself is unaffected by the unreadable plan_format (§1.4)
    assert verdict.actionable_fresh


def test_refresh_handle_for_an_operator_kind_opaque_leaf() -> None:
    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope, refresh_kind="operator")
    verdict = check_artifact(record, log)
    assert verdict.refresh is not None
    assert verdict.refresh.kind == "operator"
    assert verdict.refresh.plan_format is None


# ---------------------------------------------------------------------------
# the level1 seam — a documented no-op until tgms.tgir.level1 exists
# ---------------------------------------------------------------------------


def test_level1_flag_is_currently_a_no_op() -> None:
    import importlib.util
    assert importlib.util.find_spec("tgms.tgir.level1") is None, (
        "tgms.tgir.level1 now exists — check_artifact's level1 no-op fail-safe "
        "in tgms/artifact/witness.py needs to be wired up to it (P1.3)")

    log = _log((10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))))
    record = _register(log, scope)
    log.append(20, [make_op("assert_node", uid="A", label="N", props={"x": 1},
                            vt_s=5, vt_e=50)])
    with_level1 = check_artifact(record, log, level1=True)
    without_level1 = check_artifact(record, log, level1=False)
    assert with_level1.to_json() == without_level1.to_json()
    assert all(t.level == "level-0" for t in with_level1.terms)


# ---------------------------------------------------------------------------
# the honesty clause (§5.6) — no is_stale anywhere
# ---------------------------------------------------------------------------


def test_no_is_stale_surface_anywhere() -> None:
    import tgms.artifact.witness as witness_mod

    for name in ("ArtifactVerdict",):
        cls = getattr(witness_mod, name)
        fields = getattr(cls, "__dataclass_fields__", {})
        assert "is_stale" not in fields
        assert not hasattr(cls, "is_stale")
    # `.actionable_fresh` is the only affirmative question; a bare `stale`
    # property (as opposed to prose *about* the rule, which the docstring
    # legitimately carries) would be a code-level attribute.
    assert not hasattr(witness_mod.ArtifactVerdict, "stale")


# ---------------------------------------------------------------------------
# the boundary gate (§7.1) — including the positive "would be caught" case
# ---------------------------------------------------------------------------


def test_boundary_script_is_green() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_freshness_boundary.py")],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_boundary_script_catches_a_deliberate_store_import(tmp_path: Path) -> None:
    """`witness.py:20-23`'s AST walk catches a function-body import too — the
    usual way this rule gets broken while looking clean."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_freshness_boundary",
        Path(__file__).resolve().parent.parent / "scripts" / "check_freshness_boundary.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    bad = tmp_path / "witness_with_store_import.py"
    bad.write_text(
        "def check_artifact(record, log):\n"
        "    from tgms.store import Store\n"
        "    return Store\n")
    found = mod.imports_of(bad)
    assert any(name == "tgms.store" for _line, name in found)
    assert "tgms.store" not in mod.ALLOWED["tgms/artifact/witness.py"]
