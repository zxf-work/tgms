"""Step 7b — `UNDECIDABLE("known-overlap")` (failure ledger
`check-assumed-non-overlap-precondition`, pre-registered engine change P-ENG2).

Every footprint and every scope derivation assumes I1: no two believed
versions of one identity overlap in valid time. `ingest_events` supersedes
nothing, so an event whose explicit `disc` names an edge that already exists
sits beside it, and the store then holds exactly the overlap `tgms check
--full` reports as `row/believed-versions-overlap` (soak 1: 13,714 of them).
`check` now replays the log over intervals during its walk and refuses with
`known-overlap` whenever a term could have read such an identity.

This file pins:

1. an overlap-free log yields no known overlap, and every verdict is what it
   was (the campaign-store byte-identity proof is run separately, untimed);
2. one injected overlapping pair: every scope that touches the identity —
   by eid, by endpoint, or `⊤` — is `UNDECIDABLE("known-overlap")`, and every
   scope that does not (another endpoint, another relation type, a far valid
   time, a nodes-only arm, the empty scope) has exactly the verdict it has on
   the same log without the collision;
3. the overlap set is the end-of-log believed state: an assert that covers the
   pair resolves it, one that covers only part of it does not, and a batch the
   applier refuses is rolled back whole;
4. the log-derived set equals the native adapter's own believed-version
   overlaps on random batches (a differential against the real write path),
   and is never smaller than it;
5. the replay runs once per log state and resumes over appended records with
   the same result as a replay from byte 0, refusing if the log changed under
   it; the chain cache gives the same verdicts;
6. the artifact level: on a tiny store with an injected pair, every artifact
   whose lineage touches the identity is UNDECIDABLE and the rest are
   unchanged.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import tgms
import tgms.tgir.check as check_mod
from tgms.core.errors import StateError, TgmsError
from tgms.core.model import OPEN_END, edge_eid
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import (
    REASONS, ChainCache, KnownOverlap, _walk, check, known_overlap,
)
from tgms.tgir.depscope import (
    K_EDGE, K_NODE, TOP_TERM, DependencyScope, Incident, ScopeTerm, Targets,
    store_identity,
)
from tgms.tgir.explain import render

from .conftest import write_op

# ---------------------------------------------------------------------------
# two logs that differ only in whether the second batch collides
# ---------------------------------------------------------------------------

GENESIS = make_op("ingest_events", events=[
    {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10},     # disc "#0"
    {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 20},     # disc "#1"
    {"src": "E", "dst": "F", "rel_type": "CITES", "vt_s": 30},   # disc "#2"
])
#: the collision: an explicit `disc` naming the existing edge A->B (#0) over
#: the same instant — soak 1's shape exactly
COLLIDE = make_op("ingest_events", events=[
    {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10, "disc": "#0"}])
#: the control: the same event under a fresh `disc`, so its own new edge
CONTROL = make_op("ingest_events", events=[
    {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10, "disc": "fresh"}])

EID_AB = edge_eid("A", "B", "MSG", "#0")


def _log(*batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 1000) -> DependencyScope:
    return DependencyScope(store=store_identity(log.header(), log.first_batch()),
                           tt_q=tt_q, terms=terms)


def _collided() -> EventLog:
    return _log((900, [GENESIS]), (2000, [COLLIDE]))


def _control() -> EventLog:
    return _log((900, [GENESIS]), (2000, [CONTROL]))


#: scopes that could have read edge A->B (#0)
TOUCHING = {
    "by-eid": ScopeTerm(kinds=K_EDGE, targets=Targets(edges=(EID_AB,))),
    "by-endpoint": ScopeTerm(kinds=K_EDGE,
                             targets=Targets(incident=Incident("either", ("A",)))),
    "by-dst-and-time": ScopeTerm(kinds=K_EDGE,
                                 targets=Targets(incident=Incident("dst", ("B",))),
                                 rel_types=("MSG",), vt=((0, 50),), props=("weight",)),
    # a kind set excluding `ingest_events` does not exclude a state the read
    # may already have seen (`_touches`)
    "kinds-exclude-ingest": ScopeTerm(kinds=("assert_edge",),
                                      targets=Targets(incident=Incident("src", ("A",)))),
    "top": TOP_TERM,
}
#: scopes that could not have
UNTOUCHED = {
    "other-endpoint": ScopeTerm(kinds=K_EDGE,
                                targets=Targets(incident=Incident("either", ("C",)))),
    "other-rel-type": ScopeTerm(kinds=K_EDGE,
                                targets=Targets(incident=Incident("either", ("A",))),
                                rel_types=("CITES",)),
    "far-valid-time": ScopeTerm(kinds=K_EDGE,
                                targets=Targets(incident=Incident("either", ("A",))),
                                vt=((500, 600),)),
    "nodes-only": ScopeTerm(kinds=K_NODE, targets=Targets(nodes=("A",))),
    "other-eid": ScopeTerm(kinds=K_EDGE,
                           targets=Targets(edges=(edge_eid("C", "D", "MSG", "#1"),))),
}


def _summary(verdict: Any) -> tuple[Any, ...]:
    """Everything a verdict says except the batch ids, which differ between
    the two logs because their second batches differ."""
    return (verdict.state, verdict.reason, verdict.total, verdict.degraded,
            [(w.tt, w.op_seq, w.arm, w.kind, w.vt, w.matched_term, w.matched_on)
             for w in verdict.witnesses])


# ---------------------------------------------------------------------------
# 1. no overlap, no change
# ---------------------------------------------------------------------------

def test_an_overlap_free_log_has_no_known_overlap():
    assert _walk(_control()).overlaps == ()
    assert _walk(_log((900, [GENESIS]))).overlaps == ()


def test_the_reason_is_declared_and_rendered():
    assert "known-overlap" in REASONS
    log = _collided()
    verdict = check(_scope(log, TOUCHING["by-eid"]), log)
    assert verdict.to_json() == {"verdict": "undecidable", "reason": "known-overlap"}
    assert not verdict.actionable_fresh
    assert "overlapping versions" in render(verdict)


# ---------------------------------------------------------------------------
# 2. one injected pair
# ---------------------------------------------------------------------------

def test_the_walk_reports_exactly_the_colliding_identity():
    overlaps = _walk(_collided()).overlaps
    assert len(overlaps) == 1
    (k,) = overlaps
    assert isinstance(k, KnownOverlap)
    assert k.to_json() == {
        "entity_kind": "edge",
        "identity": {"eid": EID_AB, "src": "A", "dst": "B", "rel_type": "MSG",
                     "disc": "#0"},
        "intervals": [[10, 11], [10, 11]],
    }


@pytest.mark.parametrize("name", sorted(TOUCHING))
@pytest.mark.parametrize("tt_q", [1000, 2500])
def test_every_scope_touching_the_identity_is_undecidable(name: str, tt_q: int):
    """Before the colliding batch (`tt_q=1000`, the read saw one version and a
    correction followed) and after it (`tt_q=2500`, the read saw both and the
    suffix is empty, so the old verdict was `FRESH`) alike."""
    log = _collided()
    verdict = check(_scope(log, TOUCHING[name], tt_q=tt_q), log)
    assert verdict.state == "undecidable"
    assert verdict.reason == "known-overlap"


@pytest.mark.parametrize("name", sorted(UNTOUCHED))
@pytest.mark.parametrize("tt_q", [1000, 2500])
def test_every_other_scope_is_unchanged(name: str, tt_q: int):
    collided, control = _collided(), _control()
    got = check(_scope(collided, UNTOUCHED[name], tt_q=tt_q), collided)
    want = check(_scope(control, UNTOUCHED[name], tt_q=tt_q), control)
    assert got.reason != "known-overlap"
    assert _summary(got) == _summary(want)


def test_the_empty_scope_stays_fresh_over_an_overlap():
    log = _collided()
    scope = DependencyScope.empty(store_identity(log.header(), log.first_batch()), 1000)
    assert check(scope, log).actionable_fresh


def test_a_mixed_scope_is_undecidable_if_any_term_touches():
    log = _collided()
    scope = _scope(log, UNTOUCHED["other-endpoint"], TOUCHING["by-endpoint"])
    assert check(scope, log).reason == "known-overlap"


def test_integrity_refusals_still_take_precedence():
    """Step 7b runs after steps 1-6: a scope for another store is still a
    `store-mismatch`, not a `known-overlap`."""
    log = _collided()
    scope = DependencyScope(store="someone-else", tt_q=1000, terms=(TOP_TERM,))
    assert check(scope, log).reason == "store-mismatch"


# ---------------------------------------------------------------------------
# 3. what "believed now" means
# ---------------------------------------------------------------------------

def _edge_ab(vt_s: int, vt_e: int) -> dict[str, Any]:
    return make_op("assert_edge", src="A", dst="B", rel_type="MSG", disc="#0",
                   props={}, vt_s=vt_s, vt_e=vt_e)


def test_an_assert_covering_the_pair_resolves_it():
    log = _log((900, [GENESIS]), (2000, [COLLIDE]), (3000, [_edge_ab(0, 100)]))
    assert _walk(log).overlaps == ()
    verdict = check(_scope(log, TOUCHING["by-eid"]), log)
    assert verdict.state == "possibly-stale"


def test_an_assert_covering_part_of_the_pair_leaves_its_remainders_overlapping():
    wide = make_op("ingest_events", events=[
        {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 5, "vt_e": 15, "disc": "#0"}])
    # believed: [10, 11) and [5, 15); the assert over [12, 20) cuts the second
    # to [5, 12), which still overlaps [10, 11)
    log = _log((900, [GENESIS]), (2000, [wide]), (3000, [_edge_ab(12, 20)]))
    (k,) = _walk(log).overlaps
    assert k.intervals == ((5, 12), (10, 11), (12, 20))


def test_a_refused_batch_is_rolled_back_whole():
    """The colliding event shares its batch with a `correct` of an edge that
    does not exist; the applier refuses the batch, so neither op landed."""
    missing = make_op("correct", ref={"kind": "edge", "src": "X", "dst": "Y",
                                      "rel_type": "MSG", "disc": ""},
                      props={"w": 1}, vt_s=0, vt_e=5)
    log = _log((900, [GENESIS]), (2000, [COLLIDE, missing]))
    assert _walk(log).overlaps == ()


def test_an_explicit_node_colliding_with_a_believed_one_refuses_its_batch():
    nodes = make_op("ingest_events", events=[
        {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10, "disc": "#0"}],
        nodes=[{"uid": "A", "label": "N", "vt_s": 0}])
    log = _log((900, [GENESIS]), (2000, [nodes]))
    assert _walk(log).overlaps == ()


# ---------------------------------------------------------------------------
# 4. differential against the real write path
# ---------------------------------------------------------------------------

DISCS = ["", "#0", "#1", "x"]


@st.composite
def _ingest_op(draw) -> dict[str, Any]:
    events = []
    for _ in range(draw(st.integers(1, 3))):
        # a narrow alphabet and mostly-explicit `disc`, so collisions with
        # each other and with `write_op`'s (`disc: ""`) edges are common
        ev: dict[str, Any] = {"src": draw(st.sampled_from(["a", "b", "c"])),
                              "dst": draw(st.sampled_from(["a", "b", "c"])),
                              "rel_type": draw(st.sampled_from(["R", "S"])),
                              "vt_s": draw(st.integers(0, 20))}
        if draw(st.booleans()):
            ev["vt_e"] = ev["vt_s"] + draw(st.integers(1, 10))
        if draw(st.integers(0, 3)) > 0:
            ev["disc"] = draw(st.sampled_from(DISCS))
        events.append(ev)
    op: dict[str, Any] = {"op": "ingest_events", "events": events,
                          "offset": draw(st.integers(0, 2))}
    if draw(st.integers(0, 4)) == 0:
        op["nodes"] = [{"uid": draw(st.sampled_from(["a", "b", "e", "f"])), "label": "N",
                        "vt_s": draw(st.integers(0, 20))}]
    return op


_batches = st.lists(st.lists(st.one_of(write_op(), _ingest_op(), _ingest_op()),
                             min_size=1, max_size=3), min_size=1, max_size=8)


def _native_adapter() -> Any:
    """The campaign backend, whatever `TGMS_TEST_BACKEND` says: DuckDB refuses
    a batch that writes two versions of one identity at one `vt_s` with a
    `duckdb.ConstraintException` on the duplicate version id (not a
    `TgmsError`, so the store does not roll it back), while the native engine
    applies it and `tgms check --full` then reports the overlap — the shape
    this step exists for, so the differential runs where it is reachable."""
    from tgms.storage.native import NativeAdapter

    tmp = tempfile.TemporaryDirectory(prefix="tgms-test-overlap-")
    adapter = NativeAdapter(tmp.name)
    adapter._test_tmpdir = tmp
    adapter.paranoid = False
    return adapter


def _vids_alias(adapter: Any) -> bool:
    """Two rows sharing a version id (`hash(identity, tt_s, vt_s)`: one batch
    writing one identity twice at one `vt_s`). Closing or retiring by that id
    then stops believing both, so the adapter can believe *less* than the
    interval replay — never more."""
    vids = [v.vid for v in adapter.all_edge_versions()]
    vids += [v.vid for v in adapter.all_node_versions()]
    return len(vids) != len(set(vids))


def _adapter_overlaps(adapter: Any) -> set[tuple[str, str]]:
    """`integrity.rs::overlaps`' rule over what the adapter believes now."""
    groups: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for v in adapter.all_edge_versions():
        if v.tt_e == OPEN_END:
            groups[("edge", v.eid)].append((v.vt_s, v.vt_e))
    for v in adapter.all_node_versions():
        if v.tt_e == OPEN_END:
            groups[("node", v.uid)].append((v.vt_s, v.vt_e))
    out = set()
    for key, ivs in groups.items():
        ivs.sort()
        if any(a[1] > b[0] for a, b in zip(ivs, ivs[1:])):
            out.add(key)
    return out


def _walk_overlaps(log: EventLog) -> set[tuple[str, str]]:
    out = set()
    for k in _walk(log).overlaps:
        ident = k.footprint.identity.to_json()
        out.add(("edge", ident["eid"]) if k.footprint.entity_kind == "edge"
                else ("node", ident["uid"]))
    return out


@settings(max_examples=150, deadline=None)
@given(_batches)
def test_the_log_derived_overlaps_equal_the_adapters(batches):
    adapter = _native_adapter()
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for i, ops in enumerate(batches):
        tt = 100 + i
        log.append(tt, ops)                     # write-ahead, as `Store._write`
        adapter.begin()
        try:
            adapter.apply_ops(ops, tt)
        except TgmsError:
            adapter.rollback()
            continue
        adapter.commit()
    derived, actual = _walk_overlaps(log), _adapter_overlaps(adapter)
    # never smaller: every overlap the store holds is known to `check`
    assert actual <= derived
    if not _vids_alias(adapter):
        assert derived == actual


def test_the_differential_reaches_overlaps():
    """Guard against a vacuous differential: the strategy's own shape does
    produce the collision this file is about."""
    adapter = _native_adapter()
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    ops = [{"op": "ingest_events", "events": [
        {"src": "a", "dst": "b", "rel_type": "R", "vt_s": 3, "disc": "x"},
        {"src": "a", "dst": "b", "rel_type": "R", "vt_s": 3, "disc": "x"}]}]
    log.append(100, ops)
    adapter.begin()
    adapter.apply_ops(ops, 100)
    adapter.commit()
    assert _walk_overlaps(log) == _adapter_overlaps(adapter) == {
        ("edge", edge_eid("a", "b", "R", "x"))}


# ---------------------------------------------------------------------------
# 5. once per log state, and the cache changes nothing
# ---------------------------------------------------------------------------

def _from_scratch(log: EventLog) -> tuple[Any, ...]:
    believed = check_mod._Believed()
    for batch, _end, _raw in log.batches_from(0):
        believed.apply_batch(batch)
    return tuple(k.to_json() for k in believed.overlaps())


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(_batches)
def test_resuming_over_appended_records_equals_replaying_from_byte_0(batches):
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for i, ops in enumerate(batches):
        log.append(100 + i, ops)
        resumed = tuple(k.to_json() for k in _walk(log).overlaps)
        assert resumed == _from_scratch(log)


def test_the_replay_runs_once_per_log_state(monkeypatch):
    log = _collided()
    check_mod._REPLAYS.clear()
    applied: list[int] = []
    real = check_mod._Believed.apply_batch
    monkeypatch.setattr(check_mod._Believed, "apply_batch",
                        lambda self, b: applied.append(int(b["tt"])) or real(self, b))
    for _ in range(3):
        _walk(log)
    assert applied == [900, 2000]                    # once, from byte 0
    log.append(3000, [_edge_ab(0, 100)])
    assert _walk(log).overlaps == ()
    _walk(log)
    assert applied == [900, 2000, 3000]              # only the appended record


def test_a_log_rewritten_between_the_walk_and_the_replay_refuses():
    log = _collided()
    walked = _walk(log)
    end = max(walked.chains)
    raw = log.path.read_bytes()
    old = b'"disc":"#0","dst":"B","rel_type":"MSG","src":"A","vt_s":10}'
    assert raw.count(old) == 1
    log.path.write_bytes(raw.replace(old, old.replace(b":10}", b":11}")))
    check_mod._REPLAYS.clear()
    with pytest.raises(StateError):
        check_mod._overlaps_at(log, walked.chains, end)


def test_the_chain_cache_agrees():
    log = _collided()
    scopes = [_scope(log, t) for t in (*TOUCHING.values(), *UNTOUCHED.values())]
    plain = [check(s, log).to_json() for s in scopes]
    cache = ChainCache()
    assert [check(s, log, chain_cache=cache).to_json() for s in scopes] == plain
    assert [check(s, log, chain_cache=cache).to_json() for s in scopes] == plain
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# 6. the artifact level
# ---------------------------------------------------------------------------

BACKEND = "native"


def _fixture_events() -> list[dict[str, Any]]:
    rng = random.Random(1)
    uids = [f"n{i}" for i in range(24)]
    events = []
    for i in range(240):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst,
                       "rel_type": rng.choice(["FOLLOWS", "MESSAGES", "CITES"]),
                       "vt_s": i * 5})
    return events


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("known-overlap-fixture")
    store = tgms.open(base, backend=BACKEND)
    store.ingest_events(_fixture_events(), node_label="Person")
    store.close()
    return base


def _verdicts_after(pristine: Path, disc: str, victim: int) -> tuple[dict, dict, Any]:
    from tgms.artifact.witness import check_artifact
    from tgms.eval.storm import Storm

    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    storm = Storm(work, n_artifacts=40, seed=4, backend=BACKEND)
    try:
        ev = dict(_fixture_events()[victim])
        ev["disc"] = disc
        storm.store.ingest_events([ev], node_label="Person")
        records = {n: storm.registry.current(n) for n in storm.registry.names()}
        verdicts = {n: check_artifact(r, storm.store.eventlog, level1=False)
                    for n, r in records.items()}
        return records, verdicts, _walk(storm.store.eventlog).overlaps
    finally:
        storm.close()


def _strip_batch_ids(obj: Any) -> Any:
    """A verdict minus what necessarily differs between the two runs: the
    injected batch's id and wall-clock `tt`, and its event's identity (the
    colliding `disc` against the control's fresh one)."""
    if isinstance(obj, dict):
        return {k: _strip_batch_ids(v) for k, v in obj.items()
                if k not in ("batch_id", "tt", "identity")}
    if isinstance(obj, list):
        return [_strip_batch_ids(v) for v in obj]
    return obj


def test_artifacts_touching_an_injected_overlap_are_undecidable_and_the_rest_unchanged(
    pristine_store: Path,
) -> None:
    victim = 7
    v = _fixture_events()[victim]
    records, got, overlaps = _verdicts_after(pristine_store, f"#{victim}", victim)
    _records_c, want, overlaps_c = _verdicts_after(pristine_store, "ctl", victim)
    assert overlaps_c == ()
    (k,) = overlaps
    assert k.footprint.identity.to_json()["eid"] == edge_eid(
        v["src"], v["dst"], v["rel_type"], f"#{victim}")

    touched, untouched = set(), set()
    for name, record in records.items():
        # the scopes `check_trace` checks: per step, else the merged basis
        doc = record.to_json()
        scopes = [DependencyScope.from_json(s["dependency"])
                  for s in doc.get("steps") or () if isinstance(s.get("dependency"), dict)]
        if not scopes and isinstance(doc.get("dependency"), dict):
            scopes = [DependencyScope.from_json(doc["dependency"])]
        hit = any(known_overlap(s, overlaps) for s in scopes)
        (touched if hit else untouched).add(name)
    assert touched and untouched          # the fixture exercises both sides

    for name in touched:
        assert "known-overlap" in got[name].steps.reasons, name
        assert not got[name].actionable_fresh
    for name in untouched:
        assert "known-overlap" not in got[name].steps.reasons, name
        assert _strip_batch_ids(got[name].to_json()) == _strip_batch_ids(want[name].to_json())
