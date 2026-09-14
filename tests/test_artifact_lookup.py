"""P1.2 — the lookup pre-filter's `REFUSE` handling (M5 design memo §3;
`docs/design/M5_DESIGN.md`).

`affected()` (`tgms/artifact/lookup.py`) walks every registered record
against a batch's footprints and calls `intersects(term, fp)` for each
(term, footprint) pair. `intersects` is three-valued (D13.23a): an
unrecognized term or footprint form must produce `Match.REFUSE`, never a
silent `False`. Before this fix, `affected()` treated only `Match.HIT` as a
hit, so a `REFUSE` fell through as a miss and a record with an unrecognized
term was reported unaffected — the same class of bug `tgms/tgir/check.py`
guards against by lifting a single `REFUSE` to `UNDECIDABLE` for the whole
scope. These tests pin the fix: a `REFUSE` makes a record survive, a
well-formed non-matching record still does not, and `intersects_calls`
counts the same way it always did for a well-formed population.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import DependencyScope, Incident, ScopeTerm, Targets, store_identity
from tgms.artifact.lookup import affected
from tgms.artifact.record import StepDependency
from tgms.artifact.registry import Registry

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def _log(store: Path, *batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(store / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 10) -> DependencyScope:
    return DependencyScope(store=_identity(log), tt_q=tt_q, terms=terms)


def _fields(name: str, log: EventLog, scope: DependencyScope, **overrides: Any) -> dict[str, Any]:
    """A minimal, valid set of `Registry.register()` keyword fields — the
    `tests/test_artifact_registry.py` pattern."""
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


def _rogue_incident_term() -> ScopeTerm:
    """A `ScopeTerm` that `intersects` cannot parse: an `Incident` with a
    role outside `INCIDENT_ROLES`. The public `Incident(...)` constructor
    refuses an unknown role in `__post_init__`, so — the same way
    `tests/test_freshness_check.py::test_an_unrecognized_incident_role_refuses_rather_than_missing`
    provokes the same `Match.REFUSE` from `incident_match` directly — this
    builds a valid `Incident` first and then mutates the frozen field, the
    way a future wire version's value would arrive after `from_json` (no
    constructor stands between a stored record and its already-parsed
    terms, so this is exactly the shape a real unrecognized-enum record
    would take)."""
    rogue = Incident("either", ("A",))
    object.__setattr__(rogue, "role", "sideways")
    return ScopeTerm(targets=Targets(incident=rogue))


def test_refuse_makes_an_unrecognized_term_a_survivor(tmp_path: Path) -> None:
    """The defect: a record whose scope has an unrecognized term must be
    kept (over-approximated), never silently dropped, even though its term
    can never produce a `Match.HIT` against anything."""
    store = _store(tmp_path)
    log = _log(store, (10, [NODE_A]))
    reg = Registry(store)
    reg.register(**_fields("wmc", log, _scope(log, _rogue_incident_term())))

    edge_op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                      vt_s=0, vt_e=10)
    log.append(20, [edge_op])
    batch = list(log.batches())[-1]

    result = affected(batch, reg)
    assert [r.name for r in result.affected] == ["wmc"]
    assert result.candidate_survivors == 1
    assert result.refused == 1


def test_well_formed_non_matching_record_is_still_not_a_survivor(tmp_path: Path) -> None:
    """The fix must not turn every record into a survivor — a well-formed
    term that genuinely does not intersect the batch is still a miss."""
    store = _store(tmp_path)
    log = _log(store, (10, [NODE_A]))
    reg = Registry(store)
    reg.register(**_fields("no-match", log, _scope(log, ScopeTerm(targets=Targets(nodes=("Z",))))))

    edge_op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                      vt_s=0, vt_e=10)
    log.append(20, [edge_op])
    batch = list(log.batches())[-1]

    result = affected(batch, reg)
    assert result.affected == ()
    assert result.candidate_survivors == 0
    assert result.refused == 0
    # one term against the edge op's two footprints (value arm + D13.21a's
    # widened carve arm — `_assert_edge` always emits both)
    assert result.intersects_calls == 2


def test_calls_are_counted_the_same_way_for_a_well_formed_population(tmp_path: Path) -> None:
    """`intersects_calls` still counts one increment per (term, footprint)
    pair actually evaluated, unchanged by the `REFUSE` fix, for a population
    that never refuses. Two well-formed, non-matching records (one term
    each) against one edge op (two footprints: value arm + carve arm) costs
    exactly four calls, and neither record survives."""
    store = _store(tmp_path)
    log = _log(store, (10, [NODE_A]))
    reg = Registry(store)
    reg.register(**_fields("a", log, _scope(log, ScopeTerm(targets=Targets(nodes=("Z",))))))
    reg.register(**_fields("b", log, _scope(log, ScopeTerm(targets=Targets(nodes=("Y",))))))

    edge_op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                      vt_s=0, vt_e=10)
    log.append(20, [edge_op])
    batch = list(log.batches())[-1]

    result = affected(batch, reg)
    assert result.affected == ()
    assert result.intersects_calls == 4
    assert result.refused == 0
