"""M4.2 — `intersects`, `check`, the witness (D13.23–D13.27).

The gate this file discharges (M4_IMPLEMENTATION_PLAN §6, M4.2 row):

(a) **§13.6's worked example reproduced with its exact witness list** — the
    three-step plan on `S₀`, the two-op batch, four footprints, the eight-row
    intersection table, and two witnesses after dedup;
(b) **all six `UNDECIDABLE` reasons reachable** by a test;
(c) **∅-scope ⇒ `FRESH`**;
(d) **a pinned scope still scans** — FF-4's cell, and the one shortcut D13.24
    forbids by name;
(e) the **`matched_on` non-vacuity rule**: a conjunct that passed because
    either side was `"*"` is not attribution, it is absence of narrowing.

What is *not* here: the twenty-one soundness scenarios (CE-1..6, FF-1..9,
RG-1). Those are `tests/test_freshness_soundness.py`, authored independently
from the frozen documents, and duplicating them here would defeat the point of
their being written by someone else.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tgms.core.model import OPEN_END, edge_eid
from tgms.storage.base import make_op
from tgms.storage.eventlog import SEED_CHAIN, EventLog
from tgms.tgir.check import (
    REASONS, WITNESS_CAP, ChainCache, Match, check, check_steps, incident_match,
    intersects, meets, vt_overlaps,
)
from tgms.tgir.depscope import (
    K_DENSE_ID, K_EDGE, K_NODE, TOP, Checkpoint, DependencyScope, EdgeKey,
    Incident, ScopeTerm, Targets, store_identity,
)
from tgms.tgir.explain import render, render_identity, render_steps
from tgms.tgir.footprint import footprints_of_op

# ---------------------------------------------------------------------------
# a log to check against
# ---------------------------------------------------------------------------


def _log(*batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    """A real event log with `batches` appended, and nothing else."""
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _scope(log: EventLog, *terms: ScopeTerm, tt_q: int = 1000,
           **kw: Any) -> DependencyScope:
    """A scope naming this log's store, with a full-scan checkpoint so the
    integrity steps pass and the interesting part is the scan."""
    return DependencyScope(store=_identity(log), tt_q=tt_q, terms=terms, **kw)


NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)
#: a second, unrelated batch, so a log can have a checkpointed prefix that is
#: neither the genesis record (which is the store identity) nor the suffix
MID = make_op("assert_node", uid="M", label="N", props={"tier": "silv"},
              vt_s=0, vt_e=100)


# ---------------------------------------------------------------------------
# (a) §13.6's worked example
# ---------------------------------------------------------------------------

#: The four terms of §13.6, transcribed. `s1`'s three come from
#: `entity_history(uid="A", include_edges=true)`; `s2`'s one from the
#: `aggregate_events` call; `s3` is `compute`, whose scope is ∅.
T1a = ScopeTerm(kinds=K_NODE, targets=Targets(nodes=("A",)))
T1b = ScopeTerm(kinds=K_EDGE, targets=Targets(incident=Incident("either", ("A",))))
T1c = ScopeTerm(kinds=K_DENSE_ID, targets=Targets(incident=Incident("either", ("A",))),
                props=("@identity",))
#: `Pᵥ` — real keys ∪ {@identity, @extent, @event_key}. **Not** ⊤:
#: `aggregate_events` is not carve-reachable (D13.7a), which is what lets
#: `vt: [[0,100]]` still mean something.
PV = ("weight", "@identity", "@extent", "@event_key")
T2 = ScopeTerm(kinds=K_EDGE, targets=Targets(incident=Incident("src", ("X", "B", "C"))),
               rel_types=("MSG",), vt=((0, 100),), vt_mode="event", props=PV)

#: §13.6's correction batch: one of each of the two effect classes a row-touch
#: rule handles worst.
OP0 = make_op("assert_node", uid="A", label="Node", props={"tier": "gold"},
              vt_s=0, vt_e=OPEN_END)
OP1 = make_op("ingest_events", events=[
    {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
    {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 46},
])


def _worked_example_log() -> EventLog:
    return _log((900, [NODE_A]), (2000, [OP0, OP1]))


#: The eids of §13.6's two ingested events. Both take the **default**
#: discriminator `f"#{offset + i}"`, so they are `#0` and `#1`.
_E136 = sorted({edge_eid("C", "D", "MSG", "#0"), edge_eid("C", "D", "MSG", "#1")})


def test_the_worked_example_produces_exactly_two_witnesses():
    """§13.6's verdict block, byte for byte on every field the document spells
    out. `batch_id` and `tt` are elided there (`"9f1c…"`, `…`) and are checked
    structurally instead.

    **One field is deliberately not §13.6's, and it is the erratum's doing.**
    The `ingest_events` edge arm's `identity` now carries an `eid` set, because
    **E-1 amends D13.22** to emit one — the fix for the false negative an
    explicit-`disc` event would otherwise cause. §13.6 was written before E-1
    and shows `{"src":["C"],"dst":["D"],"rel_type":["MSG"]}`; a witness
    rendering that identity necessarily gains the key. The errata register's
    own rule covers this — *"a reader of that definition is expected to read
    this register too"* — but the delta is spelled out here rather than
    absorbed, because §13.6's witness list is a named M4.2 gate and a silent
    divergence from it would be exactly the kind of drift the gate exists to
    catch. **Every other field matches §13.6 exactly.**
    """
    log = _worked_example_log()
    scope = _scope(log, T1a, T1b, T1c, T2, tt_q=1000)
    verdict = check(scope, log, term_steps=["s1", "s1", "s1", "s2"])

    assert verdict.state == "possibly-stale"
    assert not verdict.actionable_fresh
    assert verdict.total == 2

    got = [w.to_json() for w in verdict.witnesses]
    for w in got:
        assert len(w.pop("batch_id")) == 16
        assert w.pop("tt") == 2000

    assert got == [
        {"op_seq": 0, "arm": "value", "class": "A|B", "kind": "assert_node",
         "identity": {"uid": "A"}, "vt": [0, 4611686018427387904],
         "matched_term": 0, "matched_on": ["kinds", "targets.nodes"],
         "step_id": "s1"},
        {"op_seq": 1, "arm": "value", "class": "A", "kind": "ingest_events",
         "identity": {"eid": _E136, "src": ["C"], "dst": ["D"],
                      "rel_type": ["MSG"]},
         "vt": [45, 48],
         "matched_term": 3,
         "matched_on": ["kinds", "targets.incident", "rel_types", "vt"],
         "step_id": "s2"},
    ]


def test_the_worked_example_matches_13_6_verbatim_once_E1s_field_is_removed():
    """The other half of the statement above: strip the one field E-1 added and
    what remains is §13.6's verdict block character for character."""
    log = _worked_example_log()
    verdict = check(_scope(log, T1a, T1b, T1c, T2, tt_q=1000), log,
                    term_steps=["s1", "s1", "s1", "s2"])
    got = []
    for w in verdict.witnesses:
        obj = w.to_json()
        obj.pop("batch_id")
        obj.pop("tt")
        obj["identity"].pop("eid", None)
        got.append(obj)
    assert got == [
        {"op_seq": 0, "arm": "value", "class": "A|B", "kind": "assert_node",
         "identity": {"uid": "A"}, "vt": [0, 4611686018427387904],
         "matched_term": 0, "matched_on": ["kinds", "targets.nodes"],
         "step_id": "s1"},
        {"op_seq": 1, "arm": "value", "class": "A", "kind": "ingest_events",
         "identity": {"src": ["C"], "dst": ["D"], "rel_type": ["MSG"]},
         "vt": [45, 48], "matched_term": 3,
         "matched_on": ["kinds", "targets.incident", "rel_types", "vt"],
         "step_id": "s2"},
    ]


def test_the_worked_examples_eight_row_intersection_table():
    """Every row of §13.6's table, asserted individually — because the verdict
    above would come out right even if two rows were wrong in compensating
    directions."""
    fp0v, fp0c = footprints_of_op(OP0, 0)
    fp1e, fp1n = footprints_of_op(OP1, 1)
    hit = lambda t, f: intersects(t, f)[0] is Match.HIT  # noqa: E731

    assert hit(T1a, fp0v)                       # match
    assert not hit(T1b, fp0v) and not hit(T1c, fp0v)   # node fp, incident-only terms
    assert not hit(T2, fp0v)                    # same reason
    assert hit(T1a, fp0c)                       # props "*" meets {@recut,@version}
    assert not hit(T2, fp0c)                    # node/edge routing AND Pᵥ
    assert hit(T2, fp1e)                        # the set-vs-set incidence test
    assert not hit(T1b, fp1e)                   # endpoints {C,D} do not include A
    assert not hit(T1a, fp1n)                   # "C","D" ∉ ["A"]
    assert not hit(T1c, fp1n)                   # T1c has no `nodes` arm


def test_the_carve_arm_is_refused_by_the_aggregate_terms_props():
    """Two rows of that table are the FF-1 fix working as designed: `fp0c`
    reaches `s1`'s broad-`P` term and is correctly refused by `s2`'s `Pᵥ` term.
    The carve arm costs precision exactly where an operator exposes version
    metadata, and nowhere else."""
    _fp0v, fp0c = footprints_of_op(OP0, 0)
    assert set(fp0c.props) == {"@recut", "@version"}
    assert not meets(PV, fp0c.props)
    assert T2.carve_reachable is False
    assert T1a.carve_reachable is True


def test_the_redundant_carve_match_is_deduplicated_per_step_and_op():
    """`fp0c` matches `T1a` too, and adds nothing a reader can act on once
    `fp0v` has already fired for the same op."""
    log = _worked_example_log()
    verdict = check(_scope(log, T1a), log, step_id="s1")
    assert verdict.total == 1
    assert verdict.witnesses[0].arm == "value"


def test_the_variant_keeps_op0_and_is_a_true_false_invalidation():
    """§13.6's second test case: drop `op1`. Still `POSSIBLY_STALE` (one
    witness, `s1`), but the *answer* is unchanged — `s1.edges` is untouched, so
    `s2` and `s3` are untouched. A false invalidation: permitted by D1.13,
    counted against precision by D6.2, and the cheapest available
    demonstration that the two metrics measure different things."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    steps = check_steps([("s1", _scope(log, T1a, T1b, T1c)),
                         ("s2", _scope(log, T2)),
                         ("s3", DependencyScope.empty(_identity(log), 1000))], log)
    assert not steps.actionable_fresh
    per = dict(steps.per_step)
    assert per["s1"].state == "possibly-stale"
    assert per["s2"].actionable_fresh          # stale at a step, fresh at the answer
    assert per["s3"].actionable_fresh          # ∅ is fresh forever
    assert [w.step_id for w in steps.witnesses] == ["s1"]


# ---------------------------------------------------------------------------
# (e) the matched_on non-vacuity rule
# ---------------------------------------------------------------------------

def test_matched_on_omits_a_conjunct_that_passed_on_a_star():
    """The rule the whole precision story runs on, and the one an implementer
    treats as decoration. A conjunct that passed because either side was `"*"`
    is not attribution — it is absence of narrowing."""
    fp = footprints_of_op(NODE_A, 0)[0]
    all_star = ScopeTerm()
    m, on = intersects(all_star, fp)
    assert m is Match.HIT
    assert on == ()          # nothing narrowed, so nothing is attributed


def test_matched_on_names_only_the_conjuncts_that_were_concrete_on_both_sides():
    fp = footprints_of_op(NODE_A, 0)[0]
    term = ScopeTerm(kinds=("assert_node",), targets=Targets(nodes=("A",)),
                     vt=((0, 5),))
    m, on = intersects(term, fp)
    assert m is Match.HIT
    assert on == ("kinds", "targets.nodes", "vt")
    assert "props" not in on         # the term's props are "*"
    assert "rel_types" not in on     # a node footprint never consults it


def test_matched_on_is_reported_in_the_canonical_conjunct_order():
    """Evaluation is cheap-first; reporting is §13.6's order. They are
    different things and the second is what a witness carries."""
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    term = ScopeTerm(kinds=("assert_edge",),
                     targets=Targets(incident=Incident("src", ("A",))),
                     rel_types=("MSG",), vt=((0, 5),), props=("@extent",))
    m, on = intersects(term, fp)
    assert m is Match.HIT
    # props: the footprint's side is "*", so it is vacuous and omitted
    assert on == ("kinds", "targets.incident", "rel_types", "vt")


def test_an_incident_arm_with_star_uids_is_vacuous_too():
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    term = ScopeTerm(targets=Targets(incident=Incident("either", TOP)))
    m, on = intersects(term, fp)
    assert m is Match.HIT
    assert on == ()


# ---------------------------------------------------------------------------
# D13.23's four named properties
# ---------------------------------------------------------------------------

def test_rel_types_is_consulted_only_for_edge_footprints():
    """The guard that keeps `aggregate_events(rel_types=["MSG"])` from being
    invalidated by every unrelated node assert. Testing a node write against a
    rel-type-restricted scope would either match everything (a precision
    disaster) or nothing (**unsound**)."""
    node_fp = footprints_of_op(NODE_A, 0)[0]
    term = ScopeTerm(rel_types=("MSG",), targets=Targets(nodes=("A",)))
    assert intersects(term, node_fp)[0] is Match.HIT

    edge_fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="LIKE", props={},
                vt_s=0, vt_e=10), 0)[0]
    edge_term = ScopeTerm(rel_types=("MSG",),
                          targets=Targets(incident=Incident("src", ("A",))))
    assert intersects(edge_term, edge_fp)[0] is Match.MISS


def test_an_absent_target_arm_is_empty_not_top():
    """A scope with only a `nodes` arm never matches an edge write, and vice
    versa. This is why `None` and `TOP` are different values in `Targets`."""
    node_fp = footprints_of_op(NODE_A, 0)[0]
    edge_fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    nodes_only = ScopeTerm(targets=Targets(nodes=("A",)))
    edges_only = ScopeTerm(targets=Targets(incident=Incident("src", ("A",))))
    assert intersects(nodes_only, node_fp)[0] is Match.HIT
    assert intersects(nodes_only, edge_fp)[0] is Match.MISS
    assert intersects(edges_only, edge_fp)[0] is Match.HIT
    assert intersects(edges_only, node_fp)[0] is Match.MISS


def test_the_three_target_arms_are_a_disjunction():
    """An edge footprint matches on identity *or* endpoint."""
    op = make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                 vt_s=0, vt_e=10)
    fp = footprints_of_op(op, 0)[0]
    eid = fp.identity.eid[0]
    by_eid = ScopeTerm(targets=Targets(edges=(eid,)))
    by_endpoint = ScopeTerm(targets=Targets(incident=Incident("dst", ("B",))))
    neither = ScopeTerm(targets=Targets(edges=("nope",),
                                        incident=Incident("src", ("Z",))))
    assert intersects(by_eid, fp)[0] is Match.HIT
    assert intersects(by_endpoint, fp)[0] is Match.HIT
    assert intersects(neither, fp)[0] is Match.MISS


def test_vt_overlaps_is_plain_half_open_with_no_adjustment():
    """D13.21 already did the adjustment, once, on the footprint side. Doing it
    again here would double-count it."""
    assert vt_overlaps(((0, 10),), ((9, 20),))
    assert not vt_overlaps(((0, 10),), ((10, 20),))
    assert vt_overlaps(TOP, ((10, 20),))
    assert vt_overlaps(((0, 10),), TOP)


def test_each_arm_is_matched_separately_never_a_merged_pseudo_footprint():
    """A term that matches *either* arm fires. Merging them would either lose
    the carve arm's reach or lose the value arm's precision."""
    value, carve = footprints_of_op(NODE_A, 0)
    # a term the value arm misses on vt but the carve arm reaches
    term = ScopeTerm(targets=Targets(nodes=("A",)), vt=((500, 600),),
                     props=("@version",))
    assert intersects(term, value)[0] is Match.MISS
    assert intersects(term, carve)[0] is Match.HIT


def test_meets_is_set_versus_set_never_scalar_membership():
    """CO-8: `["C"] ∈ ["X","B","C"]` returns false under a scalar-typed test,
    and false here is a false negative."""
    assert meets(("X", "B", "C"), ("C",))
    assert meets(("C",), ("X", "B", "C"))
    assert not meets(("X",), ("C",))
    assert meets(TOP, ("C",)) and meets(("C",), TOP)


# ---------------------------------------------------------------------------
# D13.23a — totality. An unrecognized value is never a non-match.
# ---------------------------------------------------------------------------

def test_an_unrecognized_incident_role_refuses_rather_than_missing():
    """FF-8 found the one place where a *narrowing* was reachable by accident.
    `role: "both"` was a real scan mode with no encoding; an implementer
    encoding it faithfully produced a term that matched nothing."""
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    rogue = Incident("either", ("A",))
    object.__setattr__(rogue, "role", "sideways")   # a future version's wire value
    assert incident_match(rogue, fp) is Match.REFUSE


def test_role_both_is_a_real_encoding_with_a_conjunctive_test():
    """The genuinely narrower test `both` deserves — FF-8's fix, not a
    widening."""
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    assert incident_match(Incident("both", ("A", "B")), fp) is Match.HIT
    assert incident_match(Incident("both", ("A",)), fp) is Match.MISS
    assert incident_match(Incident("either", ("A",)), fp) is Match.HIT


# ---------------------------------------------------------------------------
# (b) all six UNDECIDABLE reasons
# ---------------------------------------------------------------------------

def test_reason_scope_version():
    log = _log((900, [NODE_A]))
    scope = replace(_scope(log), version=2)
    assert check(scope, log).reason == "scope-version"


def test_reason_unknown_enum():
    """Reachable only for a scope deserialized from a *future* version, which
    the version gate catches first — so it is provoked here the way the wire
    would. Implemented anyway, because "unreachable" is what FF-8 was."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    term = ScopeTerm(kinds=("assert_node",), targets=Targets(nodes=("A",)))
    object.__setattr__(term, "vt_mode", "sideways")
    assert check(_scope(log, term), log).reason == "unknown-enum"


def test_reason_unknown_enum_is_reached_at_match_time_too():
    """A `REFUSE` from `intersects` lifts to `UNDECIDABLE` for the whole
    scope — never to a `MISS`."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    rogue = Incident("either", ("A",))
    term = ScopeTerm(kinds=K_NODE, targets=Targets(incident=rogue))
    object.__setattr__(rogue, "role", "sideways")
    assert check(_scope(log, term), log).reason == "unknown-enum"


def test_reason_store_mismatch():
    log = _log((900, [NODE_A]))
    assert check(replace(_scope(log), store="some-other-store"), log).reason \
        == "store-mismatch"


def test_reason_store_mismatch_covers_unanchored():
    """An adapter-only read has no log behind it, so there is nothing this log
    could be the continuation of. It is what excludes the whole oracle test
    family from M4's population."""
    log = _log((900, [NODE_A]))
    assert check(replace(_scope(log), store="unanchored"), log).reason \
        == "store-mismatch"


def test_reason_no_tt_q():
    """Mandatory on the dataclass, so this is only reachable from the wire —
    which is exactly where a scope written by an older producer arrives."""
    log = _log((900, [NODE_A]))
    wire = _scope(log).to_json()
    del wire["tt_q"]
    assert check(wire, log).reason == "no-tt_q"


def test_reason_log_rewritten():
    """D13.18's tamper-evidence. The scope's checkpoint chain no longer matches
    the log's."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    scope = _scope(log, T1a, checkpoints=(Checkpoint(0, "deadbeefdeadbeef"),))
    assert check(scope, log).reason == "log-rewritten"


def test_reason_log_unreadable():
    """`chain_of_prefix` raises rather than returning a mismatch when the offset
    is not a record boundary — a third outcome, and it refuses like the other
    two."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    mid = log.size() // 2
    scope = _scope(log, T1a, checkpoints=(Checkpoint(mid, "deadbeefdeadbeef"),))
    assert check(scope, log).reason == "log-unreadable"


def test_a_record_the_footprint_builder_cannot_read_refuses():
    """A record whose op kind this checker does not understand is a record it
    cannot read. Refusing is the only sound answer, and it is never `FRESH`."""
    log = _log((900, [NODE_A]), (2000, [{"op": "teleport", "uid": "A"}]))
    assert check(_scope(log, T1a), log).reason == "log-unreadable"


def test_every_declared_reason_is_reachable():
    """The list is closed at six. A seventh would mean the frozen algorithm
    grew a step, which is an escalation rather than a commit."""
    assert set(REASONS) == {"scope-version", "unknown-enum", "store-mismatch",
                            "no-tt_q", "log-rewritten", "log-unreadable"}


# ---------------------------------------------------------------------------
# (c) the empty scope, and (d) the pinned scope
# ---------------------------------------------------------------------------

def test_the_empty_scope_is_fresh_forever():
    """D5.3 — `terms: []` is the correct, non-degenerate value for a `compute`
    node over literal inputs, never a defect."""
    log = _log((900, [NODE_A]), (2000, [OP0, OP1]))
    verdict = check(DependencyScope.empty(_identity(log), 1000), log)
    assert verdict.actionable_fresh


def test_the_empty_scope_still_refuses_over_a_rewritten_log():
    """The integrity steps run before step 7. A ∅ scope whose log was rewritten
    has no basis to be confident about."""
    log = _log((900, [NODE_A]))
    scope = DependencyScope.empty(_identity(log), 1000,
                                  checkpoints=(Checkpoint(0, "deadbeefdeadbeef"),))
    assert check(scope, log).reason == "log-rewritten"


def test_a_pinned_scope_still_scans_the_suffix():
    """FF-4, and the shortcut D13.24 forbids by name. D1.10 clamps an
    above-frontier `as_of_tt` down to the frontier, so a genuinely pinned scope
    scans a suffix that is empty *because the log says so*, not because a flag
    said to skip the scan."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    pinned = _scope(log, T1a, tt_q=1000, pinned=True)
    unpinned = _scope(log, T1a, tt_q=1000, pinned=False)
    assert check(pinned, log).state == "possibly-stale"
    assert check(pinned, log).total == check(unpinned, log).total


def test_a_clamped_scope_still_scans_the_suffix():
    log = _log((900, [NODE_A]), (2000, [OP0]))
    assert check(_scope(log, T1a, tt_q=1000, clamped=True), log).state \
        == "possibly-stale"


def test_the_source_carries_no_pinned_short_circuit():
    """Belt and braces: the property above would still hold if a shortcut were
    added behind a flag this test does not set."""
    import inspect

    from tgms.tgir import check as check_mod
    body = inspect.getsource(check_mod.check)
    assert "pinned" not in body.replace("`pinned`", "").replace(
        "pinned scope", "").replace("pinned` = true", "")


# ---------------------------------------------------------------------------
# the suffix window
# ---------------------------------------------------------------------------

def test_only_batches_strictly_after_tt_q_are_scanned():
    log = _log((900, [NODE_A]), (2000, [OP0]))
    assert check(_scope(log, T1a, tt_q=2000), log).actionable_fresh
    assert not check(_scope(log, T1a, tt_q=1999), log).actionable_fresh


def test_tt_now_defaults_to_open_end_and_scans_the_whole_suffix():
    """D-M4a. The rounding direction is the *opposite* of `tt_q`'s: a `tt_now`
    below a batch already in the log excludes it from the scan while every
    recomputing reader can see it. The log is fsynced before apply, so passing
    the applied frontier as `tt_now` is the false-fresh direction."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    assert check(_scope(log, T1a, tt_q=1000), log).state == "possibly-stale"
    # a caller asking a narrower "as of" question owns it, and gets FRESH
    assert check(_scope(log, T1a, tt_q=1000), log, tt_now=1500).actionable_fresh


def test_tt_q_unverified_widens_to_a_full_scan_rather_than_refusing():
    """D-M4b. Setting `tt_q := 0` scans every batch ever written, which cannot
    skip anything and can therefore still return an honest `FRESH` —
    where refusing could only ever say "don't know"."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    scope = _scope(log, T1a, tt_q=9999, tt_q_verified=False)
    verdict = check(scope, log)
    assert verdict.state == "possibly-stale"
    assert "tt_q-unverified" in verdict.degraded
    # the same scope, trusted, would have skipped the whole log
    assert check(replace(scope, tt_q_verified=True), log).actionable_fresh


def test_an_unverified_tt_q_can_still_return_fresh():
    log = _log((900, [NODE_A]))
    scope = _scope(log, ScopeTerm(targets=Targets(nodes=("Z",))),
                   tt_q=9999, tt_q_verified=False)
    verdict = check(scope, log)
    assert verdict.actionable_fresh
    assert verdict.degraded == ("tt_q-unverified",)


def test_a_broken_cursor_invariant_widens_to_a_full_scan():
    """D13.8a sanctions the reset in as many words. Refusing would also be
    sound and would tell the caller nothing."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    end = [e for _b, e, _r in log.batches_from(0)][-1]
    chain = log.chain_of_prefix(end)
    # a checkpoint past the first suffix batch: the scan would start too late
    scope = _scope(log, T1a, tt_q=100, checkpoints=(Checkpoint(end, chain),))
    verdict = check(scope, log)
    assert "cursor-invariant" in verdict.degraded
    assert verdict.state == "possibly-stale"


# ---------------------------------------------------------------------------
# witnesses: order, dedup, cap
# ---------------------------------------------------------------------------

def test_witnesses_are_in_log_order_which_is_tt_then_offset():
    log = _log((900, [NODE_A]),
               (2000, [OP0]), (3000, [OP0]), (4000, [OP0]))
    verdict = check(_scope(log, T1a, tt_q=1000), log)
    assert [w.tt for w in verdict.witnesses] == [2000, 3000, 4000]


def test_the_cap_truncates_the_list_and_never_the_total_or_the_verdict():
    """The cap is a presentation limit and can never change the verdict."""
    log = _log((900, [NODE_A]),
               *[(2000 + i, [OP0]) for i in range(WITNESS_CAP + 15)])
    verdict = check(_scope(log, T1a, tt_q=1000), log)
    assert verdict.state == "possibly-stale"
    assert len(verdict.witnesses) == WITNESS_CAP
    assert verdict.total == WITNESS_CAP + 15


def test_the_cap_is_applied_after_matched_on_accounting():
    """§9.8: truncating before the accounting would silently truncate the
    per-arm precision numbers along with the witness list."""
    log = _log((900, [NODE_A]), *[(2000 + i, [OP0]) for i in range(5)])
    full = check(_scope(log, T1a, tt_q=1000), log, witness_cap=100)
    capped = check(_scope(log, T1a, tt_q=1000), log, witness_cap=1)
    assert capped.total == full.total == 5
    assert capped.witnesses[0].matched_on == full.witnesses[0].matched_on


def test_a_witness_is_checkable_against_the_log_rather_than_trusted():
    """D13.27's third consumer: `batch_id` and `tt` locate the op, so the
    witness can be re-derived from the log instead of believed."""
    log = _worked_example_log()
    verdict = check(_scope(log, T1a, T1b, T1c, T2, tt_q=1000), log)
    for w in verdict.witnesses:
        batch = next(b for b in log.batches() if b["batch_id"] == w.batch_id)
        assert batch["tt"] == w.tt
        redrived = footprints_of_op(batch["ops"][w.op_seq], w.op_seq)
        arm = next(f for f in redrived if f.arm == w.arm
                   and f.entity_kind == ("node" if "uid" in w.identity else "edge"))
        assert arm.kind == w.kind
        assert arm.identity.to_json() == w.identity


# ---------------------------------------------------------------------------
# the chain cache (§3.9) is correctness-neutral
# ---------------------------------------------------------------------------

def test_the_chain_cache_changes_no_verdict():
    """It caches a pure function of file bytes. The report states check
    latency with and without it, because the cost claim is about the mechanism
    and the cache is an implementation convenience."""
    log = _log((900, [NODE_A]), (2000, [OP0, OP1]))
    scope = _scope(log, T1a, T1b, T1c, T2, tt_q=1000)
    cache = ChainCache()
    plain = check(scope, log)
    cached = [check(scope, log, chain_cache=cache) for _ in range(3)]
    assert all(c.to_json() == plain.to_json() for c in cached)
    assert cache.hits == 2 and cache.misses == 1


def test_the_cache_re_walks_after_the_log_grows():
    log = _log((900, [NODE_A]))
    scope = _scope(log, T1a, tt_q=1000)
    cache = ChainCache()
    assert check(scope, log, chain_cache=cache).actionable_fresh
    log.append(2000, [OP0])
    assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    assert cache.misses == 2


def test_a_tampered_prefix_is_caught_with_the_cache_in_hand():
    """The walk *is* the tamper-evidence check, so a cache that answered from a
    stale entry would be answering the one question it exists to verify."""
    log = _log((900, [NODE_A]), (1500, [MID]), (2500, [OP0]))
    ends = [e for _b, e, _r in log.batches_from(0)]
    scope = _scope(log, T1a, tt_q=1600,
                   checkpoints=(Checkpoint(ends[1], log.chain_of_prefix(ends[1])),))
    cache = ChainCache()
    assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    raw = log.path.read_bytes().split(b"\n")
    raw[2] = raw[2].replace(b'"silv"', b'"lead"')   # inside the checkpointed prefix
    assert b'"lead"' in raw[2]
    log.path.write_bytes(b"\n".join(raw))
    assert check(scope, log, chain_cache=cache).reason == "log-rewritten"


def test_a_rewrite_BEYOND_the_checkpoint_is_not_tamper_evident_and_should_not_be():
    """An honest limit of D13.18, recorded rather than papered over.

    A checkpoint attests to the **applied prefix**. Records after it are not
    covered by any chain the scope carries, so editing one is invisible to step
    6 — the scan simply reads the edited record. That is not a soundness hole:
    the suffix is what `check` reads *in order to invalidate*, and a tampered
    suffix can only change which witnesses appear, never license a `FRESH` that
    the untampered suffix would have refused... unless the tamper deletes an
    intersecting op outright, which is a threat model (an adversary with write
    access to the log) that v1 does not claim to cover and §13.11 does not
    either.
    """
    log = _log((900, [NODE_A]), (1500, [MID]), (2500, [OP0]))
    ends = [e for _b, e, _r in log.batches_from(0)]
    scope = _scope(log, T1a, tt_q=1600,
                   checkpoints=(Checkpoint(ends[1], log.chain_of_prefix(ends[1])),))
    raw = log.path.read_bytes().split(b"\n")
    raw[3] = raw[3].replace(b'"gold"', b'"lead"')   # the suffix, past the checkpoint
    assert b'"lead"' in raw[3]
    log.path.write_bytes(b"\n".join(raw))
    verdict = check(scope, log)
    assert verdict.reason is None
    assert verdict.state == "possibly-stale"        # still caught, by the scan


def test_rewriting_the_genesis_batch_is_caught_as_a_store_mismatch():
    """A stronger signal than `log-rewritten`, and it fires earlier: the store
    identity is the digest of the header with the **first batch**, so editing
    the genesis record makes this a different store rather than a tampered
    history."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    scope = _scope(log, T1a, tt_q=1000)
    raw = log.path.read_bytes().split(b"\n")
    raw[1] = raw[1].replace(b'"A"', b'"Z"')
    log.path.write_bytes(b"\n".join(raw))
    assert check(scope, log).reason == "store-mismatch"


def test_the_walk_reproduces_chain_of_prefix_exactly():
    """The one-walk fold of steps 5 and 6 has to agree with the function it
    replaces, at every boundary — otherwise the cost mitigation of §8.1 is
    bought with a different answer."""
    from tgms.tgir.check import _walk

    log = _log((900, [NODE_A]), (2000, [OP0]), (3000, [OP1]))
    walked = _walk(log)
    assert len(walked.chains) == 4      # the seed plus three record boundaries
    assert walked.chains[0] == SEED_CHAIN
    for offset, chain in walked.chains.items():
        assert log.chain_of_prefix(offset) == chain


def test_the_cursor_invariant_is_answered_from_the_same_walk():
    """E-2: *"Step 5's cursor invariant is verified inside step 6's walk, so it
    costs nothing extra and should not appear as a separate line."* The walk
    therefore carries each record's `(tt, start offset)` alongside its chain,
    and the invariant is a lookup rather than a second scan."""
    from tgms.tgir.check import _walk

    log = _log((900, [NODE_A]), (2000, [OP0]), (3000, [OP1]))
    walked = _walk(log)
    assert [tt for tt, _s in walked.starts] == [900, 2000, 3000]
    # the first suffix batch's start is the previous record's end
    ends = [e for _b, e, _r in log.batches_from(0)]
    assert walked.first_start_after(1000) == ends[0]
    assert walked.first_start_after(2500) == ends[1]
    assert walked.first_start_after(9999) is None


# ---------------------------------------------------------------------------
# per-step versus merged (D-M4e) — the substrate M4.4's surface wraps
# ---------------------------------------------------------------------------

def test_the_merged_check_is_never_fresh_where_the_per_step_fold_is_stale():
    """Monotonicity of the widening. The merged scope forces the **earliest**
    `tt_q` onto every term, which is the widening FF-7 required for a single
    scope object and is not required while the steps are still separate — so
    merging can only ever add witnesses."""
    log = _log((900, [NODE_A]), (2000, [OP0]), (3000, [OP0]))
    s1 = _scope(log, T1a, tt_q=1000)
    s2 = _scope(log, T2, tt_q=2500)
    per = check_steps([("s1", s1), ("s2", s2)], log)
    merged = check(s1.union(s2), log)
    assert not per.actionable_fresh
    assert not merged.actionable_fresh
    assert merged.total >= len(per.witnesses)


def test_per_step_keeps_each_steps_own_tt_q():
    """The point of D-M4e: a later step is not dragged back to the earliest
    `tt_q` in the plan."""
    log = _log((900, [NODE_A]), (2000, [OP0]))
    early = _scope(log, T1a, tt_q=1000)
    late = _scope(log, T1a, tt_q=2000)
    per = dict(check_steps([("s1", early), ("s2", late)], log).per_step)
    assert per["s1"].state == "possibly-stale"
    assert per["s2"].actionable_fresh
    merged = check(early.union(late), log)
    assert merged.total == 1        # the merged scope drags s2 back with it


def test_a_steps_verdict_reports_both_granularities():
    log = _log((900, [NODE_A]), (2000, [OP0]))
    per = check_steps([("s1", _scope(log, T1a, tt_q=1000)),
                       ("s2", DependencyScope.empty(_identity(log), 1000))], log)
    out = per.to_json()
    assert out["verdict"] == "possibly-stale"
    assert out["steps"]["s2"]["verdict"] == "fresh"


def test_a_plan_is_not_fresh_when_any_step_is_undecidable():
    """D13.25: `UNDECIDABLE` is not a third contract."""
    log = _log((900, [NODE_A]))
    good = _scope(log, T1a, tt_q=1000)
    bad = replace(good, store="elsewhere")
    per = check_steps([("s1", good), ("s2", bad)], log)
    assert not per.actionable_fresh
    assert per.reasons == ("store-mismatch",)


# ---------------------------------------------------------------------------
# §9.7 — the EdgeKey form of the edges arm (spec completion, not ruled)
# ---------------------------------------------------------------------------

def test_E1_an_eid_keyed_term_matches_an_ingest_of_an_existing_edge():
    """**E-1**, the blocking soundness erratum, end to end.

    An event carrying an explicit `disc` writes the logical edge that `disc`
    names — which may already exist. Before E-1 the `ingest_events` edge arm
    carried no `eid`, an absent field is ∅ under D13.5, and a term scoped by
    `edges` therefore **missed** the write: a false negative, the one direction
    D1.13 forbids.
    """
    eid = edge_eid("C", "D", "MSG", "d9")
    ingest = make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45, "disc": "d9"}])
    log = _log((900, [NODE_A]), (2000, [ingest]))
    scope = _scope(log, ScopeTerm(targets=Targets(edges=(eid,))), tt_q=1000)
    verdict = check(scope, log)
    assert verdict.state == "possibly-stale", "E-1: the false negative it closes"
    assert verdict.witnesses[0].matched_on == ("targets.edges",)


def test_E1_the_eid_set_coarsens_to_star_above_the_threshold():
    """*"coarsening to `"*"` above an implementation-chosen size threshold"*.
    `meets(T.edges, "*")` is true, so the coarsened form is sound too — it only
    ever adds matches."""
    from tgms.tgir.footprint import COARSEN_ABOVE

    big = make_op("ingest_events", events=[
        {"src": f"n{i}", "dst": "D", "rel_type": "MSG", "vt_s": i}
        for i in range(COARSEN_ABOVE + 1)])
    edge, _node = footprints_of_op(big, 0)
    assert edge.identity.eid is TOP
    assert intersects(ScopeTerm(targets=Targets(edges=("anything-at-all",))),
                      edge)[0] is Match.HIT


def test_E4b_a_field_the_footprint_does_not_carry_is_a_wildcard():
    """**E-4(b)**'s second branch, and the one that matters in practice:
    `ingest_events`' edge arm coarsens `disc` away (D13.22), so an `EdgeKey`
    naming `disc` must still meet a footprint that has none. Returning `False`
    there would be a false negative."""
    ingest = footprints_of_op(make_op("ingest_events", events=[
        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45}]), 0)[0]
    assert ingest.identity.disc is None
    for key in (EdgeKey(disc=""), EdgeKey(disc="zzz"), EdgeKey(src="C", disc="")):
        assert intersects(ScopeTerm(targets=Targets(edges=(key,))),
                          ingest)[0] is Match.HIT, key
    # a field the footprint DOES carry is still tested
    assert intersects(ScopeTerm(targets=Targets(edges=(EdgeKey(src="Z"),))),
                      ingest)[0] is Match.MISS


def test_E4b_an_edge_key_stating_nothing_is_star():
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    assert intersects(ScopeTerm(targets=Targets(edges=(EdgeKey(),))),
                      fp)[0] is Match.HIT


def test_E4b_an_unrecognized_entry_form_refuses_rather_than_missing():
    """D13.23a, the rule FF-8 exists to have installed."""
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", props={},
                vt_s=0, vt_e=10), 0)[0]
    term = ScopeTerm(targets=Targets(edges=("valid-eid",)))
    object.__setattr__(term.targets, "edges", (42,))    # a future wire form
    assert intersects(term, fp)[0] is Match.REFUSE


def test_an_edge_key_matches_on_the_fields_it_names():
    fp = footprints_of_op(
        make_op("assert_edge", src="A", dst="B", rel_type="MSG", disc="d1",
                props={}, vt_s=0, vt_e=10), 0)[0]
    assert intersects(ScopeTerm(targets=Targets(edges=(EdgeKey(src="A"),))),
                      fp)[0] is Match.HIT
    assert intersects(ScopeTerm(targets=Targets(
        edges=(EdgeKey(src="A", dst="B", rel_type="MSG", disc="d1"),))), fp)[0] \
        is Match.HIT
    assert intersects(ScopeTerm(targets=Targets(edges=(EdgeKey(src="Z"),))),
                      fp)[0] is Match.MISS


# ---------------------------------------------------------------------------
# explain (the memo's sentence)
# ---------------------------------------------------------------------------

def test_a_verdict_renders_the_memo_sentence_from_a_real_witness():
    log = _worked_example_log()
    verdict = check(_scope(log, T1a, T1b, T1c, T2, tt_q=1000), log,
                    term_steps=["s1", "s1", "s1", "s2"])
    text = render(verdict, produced_tt=1000)
    assert "node A" in text
    assert "Reconsider." in text
    assert "the edge C→D (MSG)" in text


def test_undecidable_renders_as_may_be_stale_never_as_unknown():
    """D13.25: a rendering that offers a third mood invites a reader to treat
    "don't know" as "probably fine"."""
    log = _log((900, [NODE_A]))
    text = render(check(replace(_scope(log), store="elsewhere"), log))
    assert "may be stale" in text
    assert "not produced against this store" in text


def test_fresh_renders_without_inventing_a_repair():
    log = _log((900, [NODE_A]))
    text = render(check(DependencyScope.empty(_identity(log), 9999), log))
    assert "Nothing written since could have changed it" in text


def test_a_capped_render_says_how_many_there_really_were():
    log = _log((900, [NODE_A]), *[(2000 + i, [OP0]) for i in range(WITNESS_CAP + 3)])
    text = render(check(_scope(log, T1a, tt_q=1000), log))
    assert f"{WITNESS_CAP + 3} in total" in text


def test_a_plan_render_keeps_per_step_attribution():
    log = _log((900, [NODE_A]), (2000, [OP0]))
    per = check_steps([("s1", _scope(log, T1a, tt_q=1000)),
                       ("s2", DependencyScope.empty(_identity(log), 1000))], log)
    text = render_steps(per)
    assert "s1:" in text and "s2:" not in text


@pytest.mark.parametrize("identity,expect", [
    ({"uid": "A"}, "node A"),
    ({"uid": ["A"]}, "node A"),
    ({"uid": ["A", "B"]}, "2 nodes"),
    ({"src": ["C"], "dst": ["D"], "rel_type": ["MSG"]}, "the edge C→D (MSG)"),
    ({"src": "C", "dst": "D"}, "the edge C→D"),
])
def test_identity_renders_the_way_a_person_would_name_it(identity, expect):
    assert render_identity(identity) == expect
