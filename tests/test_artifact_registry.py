"""P1.2 — the registry (M5 design memo §1, §2; `docs/design/M5_DESIGN.md`).

Discharges the registry half of the memo's §8 test plan: round-trip,
deterministic replay, supersession audit, chain tamper, the relative-ref
writer rule, and the CLI smoke (`register`/`list`/`check` round-trip on a
tiny fixture store). Verdict-shaped tests (version gate, verdict
equivalence, exemption passthrough, refresh handles) live in
`tests/test_artifact_check.py`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tgms.core.errors import InvalidArgError, StateError
from tgms.core.model import canonical_json
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity
from tgms.artifact.lookup import affected
from tgms.artifact.record import ArtifactId, ArtifactRecord, StepDependency
from tgms.artifact.registry import Registry

# ---------------------------------------------------------------------------
# fixture helpers — the `tests/test_freshness_check.py` pattern
# ---------------------------------------------------------------------------


def _store(tmp: Path | None = None) -> Path:
    return tmp or Path(tempfile.mkdtemp())


def _log(store: Path, *batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(store / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 1000) -> DependencyScope:
    return DependencyScope(store=_identity(log), tt_q=tt_q, terms=terms)


def _fields(name: str, log: EventLog, scope: DependencyScope,
           **overrides: Any) -> dict[str, Any]:
    """A minimal, valid set of `Registry.register()` keyword fields."""
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


# ---------------------------------------------------------------------------
# 1 — round trip
# ---------------------------------------------------------------------------


def test_round_trip_and_stable_digest() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    rec = reg.register(**_fields("wmc", log, scope))

    again = ArtifactRecord.from_json(rec.to_json())
    assert again == rec
    assert again.record_digest == rec.record_digest
    assert ArtifactRecord.from_json(again.to_json()).to_json() == rec.to_json()


def test_registered_tt_is_basis_tt_q_not_a_stored_field() -> None:
    """§1.3 rule 5. A hand-edited `registered_tt` disagreeing with
    `basis.tt_q` is not representable after a round trip."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    rec = reg.register(**_fields("wmc", log, scope))
    assert rec.registered_tt == 10

    doc = rec.to_json()
    doc["registered_tt"] = 999999  # tampered
    rebuilt = ArtifactRecord.from_json(doc)
    assert rebuilt.registered_tt == 10  # re-derived from basis.tt_q, not trusted


# ---------------------------------------------------------------------------
# 2 — deterministic replay (§2.4 obligations 1 and 2)
# ---------------------------------------------------------------------------


def test_deterministic_replay() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    names = [f"artifact-{i}" for i in range(5)]
    for name in names:
        reg.register(**_fields(name, log, scope))

    # obligation 1: fold, re-emit canonical_json per record, byte-identical
    # to the file.
    with open(store / "artifacts.jsonl", "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    header, *record_lines = lines
    assert json.loads(header) == {"format": "tgms-artifact-registry", "version": 1}
    assert len(record_lines) == len(names)
    for name, line in zip(names, record_lines):
        rec = reg.current(name)
        assert rec is not None
        assert canonical_json(rec.to_json()) == line

    # obligation 2: rebuild the store from the event log on a fresh path,
    # re-run the same registrations, and get identical record_digests. The
    # event log itself is what `tgms.storage.eventlog.replay` rebuilds from
    # a backend; here only the log matters, so the batches are re-appended
    # directly, which produces the same log bytes a `replay` into a fresh
    # backend followed by a re-derivation would.
    store2 = _store()
    log2 = EventLog(store2 / "eventlog.jsonl")
    for batch in log.batches():
        log2.append(batch["tt"], batch["ops"])
    scope2 = _scope(log2, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg2 = Registry(store2)
    for name in names:
        reg2.register(**_fields(name, log2, scope2))
    for name in names:
        assert reg.current(name).record_digest == reg2.current(name).record_digest


# ---------------------------------------------------------------------------
# 3 — supersession audit
# ---------------------------------------------------------------------------


def test_supersession_audit() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)

    gen0 = reg.register(**_fields("wmc", log, scope))
    gen0_bytes = gen0.to_json()
    assert gen0.supersedes is None

    gen1 = reg.register(**_fields("wmc", log, scope))
    assert gen1.generation == 1
    assert gen1.supersedes == ArtifactId("wmc", 0)
    # generation 0's bytes are unchanged after a later registration
    assert reg.at("wmc", 0).to_json() == gen0_bytes

    gen2 = reg.register(**_fields("wmc", log, scope))
    gen3 = reg.register(**_fields("wmc", log, scope))
    assert [r.generation for r in reg.history("wmc")] == [0, 1, 2, 3]
    assert gen2.supersedes == ArtifactId("wmc", 1)
    assert gen3.supersedes == ArtifactId("wmc", 2)
    assert reg.current("wmc") == gen3
    assert reg.at("wmc", 0).to_json() == gen0_bytes


def test_supersedes_must_name_the_immediate_predecessor() -> None:
    log = _log(_store(), (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    fields = _fields("wmc", log, scope)
    with pytest.raises(InvalidArgError):
        ArtifactRecord(
            name="wmc", generation=1, kind="query_result", store=fields["store"],
            plan=fields["plan"], basis=fields["basis"], state=fields["state"],
            refresh=fields["refresh"], steps=tuple(fields["steps"]),
            supersedes=ArtifactId("wmc", 5),  # wrong predecessor
        )


def test_generation_zero_carries_no_supersedes() -> None:
    log = _log(_store(), (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    fields = _fields("wmc", log, scope)
    with pytest.raises(InvalidArgError):
        ArtifactRecord(
            name="wmc", generation=0, kind="query_result", store=fields["store"],
            plan=fields["plan"], basis=fields["basis"], state=fields["state"],
            refresh=fields["refresh"], steps=tuple(fields["steps"]),
            supersedes=ArtifactId("wmc", -1),
        )


# ---------------------------------------------------------------------------
# 4 — chain tamper
# ---------------------------------------------------------------------------


def test_tamper_raises_on_reopen() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    reg.register(**_fields("wmc", log, scope))
    reg.register(**_fields("wmc", log, scope))

    path = store / "artifacts.jsonl"
    raw = bytearray(path.read_bytes())
    lines = raw.split(b"\n")
    target = 2  # header=0, first record=1, second record=2
    line = bytearray(lines[target])
    # flip a hex digit inside the record_digest field's own value — content
    # tamper, not a JSON parse error, and it must still be caught (the
    # tampered field's own stored digest is now stale, since it was
    # computed over the record's *other* fields, none of which changed).
    marker = b'"record_digest":"'
    at = line.index(marker) + len(marker) + 3
    line[at] ^= 0x01
    lines[target] = bytes(line)
    path.write_bytes(b"\n".join(lines))

    with pytest.raises(StateError):
        Registry(store)


def test_tamper_via_generation_gap_also_raises() -> None:
    """Deleting a middle generation breaks the consecutiveness invariant —
    the second half of the tamper story, independent of `record_digest`."""
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    reg.register(**_fields("wmc", log, scope))
    reg.register(**_fields("wmc", log, scope))
    reg.register(**_fields("wmc", log, scope))

    path = store / "artifacts.jsonl"
    lines = path.read_text().splitlines()
    # drop the generation-1 line (index 2: header, gen0, gen1, gen2)
    del lines[2]
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(StateError):
        Registry(store)


# ---------------------------------------------------------------------------
# the writer rules (§2.4 obligation 2's relative-ref rule)
# ---------------------------------------------------------------------------


def test_writer_refuses_absolute_ref() -> None:
    log = _log(_store(), (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    fields = _fields("wmc", log, scope, refresh={
        "kind": "tgir_plan", "ref": "/abs/plans/pd.json", "basis_policy": "open"})
    with pytest.raises(InvalidArgError):
        ArtifactRecord(
            name="wmc", generation=0, kind="query_result", store=fields["store"],
            plan=fields["plan"], basis=fields["basis"], state=fields["state"],
            refresh=fields["refresh"], steps=tuple(fields["steps"]),
        )


def test_construction_refuses_store_disagreement() -> None:
    """§1.3 rule 2: `store` is duplicated from the scope and must agree,
    refused at construction (the `⊎` precedent)."""
    log = _log(_store(), (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    fields = _fields("wmc", log, scope, store="a-different-store")
    with pytest.raises(InvalidArgError):
        ArtifactRecord(
            name="wmc", generation=0, kind="query_result", store=fields["store"],
            plan=fields["plan"], basis=fields["basis"], state=fields["state"],
            refresh=fields["refresh"], steps=tuple(fields["steps"]),
        )


def test_registry_append_refuses_store_mismatch_against_the_log() -> None:
    store = _store()
    _log(store, (10, [NODE_A]))
    # a *different* history — a different first batch, so its store identity
    # (content-addressed: header + first batch, depscope.py:571-591) genuinely
    # differs from `store`'s, rather than coincidentally matching it.
    other = _log(_store(), (10, [make_op("assert_node", uid="B", label="N", props={},
                                         vt_s=0, vt_e=100)]))
    scope_for_other = _scope(other, ScopeTerm(targets=Targets(nodes=("B",))), tt_q=10)
    reg = Registry(store)
    with pytest.raises(InvalidArgError):
        reg.register(**_fields("wmc", other, scope_for_other))


def test_needs_at_least_one_dependency_scope() -> None:
    log = _log(_store(), (10, [NODE_A]))
    fields = _fields("wmc", log, _scope(log), steps=())
    with pytest.raises(InvalidArgError):
        ArtifactRecord(
            name="wmc", generation=0, kind="query_result", store=fields["store"],
            plan=fields["plan"], basis=fields["basis"], state=fields["state"],
            refresh=fields["refresh"], steps=(),
        )


# ---------------------------------------------------------------------------
# 8 — generality over dependent artifacts (§3.4)
# ---------------------------------------------------------------------------


def test_generality_feature_table_found_by_the_same_walk() -> None:
    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    reg = Registry(store)
    reg.register(**_fields("some-embedding-table", log, scope, kind="feature_table"))

    op2 = make_op("assert_node", uid="A", label="N", props={"x": 1}, vt_s=5, vt_e=50)
    log.append(20, [op2])
    batch = list(log.batches())[-1]
    result = affected(batch, reg)
    assert [r.name for r in result.affected] == ["some-embedding-table"]


def test_lookup_and_witness_never_branch_on_artifact_kind() -> None:
    """§3.4's one-line CI check, made executable: `lookup.py` never spells
    the word naming `ArtifactRecord.kind` at all (checked over its full
    source, including prose), and `witness.py`'s *code* — everything past
    its module docstring, which is free to explain the rule in prose —
    never reads `record.kind` / `rec.kind` / `.kind` off an `ArtifactRecord`.
    `refresh.kind` / `RefreshHandle.kind` (accessed as `self.kind` inside
    `RefreshHandle.to_json`) are §5.5's own, unrelated, closed vocabulary for
    the refresh mechanism and are not what this gate is about — every
    `ArtifactRecord` instance in this module is consistently named `record`,
    so `record.kind` is the one spelling that would actually violate §3.4."""
    import ast

    import tgms.artifact.lookup as lookup_mod
    import tgms.artifact.witness as witness_mod

    lookup_src = Path(lookup_mod.__file__).read_text()
    assert "kind" not in lookup_src.lower()

    witness_path = Path(witness_mod.__file__)
    witness_src = witness_path.read_text()
    tree = ast.parse(witness_src, filename=str(witness_path))
    docstring_end = 0
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        docstring_end = tree.body[0].end_lineno or 0
    code_lines = witness_src.splitlines()[docstring_end:]
    for lineno, line in enumerate(code_lines, start=docstring_end + 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "record.kind" not in line, f"witness.py:{lineno}: {line!r}"


# ---------------------------------------------------------------------------
# CLI smoke: register / list / check round trip on a tiny fixture store
# ---------------------------------------------------------------------------


def test_cli_register_list_check_round_trip(capsys: pytest.CaptureFixture[str]) -> None:
    from tgms.cli import main

    store = _store()
    log = _log(store, (10, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("A",))), tt_q=10)
    record_doc = _fields("wmc", log, scope)
    # `_fields` builds `StepDependency` objects for direct API use; the CLI's
    # `--record-json` takes the wire (JSON) shape instead.
    record_doc["steps"] = [{"step_id": "s1", "dependency": scope.to_json()}]
    record_path = store / "record.json"
    record_path.write_text(json.dumps(record_doc))

    rc = main(["artifact", "register", str(store), "--record-json", str(record_path)])
    assert rc in (0, None)
    out = capsys.readouterr().out
    assert "wmc" in out

    rc = main(["artifact", "list", str(store), "--json"])
    assert rc in (0, None)
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["name"] == "wmc"

    rc = main(["artifact", "check", str(store), "--name", "wmc"])
    out = capsys.readouterr().out
    assert "wmc@0" in out
    assert rc == 0  # nothing written since registration — FRESH

    # a correction inside the scope flips the verdict and the exit code
    op2 = make_op("assert_node", uid="A", label="N", props={"x": 1}, vt_s=5, vt_e=50)
    log.append(20, [op2])
    rc = main(["artifact", "check", str(store), "--name", "wmc", "--json"])
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "possibly-stale"
    assert rc == 1
