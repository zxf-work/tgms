"""M4.3 — the soundness suite: the contract's own counterexamples, executable.

**The one-sided contract this file exists to defend** (FRESHNESS_SEMANTICS
D1.13): `V(R, τ_now) = FRESH ⇒ FRESH*(R, τ_now)`. *False invalidation is
allowed; false freshness is not.* Every test below is one half of that
implication made concrete: a store, a query, a correction that provably changes
the answer, and the assertion that the mechanism does not say `FRESH`.

**Twenty-one required scenarios** (M4 plan §5): the six counterexamples of §3
and the fifteen gate-review findings re-run in §13.9 — FF-1 A/B/mirror,
FF-2 a/b, FF-3, FF-4, FF-5, FF-6, FF-7 a/b/c, FF-8, FF-9, RG-1 — plus the two
shape regressions (CO-3, CO-8) and the two integration cases (§13.6's worked
example and its variant). Each scenario asserts three things, and the second is
the one that matters:

1. the result really did change, by **recompute-and-compare** (D1.11, D6.1) —
   never by assertion about the store;
2. the verdict is `POSSIBLY_STALE` **through the arm or conjunct the frozen
   document names**. Verdict alone is insufficient: several of these pass for
   the wrong reason under a mechanism that is `"*"` everywhere, which is
   precisely the mechanism §6's measurement must distinguish itself from;
3. where the scenario has a **must-not-fire sibling**, that its `FRESH` half
   still holds — the precision claim that would rot silently if a derivation
   were coarsened, since widening can never fail anything else in the tree.

*"A fix is not accepted here unless its own scenario is caught."* (§13.9.)

The file closes with the **contract-level invariants** the documents state as
properties rather than as scenarios: `∅ ⇒ FRESH` always (D13.2, D5.3);
widening any component of a scope never turns `POSSIBLY_STALE` into `FRESH`
(D4.5, D13.1); `UNDECIDABLE` is never downgraded to `FRESH` (D13.25); and a
result pinned at or below the frontier still has its suffix scanned — **T1 does
not exempt it** (D13.24, and FF-4 is what the exemption cost).

**Errata incorporated: §15's register as of 2026-08-22** — E-1 (BLOCKING;
`ingest_events` with an explicit `disc` writes an *existing* `eid`, so the
coarsened edge arm must carry an `eid` set), E-3 (`matched_on` lists a conjunct
**only when both sides were concrete and intersected** — §13.6's spelling wins
and D13.27's specimen is erroneous), and E-4 (a scan's belief-mode soundness is
a named dependency on `P` naming `@recut`/`@version`; the `edges` arm's
`EdgeKey` form gains an intersection rule). E-2 amends D13.26's cost claim and
is an honesty item with no assertion to make here. §15 is append-only and a
reader of any definition is expected to read it, so this file cites the
definition **and** its erratum wherever one exists.

**Written as an independent oracle.** This file is derived from
`docs/design/FRESHNESS_SEMANTICS.md`, `docs/design/tgir_b1/B2C_GATE_REVIEW.md`
and `docs/design/M4_IMPLEMENTATION_PLAN.md` §M4.2/§M4.3 alone. Neither
`tgms/tgir/footprint.py` nor `tgms/tgir/check.py` was read while writing it, by
the same discipline §3.8 imposes in the other direction: the two must be
independent transcriptions of D13.20–D13.25, or their agreement is evidence of
nothing. Where the implementation and this file disagree, the frozen document
decides, and the disagreement is a finding under the plan's §9 — not a test
relaxation (§6, M4.3's exit gate).
"""

from __future__ import annotations

import pytest

# The suite arms itself the moment the implementation lands, and lands
# green-as-skipped before then (M4 plan §7.3: M4.3 is authored in parallel with
# M4.1/M4.2, from documents in which every scenario is fully specified).
pytest.importorskip("tgms.tgir.check",
                    reason="M4.2's checker is not implemented yet")

from dataclasses import replace  # noqa: E402

from tgms.core.errors import InvalidArgError, NotFoundError  # noqa: E402
from tgms.core.model import OPEN_END, EntityRef, edge_eid  # noqa: E402
from tgms.storage.base import make_op  # noqa: E402
from tgms.tgir.anchor import anchor_of_var  # noqa: E402
from tgms.tgir.depscope import (  # noqa: E402
    TOP, Checkpoint, DependencyScope, EdgeKey, Incident, K_EDGE, K_NODE,
    ScopeTerm, Targets, vt_closed,
)
from tgms.tgir.expr import Col, PropRef  # noqa: E402
from tgms.tgir.node import (  # noqa: E402
    EdgePat, EdgeScan, Endpoints, Exact, Expand, NodePat, NodeScan, Pattern,
    PatternMatch, Project, Source,
)
from tgms.tgir.types import Sigma  # noqa: E402

from . import freshness_fixtures as fx  # noqa: E402


@pytest.fixture()
def stores(tmp_path):
    """A factory for real on-disk stores, closed at teardown.

    Each scenario gets its own store: an isolation bug in a shared one is a
    false-fresh factory (M4 plan §8.6), and the suite must not be able to
    manufacture the very error it is here to detect.
    """
    made = []

    def make(name: str = "s"):
        s = fx.open_store(tmp_path, f"{name}-{len(made)}")
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


# ===========================================================================
# §3 — the six counterexamples. "Each item becomes a test case."
# ===========================================================================

def test_ce1_reachability_gains_a_witness_with_no_row_to_touch(stores):
    """**CE-1** (§3.1). *Reachability gains a witness; the old result has no
    edge rows to touch.*

    `Q₁ = temporal_reachability(src="A", window=[0,100))` over `S₀` answers
    `[(X,0), (B,10), (C,20), (D,30)]` — **the result contains no edge rows at
    all**. The correction `ingest_events([C→D @25])` is Class A: a fresh
    `disc`, a fresh `eid`, nothing superseded, **zero existing version rows
    modified**. `D`'s arrival moves 30 → 25.

    Why it defeats the naive rule (D6.4): there is no row in `R₁` for the
    correction to touch even in principle, so row-touch returns `FRESH`
    vacuously. The dependency is on *what paths exist* (§9.13's `I = ⊤`, and
    D4.3's quantification over the suffix jointly), which positive evidence
    does not describe.
    """
    s = stores("ce1")
    fx.build_s0(s)
    q = {"src": "A", "window": {"t_a": 0, "t_b": 100}}

    before = fx.call(s, "temporal_reachability", q)
    assert [r["uid"] for r in before["rows"]] == ["X", "B", "C", "D"]
    scope = DependencyScope.from_json(before["dependency"])

    s.ingest_events([{"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 25}])

    after = fx.call(s, "temporal_reachability", q)
    assert after["rows"][-1] == {"uid": "D", "earliest_arrival": 25}
    assert after["result_digest"] != before["result_digest"], "CE-1: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    ws = fx.assert_stale(verdict, "CE-1")
    # `ingest_events` supersedes nothing, so it emits **no carve arm** (D13.21a,
    # D2.1): the catch is on the value arm of a brand-new edge identity — the
    # `incident` arm's reason for existing (D13.4), which matches an edge write
    # whose `eid` did not exist when the scope was written.
    assert fx.arms(verdict) == {"value"}, fx.describe(verdict)
    assert fx.kinds(verdict) == {"ingest_events"}, fx.describe(verdict)
    assert all(w.get("class") == "A" for w in ws), "CE-1: Class A (D13.20)"


def test_ce2_topk_displaced_by_a_group_that_did_not_exist(stores):
    """**CE-2** (§3.2). *Top-k displaced by a group that did not exist.*

    `s1 = aggregate_events(group_by=[endpoint src], count, [0,100), MSG)` over
    `S₀` gives `[A:2, B:1]` — **`C` and `D` have no row at all**, because the
    operator emits only non-empty groups. `s2 = topk(k=1)` answers `A`. Three
    appended `C→A` events make it answer `C`.

    No returned row was modified: `A:2` and `B:1` are unchanged and still
    present. A group *absent from the evidence* displaced the answer, so the
    dependency is on the absence of a heavier group anywhere in the scan region
    (§10, D13.12 — the scope covers the scan, never the rows).
    """
    s = stores("ce2")
    fx.build_s0(s)
    q = {"group_by": [{"dim": "endpoint", "role": "src"}],
         "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["MSG"]}

    before = fx.call(s, "aggregate_events", q)
    assert before["rows"] == [{"src": "A", "count": 2}, {"src": "B", "count": 1}]
    scope = DependencyScope.from_json(before["dependency"])

    s.ingest_events([{"src": "C", "dst": "A", "rel_type": "MSG", "vt_s": t}
                     for t in (40, 41, 42)])

    after = fx.call(s, "aggregate_events", q)
    assert {r["src"]: r["count"] for r in after["rows"]} == {"A": 2, "B": 1, "C": 3}
    assert after["result_digest"] != before["result_digest"], "CE-2: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "CE-2")
    # three conjuncts are concrete on both sides here and so are real
    # attribution: `K = ℰ`, `T = ["MSG"]`, `V = [0,100)` (§9.7).
    for conjunct in ("kinds", "rel_types", "vt"):
        fx.assert_matched(verdict, conjunct, "CE-2")
    assert fx.arms(verdict) == {"value"}, fx.describe(verdict)


def test_ce3_a_nonexistence_answer_has_no_positive_evidence(stores):
    """**CE-3** (§3.3). *A nonexistence answer has no positive evidence
    whatsoever.*

    `find_temporal_motif_instances(M_triangle_cyclic, delta=100, [0,100))` over
    `S₀` returns `rows = []`, `rows_total = 0` — the answer's content is
    *"there are no cyclic triangles in this window"*. One appended `D→A @40`
    completes `A→B(10), B→D(30), D→A(40)` and `rows_total` becomes 1.

    "The evidence is the empty list; there is nothing to touch, and every
    row-touch rule returns `FRESH` vacuously **forever**." The dependency is the
    entire scanned region — every event in the window and the absence of any
    further ones (§10, D10.1's *any zero count, empty row list, or absent
    group*), which is why the scope of an empty answer must not be empty.
    """
    s = stores("ce3")
    fx.build_s0(s)
    q = {"motif": "M_triangle_cyclic", "delta": 100,
         "window": {"t_a": 0, "t_b": 100}}

    before = fx.call(s, "find_temporal_motif_instances", q)
    assert before["rows"] == [] and before["rows_total"] == 0
    scope = DependencyScope.from_json(before["dependency"])
    # §10/D13.12, stated as the assertion the row-touch rule fails: a
    # nonexistence answer carries a **non-empty** scope. `terms == []` is ∅ and
    # means "nothing can ever invalidate this" (D13.2) — correct for a
    # `compute` over literals, catastrophic here.
    assert scope.terms, "CE-3: an empty answer must not carry the empty scope"

    s.ingest_events([{"src": "D", "dst": "A", "rel_type": "MSG", "vt_s": 40}])

    after = fx.call(s, "find_temporal_motif_instances", q)
    assert after["rows_total"] == 1
    assert after["result_digest"] != before["result_digest"], "CE-3: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "CE-3")
    assert fx.kinds(verdict) == {"ingest_events"}, fx.describe(verdict)


def test_ce4_a_correction_disjoint_from_the_query_window(stores):
    """**CE-4** (§3.4). *A correction whose valid interval is disjoint from the
    query window.*

    `graph_metric_timeseries(new_node_rate, window=[50,100), stride=10)` puts
    `W` (first seen at 60) in the bucket `[60,70)`. The correction
    `assert_node("W", vt_s=20, vt_e=30)` overlaps **nothing**, so it is a Class
    A append and `W` gains a second believed version. `new_node_rate` computes
    first appearance as the **minimum `vt_s` over all of an identity's believed
    versions** — scanning `nodes_columnar(vt_max=t_b)` with **no `vt_min`** —
    so `W`'s birth moves to 20 and the bucket drops to 0.

    *"Any domain whose valid-time component is 'the query window' is unsound for
    this operator. The correct component is `[0, t_b)`."*
    """
    s = stores("ce4")
    fx.build_s0(s)
    s.assert_node("W", "Node", {}, 60, 70)
    q = {"metric": "new_node_rate", "window": {"t_a": 50, "t_b": 100},
         "stride": 10}

    before = fx.call(s, "graph_metric_timeseries", q)
    assert [r["value"] for r in before["rows"]] == [0, 1, 0, 0, 0]
    scope = DependencyScope.from_json(before["dependency"])

    s.assert_node("W", "Node", {}, 20, 30)

    after = fx.call(s, "graph_metric_timeseries", q)
    assert [r["value"] for r in after["rows"]] == [0, 0, 0, 0, 0]
    assert after["result_digest"] != before["result_digest"], "CE-4: ¬FRESH*"

    # The correction's own footprint is `[20, 31)` (D13.21's `vt_closed`), which
    # is disjoint from the query window `[50, 100)` — this is the arithmetic a
    # window-scoped `V` would have used to answer FRESH.
    (fp_lo, fp_hi), = vt_closed(20, 30)
    assert (fp_lo, fp_hi) == (20, 31)
    assert not (fp_lo < 100 and 50 < fp_hi), "CE-4: the op misses the window"

    fx.assert_stale(fx.check_scope(s, scope), "CE-4")


def test_ce5_a_property_only_correction_triples_an_event_count(stores):
    """**CE-5** (§3.5). *A property-only correction triples an event count.*

    `aggregate_events(group_by=[], count, [0,100), ROLE)` over `S₀` counts the
    one believed `ROLE` version `[0,100)` as one event at `vt_s = 0`. A
    `correct` over `[30,40)` supersedes it and `_remainder` writes three rows —
    `[0,30)`, `[30,40)`, `[40,100)` — all with `vt_s` inside the window.
    **Count = 3.**

    The query reads **no property at all**, so a scope narrowed to "the
    properties this query reads" would call the correction irrelevant. The
    channel is `@event_key` on the **value** arm (D13.22, D13.7a): an edge
    version *is* an event at `vt_s`, and carving changes event multiplicity.
    That the value arm suffices — Appendix A.3's re-derivation — is exactly what
    lets this operator keep `V = window` against the carve arm (D13.21a).
    """
    s = stores("ce5")
    fx.build_s0(s)
    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["ROLE"]}

    before = fx.call(s, "aggregate_events", q)
    assert before["rows"] == [{"count": 1}]
    scope = DependencyScope.from_json(before["dependency"])
    assert "@event_key" in scope.terms[0].props, "CE-5: §9.7's `Pᵥ` names it"
    assert "@recut" not in scope.terms[0].props, "CE-5: and not the carve keys"

    s.correct(EntityRef(kind="edge", src="A", dst="X", rel_type="ROLE", disc=""),
              {"level": 2}, 30, 40)

    after = fx.call(s, "aggregate_events", q)
    assert after["rows"] == [{"count": 3}], "CE-5: one version became three"
    assert after["result_digest"] != before["result_digest"], "CE-5: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "CE-5")
    assert fx.arms(verdict) == {"value"}, (
        "CE-5 is caught by the value arm's `@event_key`, not by carving — "
        f"{fx.describe(verdict)}")
    fx.assert_matched(verdict, "props", "CE-5")
    assert fx.kinds(verdict) == {"correct"}


def test_ce5_sibling_a_recut_outside_the_window_must_not_fire(stores):
    """**CE-5, the must-not-fire half** (§9.7's own scenario, and the reason
    `P = Pᵥ` rather than `⊤`).

    Same edge, believed `[0,100)`, read with `window = [0,20)`. A `correct` over
    `[50,60)` — entirely outside the window — leaves the **count** untouched.
    The value arm's `vt = [50,61)` misses `[0,20)` and the carve arm is excluded
    by `P`, so the scope says the result cannot have changed. It has not.

    If this assertion ever fails, `V = window` has stopped meaning anything for
    every event-keyed operator: the carve arm's `vt = "*"` overlaps every
    window, so `P` is the only thing that can exclude it (D13.7a).
    """
    s = stores("ce5b")
    s.assert_node("A", "Node", {}, 0, OPEN_END)
    s.assert_node("X", "Node", {}, 0, OPEN_END)
    s.assert_edge("A", "X", "ROLE", {"level": 1}, 0, 100)
    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 20}, "rel_types": ["ROLE"]}

    before = fx.call(s, "aggregate_events", q)
    scope = DependencyScope.from_json(before["dependency"])

    s.correct(EntityRef(kind="edge", src="A", dst="X", rel_type="ROLE", disc=""),
              {"level": 2}, 50, 60)

    after = fx.call(s, "aggregate_events", q)
    assert after["result_digest"] == before["result_digest"], "the count held"
    fx.assert_fresh(fx.check_scope(s, scope), "CE-5 sibling")


def test_ce6_a_plan_whose_upstream_arguments_move(stores):
    """**CE-6** (§3.6, §5). *A plan whose upstream arguments move.*

    ```
    s1 = resolve_entities(query="acme")                  → ["acme-hq"]
    s2 = aggregate_events(…, endpoint_filter={either, uids: $ref s1.rows[*].uid})
    ```

    The correction adds `acme-west` and five `MSG` events touching it. `s2`'s
    domain, computed from its **recorded** args, covers only edges incident to
    `acme-hq`: the new events fall outside it and its own scope is `FRESH`. It
    is `s1`'s scope — a step whose rows are consumed as *arguments* and never
    returned — that contains the correction.

    This is §5's prohibition 1 as a soundness requirement rather than a
    coverage nicety: *"You may not drop an intermediate step's domain because
    its rows do not reach the answer."* `resolve_entities` is excluded from M4's
    **measurement** (§9.5's ruling) and not from the contract, so it carries its
    scope here exactly as every operator does.
    """
    s = stores("ce6")
    s.assert_node("acme-hq", "Org", {"name": "Acme HQ"}, 0, OPEN_END)
    s.assert_node("other", "Org", {"name": "Other"}, 0, OPEN_END)
    s.ingest_events([{"src": "acme-hq", "dst": "other", "rel_type": "MSG",
                      "vt_s": 10}])

    q1 = {"query": "acme"}
    r1 = fx.call(s, "resolve_entities", q1)
    assert [row["uid"] for row in r1["rows"]] == ["acme-hq"]
    scope_s1 = DependencyScope.from_json(r1["dependency"])

    q2 = {"group_by": [{"dim": "rel_type"}], "aggregates": [{"agg": "count"}],
          "window": {"t_a": 0, "t_b": 100},
          "endpoint_filter": {"role": "either", "uids": ["acme-hq"]}}
    r2 = fx.call(s, "aggregate_events", q2)
    scope_s2 = DependencyScope.from_json(r2["dependency"])
    assert scope_s2.terms[0].targets.incident.uids == ("acme-hq",)

    s.assert_node("acme-west", "Org", {"name": "Acme West"}, 0, OPEN_END)
    s.ingest_events([{"src": "acme-west", "dst": "other", "rel_type": "MSG",
                      "vt_s": t} for t in (20, 21, 22, 23, 24)])

    # ground truth: `s1` now resolves two uids, so `s2`'s *arguments* move and
    # the plan's answer with them (D1.11 at both granularities, D5.4).
    assert [row["uid"] for row in fx.call(s, "resolve_entities", q1)["rows"]] == \
        ["acme-hq", "acme-west"]
    assert fx.digest_of(s, "resolve_entities", q1) != r1["result_digest"]

    # the downstream step's own scope is FRESH — correctly, under T5.1's
    # induction hypothesis, and uselessly on its own
    fx.assert_fresh(fx.check_scope(s, scope_s2), "CE-6: s2's own scope")
    # and the union discharges the hypothesis (D5.2, T5.1)
    plan_scope = scope_s1.union(scope_s2)
    fx.assert_stale(fx.check_scope(s, plan_scope), "CE-6: the plan scope")
    fx.assert_stale(fx.check_scope(s, scope_s1), "CE-6: s1's scope is the catch")


# ===========================================================================
# §13.9 — the gate review's false-fresh findings, each re-run
# ===========================================================================

def test_ff1a_version_history_recut_outside_the_window(stores):
    """**FF-1 A** — *the carve rewrites rows arbitrarily far outside the op's
    own valid interval.*

    `assert_node("u","L",{"p":1},0,100)`; read
    `version_history(kind="node", window=[0,20), belief="current")` → one row
    with `vid = V₁`, `vt_e = 100`. `correct(u, {"p":2}, 50, 60)` supersedes it
    and `_remainder` leaves `[0,50)` in the window with a **different `vid` and
    a different `vt_e`** (and, under `belief="all"`, an in-window row *count* of
    2 rather than 1, so this is not a `vid`-only artifact).

    The op's own footprint is `vt_closed(50,60) = [50,61)`, which misses
    `[0,20)` entirely: **conjunct 4 fails on the value arm.** Only the carve arm
    (`vt = "*"`, `props = {@recut, @version}`) reaches §9.6's `P = ⊤`, and it is
    the whole of FF-1's fix (D13.21a).

    The scope under test is the one **§9.6 specifies** — `V = window`,
    `P = ⊤` — because that is the derivation whose soundness FF-1 is about; the
    live tree carries the coarser `"*"` fallback, which is a widening (D13.1)
    and is asserted separately below to be no weaker.
    """
    s = stores("ff1a")
    s.assert_node("u", "L", {"p": 1}, 0, 100)
    q = {"kind": "node", "window": {"t_a": 0, "t_b": 20}, "belief": "current"}

    before = fx.call(s, "version_history", q)
    assert before["rows"][0]["vt_e"] == 100 and before["rows_total"] == 1
    live_scope = DependencyScope.from_json(before["dependency"])
    # §9.6: `K` = the ops that write versions of `kind`; `I = ⊤`; `V = window`;
    # `P = ⊤` — rows carry `vid`, `vt_s`, `vt_e`, `tt_s`, `tt_e`.
    spec_scope = fx.scope_from_terms(s, ScopeTerm(
        kinds=K_NODE, targets=TOP, rel_types=TOP,
        vt=((0, 20),), vt_mode="overlap", props=TOP))

    s.correct(EntityRef(kind="node", uid="u"), {"p": 2}, 50, 60)

    after = fx.call(s, "version_history", q)
    assert after["rows"][0]["vt_e"] == 50
    assert after["rows"][0]["vid"] != before["rows"][0]["vid"]
    assert after["result_digest"] != before["result_digest"], "FF-1 A: ¬FRESH*"
    assert fx.call(s, "version_history",
                   {**q, "belief": "all"})["rows_total"] == 2

    verdict = fx.check_scope(s, spec_scope)
    fx.assert_stale(verdict, "FF-1 A")
    assert fx.arms(verdict) == {"carve"}, (
        "FF-1 A is the carve arm's headline scenario: the value arm's "
        f"`vt = [50,61)` cannot meet `[0,20)` — {fx.describe(verdict)}")
    assert "vt" not in fx.matched_on(verdict), (
        "the carve arm's `vt` is `\"*\"`, so it is not attribution (E-3): the "
        f"scope's window bought nothing here — {fx.describe(verdict)}")
    # and the live coarse scope is a widening of it, never weaker (D13.1)
    fx.assert_stale(fx.check_scope(s, live_scope), "FF-1 A: the live scope")


def test_ff1b_nodescan_at_an_instant_under_a_narrowed_sigma(stores):
    """**FF-1 B** — *the compositional core, `NodeScan` under any narrowed Σ.*

    `NodeScan(uids=["u"]) @ Σ = (instant [10,11))` returns one row whose `vt_e`
    column reads 100. `assert_node("u","L",{"p":2},50,60)` is Class B — it
    overlaps, so `_assert_node` carves — and the version valid at 10 becomes
    `[0,50)`: the `vt_e` column reads 50 and `vid` changed.

    §2.1 emits `vt_s` and `vt_e` on **every** row, so `@recut` is unconditional
    for `NodeScan`/`EdgeScan` and **there is no Level-0 escape by projection**
    (D13.15). The value arm's `[50,61)` misses the instant; the carve arm is
    the only thing left.
    """
    s = stores("ff1b")
    s.assert_node("u", "L", {"p": 1}, 0, 100)
    scan = NodeScan("u", uids=("u",), sigma_=Sigma.at_instant(10))
    scope = fx.scope_of_node(s, scan)
    assert scope.terms[0].vt == ((10, 11),), "the narrowed Σ is the whole point"
    assert scope.terms[0].carve_reachable, "D13.15: `@recut` is unconditional"

    from tgms.tgir.execute import run_plan
    before = run_plan(scan, s.adapter, tt_source=s)
    assert before["rows"][0]["u.vt_e"] == 100

    s.assert_node("u", "L", {"p": 2}, 50, 60)

    after = run_plan(scan, s.adapter, tt_source=s)
    assert after["rows"][0]["u.vt_e"] == 50
    assert after["result_digest"] != before["result_digest"], "FF-1 B: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "FF-1 B")
    assert fx.arms(verdict) == {"carve"}, fx.describe(verdict)


def test_ff1_mirror_a_correction_below_the_query_instant(stores):
    """**FF-1 mirror** — *the carve arm is direction-agnostic by construction.*

    Correction over `[10,20)`, query at instant **90**. The surviving right
    fragment is `[20,100)`: its `vt_s` moved `0 → 20`, at a valid-time location
    the op's arguments bound from *below* rather than above. `_remainder`
    re-inserts `[max(vs, ce), ve)` with `ve` from the **superseded** version —
    apply-time store state the log record does not carry (D13.7a), which is why
    no interval arithmetic can recover the reach and `vt_carve() = "*"` is the
    only sound answer.
    """
    s = stores("ff1m")
    s.assert_node("u", "L", {"p": 1}, 0, 100)
    scan = NodeScan("u", uids=("u",), sigma_=Sigma.at_instant(90))
    scope = fx.scope_of_node(s, scan)

    from tgms.tgir.execute import run_plan
    before = run_plan(scan, s.adapter, tt_source=s)
    assert before["rows"][0]["u.vt_s"] == 0

    s.assert_node("u", "L", {"p": 2}, 10, 20)

    after = run_plan(scan, s.adapter, tt_source=s)
    assert after["rows"][0]["u.vt_s"] == 20
    assert after["result_digest"] != before["result_digest"], "FF-1 mirror: ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "FF-1 mirror")
    assert fx.arms(verdict) == {"carve"}, fx.describe(verdict)


def test_ff2a_patternmatch_carries_a_nodes_arm(stores):
    """**FF-2 a** — *`PatternMatch`'s narrowed scope must carry `nodes: "*"`.*

    A `PatternMatch` whose node variables are anchored binds node columns
    `(uid, vid, label, vt_s, vt_e, props)`, so a `correct` to a matched node's
    `name` changes a projected value. Such an op emits a **node** footprint, and
    `targets_match` routes node footprints to the `nodes` arm **alone** — an
    absent arm being ∅ (D13.5). A narrowed target naming only `incident` is
    therefore not merely imprecise, it is unsound (L13.2a).

    The trap this walks into is worth restating: `kinds` already contains node
    kinds (`ℰ ∪ 𝒩`), so a reviewer checking the first conjunct sees node ops
    admitted and stops. **The failure is in the second conjunct.**

    Checked on the `PatternMatch` node's **own** leaf scope: the anchoring
    `NodeScan`'s scope would catch this correction for a different reason, and
    unioning it in would hide exactly the hole FF-2 is about.
    """
    s = stores("ff2a")
    s.assert_node("F1", "Person", {"name": "Alice"}, 0, OPEN_END)
    s.assert_node("F2", "Person", {"name": "Bob"}, 0, OPEN_END)
    s.assert_edge("F1", "F2", "LIKES", {}, 0, OPEN_END)

    src = Project(NodeScan("f", uids=("F1", "F2")), (("who", Col("f.uid")),))
    pattern = Pattern((NodePat("x"), NodePat("y")),
                      (EdgePat("e", "x", "y", "LIKES"),))
    pm = PatternMatch(pattern, (Source("x", src, "who"), Source("y", src, "who")))
    proj = Project(pm, (("nm", PropRef("y.props", "name")),))

    own = fx.leaf_scope_of(s, pm)
    (term,) = own.terms
    assert term.targets is not TOP, "the narrowing is taken, so FF-2 applies"
    assert term.targets.nodes is TOP, (
        "L13.2a: an operator that binds node columns and narrows its targets "
        "carries `nodes: \"*\"` — an absent arm is ∅, not ⊤")

    from tgms.tgir.execute import run_plan
    before = run_plan(proj, s.adapter, tt_source=s)
    assert before["rows"] == [{"nm": "Bob"}]

    s.correct(EntityRef(kind="node", uid="F2"), {"name": "Robert"}, 0, OPEN_END)

    after = run_plan(proj, s.adapter, tt_source=s)
    assert after["rows"] == [{"nm": "Robert"}]
    assert after["result_digest"] != before["result_digest"], "FF-2 a: ¬FRESH*"

    verdict = fx.check_scope(s, own)
    fx.assert_stale(verdict, "FF-2 a")
    assert fx.kinds(verdict) == {"correct"}, fx.describe(verdict)


def test_ff2b_expand_exact0_targets_a_nodes_arm_not_an_incidence_arm(stores):
    """**FF-2 b** — *`Expand{exact(0)}`'s target is a `nodes` arm.*

    `exact(0)` traverses **no edge at all**, so an incidence-only term is the
    wrong *shape*, not merely an incomplete one (L13.2a's corollary). It binds
    `into`'s node columns, so `assert_node("B","Bot",…)` — a relabel of a node
    the expansion binds — changes an output row.

    The must-not-fire half is RG-10's exemption, and it is the reason the arm
    is a `nodes` arm rather than both: L13.3 requires an `incident` arm wherever
    `kinds` includes `𝒟`, and `Expand{exact(0)}` drops it explicitly because
    *a core scan has no unknown-uid outcome to flip*. So an edge write incident
    to the seed must **not** fire this term.
    """
    s = stores("ff2b")
    s.assert_node("B", "Node", {}, 0, OPEN_END)
    seed = NodeScan("p", uids=("B",))
    hop = Expand(seed, "p", "n", Exact(0))
    proj = Project(hop, (("lbl", Col("n.label")),))

    own = fx.leaf_scope_of(s, hop)
    (term,) = own.terms
    assert term.targets.nodes == ("B",), "a **nodes** arm (FF-2 b)"
    assert term.targets.incident is None, "and no incidence arm (RG-10)"

    from tgms.tgir.execute import run_plan
    before = run_plan(proj, s.adapter, tt_source=s)
    assert before["rows"] == [{"lbl": "Node"}]

    # the precision half first, on a store state the relabel has not touched:
    # an edge write incident to the seed cannot flip a core scan's outcome
    s.assert_edge("B", "Z", "REL", {}, 0, OPEN_END)
    assert run_plan(proj, s.adapter, tt_source=s)["result_digest"] == \
        before["result_digest"]
    fx.assert_fresh(fx.check_scope(s, own), "FF-2 b: RG-10's exemption")

    s.assert_node("B", "Bot", {}, 0, OPEN_END)

    after = run_plan(proj, s.adapter, tt_source=s)
    assert after["rows"] == [{"lbl": "Bot"}]
    assert after["result_digest"] != before["result_digest"], "FF-2 b: ¬FRESH*"

    verdict = fx.check_scope(s, own)
    fx.assert_stale(verdict, "FF-2 b")
    assert fx.kinds(verdict) == {"assert_node"}, fx.describe(verdict)


def test_ff3_aggregate_events_label_dimension_reads_node_versions(stores):
    """**FF-3** — *the `label` group-by dimension reads node versions; the
    domain was `K = ℰ`.*

    `aggregate_events(group_by=[{dim: "label", role: "dst"}], count, [0,100),
    MSG)` resolves each endpoint's label through `nodes_columnar`, so the answer
    is a function of **node version state**. `assert_node("B","Bot",…)` splits
    `[{Node: 2}]` into `[{Bot: 1}, {Node: 1}]` — row count, values and digest.

    It fails on the **first** conjunct with no `targets` or interval subtlety:
    `assert_node ∉ ℰ`. §9.7's second, node-kinded term
    (`K = 𝒩`, `targets = {nodes: … ∪ "*"}`, `T = "*"`) is the fix, and `T` must
    be `"*"` on it because `intersects` consults `rel_types` only for edge
    footprints (D13.23 property 1).

    Must-not-fire sibling: the same relabel against an **endpoint**-grouped
    call, which reads no node state and must stay `FRESH`.
    """
    s = stores("ff3")
    s.ingest_events([{"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10},
                     {"src": "A", "dst": "C", "rel_type": "MSG", "vt_s": 20}])
    window = {"t_a": 0, "t_b": 100}
    by_label = {"group_by": [{"dim": "label", "role": "dst"}],
                "aggregates": [{"agg": "count"}], "window": window,
                "rel_types": ["MSG"]}
    by_endpoint = {**by_label, "group_by": [{"dim": "endpoint", "role": "dst"}]}

    lab_before = fx.call(s, "aggregate_events", by_label)
    ep_before = fx.call(s, "aggregate_events", by_endpoint)
    assert lab_before["rows"] == [{"dst_label": "Node", "count": 2}]
    lab_scope = DependencyScope.from_json(lab_before["dependency"])
    ep_scope = DependencyScope.from_json(ep_before["dependency"])
    assert len(lab_scope.terms) == 2, "FF-3: the edge term and the node term"
    node_term = lab_scope.terms[1]
    assert node_term.kinds is not TOP and "assert_node" in node_term.kinds
    assert node_term.rel_types is TOP, "§9.7's box: `T` must be `\"*\"` here"

    s.assert_node("B", "Bot", {}, 0, OPEN_END)

    lab_after = fx.call(s, "aggregate_events", by_label)
    assert {r["dst_label"]: r["count"] for r in lab_after["rows"]} == \
        {"Bot": 1, "Node": 1}
    assert lab_after["result_digest"] != lab_before["result_digest"], "FF-3: ¬FRESH*"

    verdict = fx.check_scope(s, lab_scope)
    fx.assert_stale(verdict, "FF-3")
    fx.assert_matched(verdict, "kinds", "FF-3")
    assert [w["matched_term"] for w in fx.witnesses(verdict)] == [1], (
        f"FF-3 is caught by the second, node-kinded term — {fx.describe(verdict)}")

    # the precision half: no label dimension, no node dependency
    assert fx.digest_of(s, "aggregate_events", by_endpoint) == \
        ep_before["result_digest"]
    fx.assert_fresh(fx.check_scope(s, ep_scope), "FF-3 sibling")


def test_ff4_an_above_frontier_pin_is_clamped_and_still_scanned(stores):
    """**FF-4** — *"a pinned result is always `FRESH`" is false above the store
    frontier.*

    `as_of_tt` accepts any value up to `OPEN_END` and is never compared against
    the frontier, and an agent deriving one from a wall clock produces an
    above-frontier pin routinely — `tt` is a **hybrid logical clock** value, so
    the two coordinate systems drift. Such a read absorbs every write with
    `tt <= as_of_tt`, so it is not stable and must never take the pinned
    shortcut.

    The fix is not a conjunct: it is **D1.10's clamp**. `tt_q = min(as_of_tt,
    frontier)`, reported `pinned = false, clamped = true` (D1.9a's ruling —
    `pinned` describes the basis the caller *requested*, not the one served), so
    the batch that lands afterwards is *in* the suffix and is tested normally.
    """
    s = stores("ff4")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    frontier = s.frontier_tt()
    above = frontier + 10 ** 9              # ~16 minutes of hlc ahead
    q = {"uid": "A", "as_of_tt": above}

    before = fx.call(s, "entity_history", q)
    assert before["pinned"] is False and before["clamped"] is True, (
        "D1.9a/RG-4: an above-frontier pin is `pinned = false, clamped = true`")
    assert before["tt_q"] == frontier, "D1.10: round down, never up (D13.17a)"
    scope = DependencyScope.from_json(before["dependency"])
    assert scope.pinned is False and scope.clamped is True

    s.assert_node("A", "L", {"tier": "gold"}, 0, OPEN_END)

    after = fx.call(s, "entity_history", q)
    assert after["result_digest"] != before["result_digest"], (
        "FF-4: ¬FRESH* — the new version has `tt_s <= as_of_tt`, so it is "
        "visible at the requested instant")
    fx.assert_stale(fx.check_scope(s, scope), "FF-4")


def test_ff4_sibling_a_genuine_pin_is_stable_and_still_scanned(stores):
    """**FF-4's other half, and T1's boundary.**

    A pin at or below the frontier *is* stable (T1, Corollary C1 as amended), so
    its recompute is byte-identical. The check still walks the suffix and
    returns `POSSIBLY_STALE` — a **false invalidation**, permitted by D1.13 and
    counted against precision by D6.2.

    That is deliberate: *"No step is skippable on the strength of
    `pinned = true`"* (D13.24). A genuinely pinned scope scans a suffix that is
    empty **because the log says so**, not because a flag said to skip the scan
    — and the shortcut Corollary C1 used to license is what FF-4 exploited.
    """
    s = stores("ff4b")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    frontier = s.frontier_tt()
    q = {"uid": "A", "as_of_tt": frontier}

    before = fx.call(s, "entity_history", q)
    assert before["pinned"] is True and before["clamped"] is False
    scope = DependencyScope.from_json(before["dependency"])

    s.assert_node("A", "L", {"tier": "gold"}, 0, OPEN_END)

    assert fx.digest_of(s, "entity_history", q) == before["result_digest"], (
        "T1: bi-temporal immutability at or below the frontier")
    verdict = fx.check_scope(s, scope)
    assert not fx.is_fresh(verdict), (
        "the suffix is scanned regardless of `pinned` (D13.24) — "
        f"{fx.describe(verdict)}")


def test_ff5_role_either_yields_a_top_anchor_so_the_narrowing_is_refused(stores):
    """**FF-5** — *`L13.1`'s anchor for `role: "either"`.*

    `EdgeScan(endpoints={either, ["P"]})` selects an edge when `src ∈ U` **or**
    `dst ∈ U`, so **neither** column is contained in `U`. The C1/C2 illustration
    named `dst` as the ⊤ column, which implies `src` is anchored — and under
    that reading `Expand{exact(1)}` from `src` narrows to
    `{incident: {src, ["P"]}}` and a `LIKES` edge from a *neighbour* escapes it.

    The fix is stronger than patching the term: with `either → src ⊤, dst ⊤`,
    L13.2's precondition `S = anchor(input, from) ≠ ⊤` fails, the narrowing is
    **never taken**, and there is nothing left to escape.

    The must-not-fire half is the contrast that shows the ⊤ is not laziness:
    under `role: "src"` the anchor *is* concrete and the narrowing *is* taken.
    """
    s = stores("ff5")
    for uid in ("P", "Q", "R"):
        s.assert_node(uid, "Person", {}, 0, OPEN_END)
    s.assert_edge("Q", "P", "KNOWS", {}, 0, OPEN_END)      # note: Q → P

    either = EdgeScan("e", rel_types=("KNOWS",),
                      endpoints=Endpoints("either", ("P",)))
    hop = Expand(either, "e.src", "x", Exact(1), rel_type="LIKES", dir="out")
    assert anchor_of_var(either, "e.src") is TOP, "L13.1 (FF-5)"
    assert fx.leaf_scope_of(s, hop).terms[0].targets is TOP, (
        "L13.2's precondition fails, so `Expand`'s targets stay `\"*\"`")

    scope = fx.scope_of_node(s, hop)
    from tgms.tgir.execute import run_plan
    before = run_plan(hop, s.adapter, tt_source=s)
    assert before["rows"] == []

    s.assert_edge("Q", "R", "LIKES", {}, 0, OPEN_END)

    after = run_plan(hop, s.adapter, tt_source=s)
    assert len(after["rows"]) == 1
    assert after["result_digest"] != before["result_digest"], "FF-5: ¬FRESH*"
    fx.assert_stale(fx.check_scope(s, scope), "FF-5")

    # the contrast: a *named* role does anchor its own column, and L13.2 fires
    by_src = EdgeScan("e", rel_types=("KNOWS",), endpoints=Endpoints("src", ("P",)))
    narrowed = Expand(by_src, "e.src", "x", Exact(1), rel_type="LIKES", dir="out")
    assert anchor_of_var(by_src, "e.src") == frozenset({"P"})
    incident = fx.leaf_scope_of(s, narrowed).terms[0].targets.incident
    assert (incident.role, incident.uids) == ("src", ("P",)), "L13.2's edge arm"


def test_ff6_the_dense_id_case_needs_an_incident_arm(stores):
    """**FF-6** — *the `𝒟` case is in `kinds` but was unreachable through
    `targets`.*

    `entity_history(uid="A")` on an unknown uid raises `E_NOT_FOUND`. Per §5's
    prohibition 3 the failed step still carries its scope — *a correction can
    make it succeed*. `assert_edge("A","Z","REL",…)` calls `ensure_entities`,
    registering a dense id for `A` **without writing a node version**, and the
    outcome flips from error to an empty result: §1.6's `ERRORED` class, *"an
    outcome change between 'error' and 'result' is a change."*

    `𝒟 = {assert_edge, ingest_events}` both write **edge** footprints, which
    `targets_match` routes to the `edges`/`incident` arms only. A term naming a
    `nodes` arm alone admits `𝒟` in its first conjunct and can never satisfy its
    second: the presence of `𝒟` in `kinds` was **inert**. L13.3's rule —
    `{nodes: U, incident: {either, U}}` wherever `kinds` includes `𝒟` — is what
    makes conjunct 2 pass, and §13.6's `T1c` is exactly that shape.
    """
    s = stores("ff6")
    s.assert_node("Z", "Node", {}, 0, OPEN_END)
    q = {"uid": "A"}

    with pytest.raises(NotFoundError):
        fx.call(s, "entity_history", q)

    # a step that failed still contributes its scope (D13.14 prohibition 3),
    # derived from the args it attempted
    from tgms.temporal.algebra import validate_args
    from tgms.tgir.ttq import dependency_of
    scope = dependency_of("entity_history", fx.basis_for(s),
                          validate_args("entity_history", dict(q)))
    dense_terms = [t for t in scope.terms
                   if t.kinds is not TOP and set(t.kinds) == {"assert_edge",
                                                              "ingest_events"}]
    assert dense_terms, "L13.3: the `𝒟` term"
    assert dense_terms[0].targets.incident.uids == ("A",), (
        "L13.3: `targets` must carry an `incident` arm over the same uids")

    s.assert_edge("A", "Z", "REL", {}, 0, OPEN_END)

    after = fx.call(s, "entity_history", q)
    assert after["rows"] == [] and after["rows_total"] == 0, (
        "FF-6: ¬FRESH* — the outcome changed from E_NOT_FOUND to a result")

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "FF-6")
    fx.assert_matched(verdict, "targets.incident", "FF-6")

    # the precision half the three-term split buys: an edge *correction* cannot
    # reach a result that reads no edges — the `𝒟` term admits only @identity
    scope2 = fx.scope_of_call(s, "entity_history", q)
    before2 = fx.digest_of(s, "entity_history", q)
    s.correct(EntityRef(kind="edge", src="A", dst="Z", rel_type="REL", disc=""),
              {"w": 1}, 0, 10)
    assert fx.digest_of(s, "entity_history", q) == before2
    fx.assert_fresh(fx.check_scope(s, scope2), "FF-6 sibling")


def test_ff7a_union_moves_the_whole_triple_with_the_smaller_offset(stores):
    """**FF-7 a** — *`⊎` must not pair one operand's `tt_q` with the other's
    cursor.*

    Two reads of one operator straddling a write. The second read's own scope is
    legitimately `FRESH` about that write — it is *in* its result. A plan
    containing both steps is not: `s1` ran before it.

    D13.8's rule is that `(tt_q, pinned, clamped)` move **as a unit**, taken
    from whichever operand has the smaller minimum checkpoint offset, and never
    component-wise. Taking `min` of the `tt_q`s while keeping the other
    operand's offset breaks D13.8a's cursor invariant — *"batches lying in the
    tt-suffix but before the retained offset are never scanned, never tested,
    and the verdict is `FRESH` on a changed result."* D13.8b keeps **every**
    checkpoint so the union does not shrink D13.18's tamper-evidence either.
    """
    s = stores("ff7a")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    q = {"uid": "A"}

    first = fx.call(s, "entity_history", q)
    scope1 = DependencyScope.from_json(first["dependency"])

    s.assert_node("A", "L", {"p": 2}, 0, OPEN_END)          # the intervening batch

    second = fx.call(s, "entity_history", q)
    scope2 = DependencyScope.from_json(second["dependency"])
    assert second["result_digest"] != first["result_digest"], "FF-7 a: ¬FRESH*"
    assert scope1.tt_q < scope2.tt_q

    union = scope1.union(scope2)
    assert union.tt_q == scope1.tt_q, "D13.8: the earliest basis wins"
    assert union.min_offset == min(scope1.min_offset, scope2.min_offset)
    assert len(union.checkpoints) == 2, "D13.8b: checkpoints concatenate"
    assert len(union.terms) == len(scope1.terms) + len(scope2.terms)

    fx.assert_fresh(fx.check_scope(s, scope2), "FF-7 a: the later step alone")
    fx.assert_stale(fx.check_scope(s, scope1), "FF-7 a: the earlier step")
    fx.assert_stale(fx.check_scope(s, union), "FF-7 a: the union")


def test_ff7b_a_pinned_basis_paired_with_a_read_time_cursor(stores):
    """**FF-7 b** — *a pinned step's past `tt_q` paired with a read-time cursor
    violates the invariant by construction.*

    The cursor invariant (D13.8a): *the minimum offset in `scope.checkpoints` is
    at or before the offset of the first batch with `tt > scope.tt_q`.* A pin
    into the past, checkpointed at the read, breaks it — and every batch between
    the pinned instant and the read would be skipped.

    Two dispositions are permitted and both are sound: `UNDECIDABLE(
    "cursor-invariant")`, or the sanctioned widening `start := 0` — *"a full-log
    scan, which is widening and therefore sound. Slow, never wrong."* What is
    **not** permitted is `FRESH`. D1.10's clamp removes the pairing at source
    for the above-frontier case, which is why FF-4 and FF-7 b are one fix.
    """
    s = stores("ff7b")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    pinned_at = s.frontier_tt()

    s.assert_node("A", "L", {"p": 2}, 0, OPEN_END)
    s.assert_node("A", "L", {"p": 3}, 0, OPEN_END)

    # the read happens now, so the cursor is at the head while `tt_q` is in the
    # past — exactly FF-7 b's pairing
    live = fx.call(s, "entity_history", {"uid": "A", "as_of_tt": pinned_at})
    scope = DependencyScope.from_json(live["dependency"])
    assert scope.pinned is True and scope.tt_q == pinned_at

    verdict = fx.check_scope(s, scope)
    assert not fx.is_fresh(verdict), (
        "FF-7 b: a past `tt_q` with a read-time cursor must never be FRESH — "
        f"{fx.describe(verdict)}")


def test_ff7c_a_plan_basis_is_the_union_of_its_steps_never_a_completion_capture(
        stores):
    """**FF-7 c** — *a plan record stamped at completion excludes batches that
    landed during execution.*

    D13.17b: a plan record's `(tt_q, pinned, clamped)` and `checkpoints` are the
    **`⊎` of its steps'** — hence the earliest — and are **never** values
    captured when the plan finishes. Stamping at completion would exclude every
    batch that landed during execution while those batches are absent from the
    early steps' results: *"exactly the false freshness D13.17 exists to forbid,
    reintroduced one level up."*

    The counterfactual is computed explicitly here, because the difference
    between the two rules is the whole finding: the same terms under a
    completion-time `tt_q` return `FRESH`.
    """
    from tgms.agent.executor import Trace

    s = stores("ff7c")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    q = {"uid": "A"}

    step1 = fx.call(s, "entity_history", q)
    s.assert_node("A", "L", {"p": 2}, 0, OPEN_END)          # lands mid-plan
    step2 = fx.call(s, "entity_history", q)

    trace = Trace(plan_id="ff7c", steps=[
        {"step_id": "s1", "op": "entity_history", "status": "ok",
         "dependency": step1["dependency"], "tt_q": step1["tt_q"]},
        {"step_id": "s2", "op": "entity_history", "status": "ok",
         "dependency": step2["dependency"], "tt_q": step2["tt_q"]},
    ])
    basis = trace.plan_basis()
    assert basis["tt_q"] == step1["tt_q"], "D13.17b: the earliest, by `⊎`"

    plan_scope = DependencyScope.from_json(basis["dependency"])
    fx.assert_stale(fx.check_scope(s, plan_scope), "FF-7 c")

    completion_capture = replace(plan_scope, tt_q=s.frontier_tt(),
                                 checkpoints=(Checkpoint(
                                     plan_scope.min_offset,
                                     plan_scope.checkpoints[0].chain),))
    assert fx.is_fresh(fx.check_scope(s, completion_capture)), (
        "the counterfactual this rule forbids: a completion-time stamp reports "
        "FRESH on a plan whose first step read older beliefs")


def test_ff8_role_both_has_an_encoding_and_a_conjunctive_test(stores):
    """**FF-8** — *`role: "both"` had no encoding and `incident_match` failed
    silently on it.*

    `both` is a real scan mode (`edges_columnar(touching_both=True)`), and an
    implementer who encoded it faithfully produced a term that matched nothing.
    *"This is the one place in the contract where a **narrowing** is reachable
    by accident: D13.1 promises that every approximation widens, but writing
    down a role the checker does not recognize narrows to ∅."*

    Two halves: `both` now has the genuinely narrower conjunctive test
    (`meets(uids, src) ∧ meets(uids, dst)`), and — D13.23a, which closes the
    class rather than the instance — **any** unrecognized enum value makes the
    reader return `UNDECIDABLE`, never a non-match.
    """
    s = stores("ff8")
    for uid in ("P", "Q", "R"):
        s.assert_node(uid, "Person", {}, 0, OPEN_END)
    term = ScopeTerm(kinds=K_EDGE,
                     targets=Targets(incident=Incident("both", ("P", "Q"))),
                     rel_types=TOP, vt=TOP, props=TOP)
    scope = fx.scope_from_terms(s, term)

    s.assert_edge("P", "R", "KNOWS", {}, 0, OPEN_END)       # only one endpoint
    fx.assert_fresh(fx.check_scope(s, scope), "FF-8: `both` is conjunctive")

    s.assert_edge("P", "Q", "KNOWS", {}, 0, OPEN_END)       # both endpoints
    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "FF-8")
    fx.assert_matched(verdict, "targets.incident", "FF-8")

    # D13.23a's totality. The wire object refuses an unknown role at
    # construction — the first line of defence, and why this path is reachable
    # only for a scope deserialized from a *future* version (D13.9). Forced
    # here, because "unreachable" is what FF-8 was.
    with pytest.raises(InvalidArgError):
        Incident("hither", ("P",))
    rogue = Incident("either", ("P",))
    object.__setattr__(rogue, "role", "hither")
    rogue_scope = fx.scope_from_terms(s, replace(
        term, targets=Targets(incident=rogue)))
    forced = fx.check_scope(s, rogue_scope)
    assert not fx.is_fresh(forced), (
        "D13.23a: an unrecognized enum is UNDECIDABLE, never a non-match — "
        f"{fx.describe(forced)}")
    assert fx.verdict_name(forced) == "UNDECIDABLE", fx.describe(forced)


def test_ff9_co_active_wired_window_closes_at_v_b_plus_gap_plus_one(stores):
    """**FF-9** — *the wired `co_active` window is off by one, and unsound
    unless the `a` side is selected by containment.*

    `before(gap)` selects `b` intervals starting in `(a.vt_e, a.vt_e + gap]` —
    **closed at the right** — so a `b` interval starting exactly at
    `v_b + gap` participates, and `vt_overlaps` needs `b_s < a_e` to fire.
    `[[v_a, v_b + gap)]]` misses it by one microsecond; `+ gap + 1` closes it.
    And under `overlap` selection an `a` interval may end arbitrarily far past
    `v_b`, so the bound is unsound by an unbounded margin: **`vt = "*"`** is the
    only sound scope there.

    `co_active`'s `window` parameter is not wired (§9.10's ruling reserves it),
    so the live scope is `vt: "*"` — which is the sound answer for the unwired
    case and is asserted as such. The wired scopes are written out from §9.10 so
    that the arithmetic the ruling fixes is the thing under test.
    """
    s = stores("ff9")
    for uid in ("A", "B", "C", "D"):
        s.assert_node(uid, "N", {}, 0, OPEN_END)
    s.assert_edge("A", "B", "R", {}, 0, 100)                 # the `a` side
    q = {"a_spec": {"rel_type": "R"}, "b_spec": {"rel_type": "S"},
         "allen_relation": {"relation": "before", "gap": 10}}

    before = fx.call(s, "co_active", q)
    assert before["rows"] == []
    live = DependencyScope.from_json(before["dependency"])
    assert live.terms[0].vt is TOP, "§9.10: unwired ⇒ `vt: \"*\"`"

    v_a, v_b, gap = 0, 100, 10
    wired = fx.scope_from_terms(s, ScopeTerm(
        kinds=K_EDGE, targets=TOP, rel_types=TOP,
        vt=((v_a, v_b + gap + 1),), vt_mode="overlap", props=TOP))
    withdrawn = fx.scope_from_terms(s, ScopeTerm(
        kinds=K_EDGE, targets=TOP, rel_types=TOP,
        vt=((v_a, v_b + gap),), vt_mode="overlap", props=TOP))

    # a `b` interval starting exactly at `v_b + gap`: `b.vt_s - a.vt_e = gap`,
    # which `before(gap)` admits (`0 < b.vt_s - a.vt_e <= gap`)
    s.assert_edge("C", "D", "S", {}, v_b + gap, v_b + gap + 10)

    after = fx.call(s, "co_active", q)
    assert len(after["rows"]) == 1 and after["rows"][0]["b"]["vt_s"] == v_b + gap
    assert after["result_digest"] != before["result_digest"], "FF-9: ¬FRESH*"

    verdict = fx.check_scope(s, wired)
    fx.assert_stale(verdict, "FF-9")
    assert "value" in fx.arms(verdict), (
        "FF-9 is conjunct **4** on the value arm: `vt_closed(110,120)` starts "
        f"at 110 and `[0, 111)` admits it — {fx.describe(verdict)}")
    fx.assert_matched(verdict, "vt", "FF-9")

    # The contrast is on the **value** arm alone, because `co_active` is
    # carve-reachable (§9.10: `P = ⊤`, since Allen relations compare whole
    # intervals) and its carve arm fires on any in-scope identity whatever the
    # window says. That is exactly why FF-9's arithmetic only starts to matter
    # once the window is wired — and why the finding names conjunct 4.
    missed = fx.check_scope(s, withdrawn)
    assert "value" not in fx.arms(missed), (
        "the C1/C2 form `[[v_a, v_b + gap)]]` is the microsecond of false "
        f"freshness `+ gap + 1` exists to close — {fx.describe(missed)}")

    fx.assert_stale(fx.check_scope(s, live), "FF-9: the unwired scope")


def test_rg1_a_duration_aggregate_is_carve_reachable(stores):
    """**RG-1** — *an operator whose output exposes no version metadata yet
    whose value changes under a pure re-cut.*

    An edge believed `[0,100)` gives `max_duration = 100` over `window =
    [0,20)`. A `correct` over `[50,60)` — **outside the window** — leaves
    `[0,50)` as the only in-window event and the answer becomes 50. The value
    arm's `vt = [50,61)` misses `[0,20)`, and `Pᵥ` excludes the carve arm by
    construction, so the C1/C2 text answered `FRESH`.

    `duration = vt_e − vt_s` is a function of **both** interval endpoints, and
    the right fragment's `vt_e` is the original `ve` — outside the op's interval
    by an unbounded margin. So the term carries `P = Pᵥ ∪ {@recut}` and the call
    keeps `K`, `I`, `T` and loses `V`: conjunct **5** fires on the carve arm.

    The must-not-fire sibling is the same store, the same correction, a
    `count` aggregate: `of: "vt_s"` and the sequence aggregates are bounded by
    the value arm (A.3), so their windows survive. *One store, one correction,
    two verdicts.*
    """
    s = stores("rg1")
    s.assert_node("A", "Node", {}, 0, OPEN_END)
    s.assert_node("X", "Node", {}, 0, OPEN_END)
    s.assert_edge("A", "X", "ROLE", {"level": 1}, 0, 100)
    window = {"t_a": 0, "t_b": 20}
    duration = {"group_by": [], "aggregates": [{"agg": "max", "of": "duration"}],
                "window": window, "rel_types": ["ROLE"]}
    counting = {"group_by": [], "aggregates": [{"agg": "count"}],
                "window": window, "rel_types": ["ROLE"]}

    dur_before = fx.call(s, "aggregate_events", duration)
    cnt_before = fx.call(s, "aggregate_events", counting)
    assert dur_before["rows"] == [{"max_duration": 100}]
    dur_scope = DependencyScope.from_json(dur_before["dependency"])
    cnt_scope = DependencyScope.from_json(cnt_before["dependency"])
    assert "@recut" in dur_scope.terms[0].props, "§9.7's `duration` exception"
    assert "@recut" not in cnt_scope.terms[0].props

    s.correct(EntityRef(kind="edge", src="A", dst="X", rel_type="ROLE", disc=""),
              {"level": 2}, 50, 60)

    dur_after = fx.call(s, "aggregate_events", duration)
    assert dur_after["rows"] == [{"max_duration": 50}], "RG-1: ¬FRESH*"
    assert dur_after["result_digest"] != dur_before["result_digest"]

    verdict = fx.check_scope(s, dur_scope)
    fx.assert_stale(verdict, "RG-1")
    assert fx.arms(verdict) == {"carve"}, (
        "RG-1 is caught by the carve arm on conjunct 5 — the value arm's "
        f"`vt` misses the window — {fx.describe(verdict)}")
    fx.assert_matched(verdict, "props", "RG-1")
    assert "vt" not in fx.matched_on(verdict), (
        "`vt = \"*\"` on the carve arm is the absence of a narrowing, not "
        f"attribution (E-3) — {fx.describe(verdict)}")

    assert fx.digest_of(s, "aggregate_events", counting) == \
        cnt_before["result_digest"], "the count is refinement-invariant here"
    fx.assert_fresh(fx.check_scope(s, cnt_scope), "RG-1 sibling")


# ===========================================================================
# the two shape regressions, which have no scenario of their own
# ===========================================================================

def test_co8_meets_is_set_vs_set_against_a_coarsened_ingest_footprint(stores):
    """**CO-8** — *`meets` is set-vs-set; there is no scalar membership
    operator.*

    `ingest_events`' edge arm coarsens `identity.src`, `identity.dst` and
    `rel_type` to the **sets** its events name (D13.22). A scalar-typed test
    reads `["C"] ∈ ["X","B","C"]`, returns false, and false here is a **false
    negative and therefore unsound**.

    Exercised on a batch whose `src` set is `{X, B, C}` against a scope whose
    cohort is `["C"]`: only a set-vs-set intersection fires.
    """
    s = stores("co8")
    for uid in ("X", "B", "C", "Z"):
        s.assert_node(uid, "N", {}, 0, OPEN_END)
    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["MSG"],
         "endpoint_filter": {"role": "src", "uids": ["C"]}}

    before = fx.call(s, "aggregate_events", q)
    scope = DependencyScope.from_json(before["dependency"])

    s.ingest_events([{"src": src, "dst": "Z", "rel_type": "MSG", "vt_s": t}
                     for src, t in (("X", 40), ("B", 41), ("C", 42))])

    assert fx.digest_of(s, "aggregate_events", q) != before["result_digest"]
    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "CO-8")
    fx.assert_matched(verdict, "targets.incident", "CO-8")


def test_co3_class_is_witness_metadata_and_never_a_conjunct(stores):
    """**CO-3** — *the effect `class` is not derivable from the log record
    alone.*

    A-vs-B is decided at apply time by `believed_node_versions(uid)`, which is
    store state, so the wire value for an assert is the literal `"A|B"`; `A` for
    `ingest_events`, `C` for `correct`, `D` for `retract`. **`class` is not a
    conjunct of `intersects`** — it is witness metadata only, which is why the
    finding is not a soundness item and why M4's primary disaggregation is by
    `kind` and `arm`, both of which *are* log-derivable (D13.27).
    """
    s = stores("co3")
    s.assert_node("A", "N", {"p": 1}, 0, 100)
    s.assert_edge("A", "B", "R", {}, 0, 100)
    scope = fx.scope_from_terms(s, ScopeTerm())        # all-`"*"`: catch everything

    s.assert_node("A", "N", {"p": 2}, 0, 100)
    s.correct(EntityRef(kind="node", uid="A"), {"p": 3}, 10, 20)
    s.retract(EntityRef(kind="edge", src="A", dst="B", rel_type="R", disc=""), 50)
    s.ingest_events([{"src": "A", "dst": "C", "rel_type": "MSG", "vt_s": 10}])

    verdict = fx.check_scope(s, scope)
    ws = fx.assert_stale(verdict, "CO-3")
    by_kind = {w["kind"]: w.get("class") for w in ws}
    assert by_kind.get("assert_node") == "A|B", (
        "CO-3: A-vs-B is not log-derivable, so the wire value is the literal")
    assert by_kind.get("correct") == "C"
    assert by_kind.get("retract") == "D"
    assert by_kind.get("ingest_events") == "A"
    assert "class" not in fx.matched_on(verdict), (
        "`class` must never become a conjunct of `intersects`")


# ===========================================================================
# §15 — the errata register, as of 2026-08-22
# ===========================================================================

def test_e1_an_explicit_disc_ingest_writes_an_existing_eid(stores):
    """**E-1** (§15, BLOCKING; amends **D13.22**). *`ingest_events` with an
    explicit `disc` writes an existing `eid`, which the coarsened edge arm
    cannot match.*

    `_ingest_events` takes `disc = ev.get("disc", f"#{offset + i}")`, so an
    event carrying an **explicit** `disc` produces `edge_eid(src, dst,
    rel_type, disc)` for an identity that **already exists** — D2.1's *"every
    ingested event is its own logical edge"* holds only for the *default*
    discriminator. D13.22's edge arm coarsens `identity` to `{src, dst,
    rel_type}` sets and carried **no `eid`**, so `targets_match` tested
    `meets(T.edges, fp.identity.eid)` against an absent field, which D13.5
    makes ∅: a term whose `targets` names only an `edges` arm **could not
    match**.

    *"Latent, not live, and that is not a defence"* — no shipped derivation is
    eid-narrow today, so the term under test is written from D13.3's wire
    format. The ruled fix emits `identity.eid` as the **set** of `edge_eid(...)`
    over the batch's events, which the same erratum verifies is derivable from
    the log alone (`offset` is a logged field and `i` is the event's index), so
    D13.20 is not weakened — with the caveat it records: the checker needs the
    log *and the identity rule*.

    The `nodes` arm is deliberately absent from the term, so the
    `ingest_events` **node** arm cannot rescue the match: the `edges` arm is
    the only path, which is exactly the isolation E-1 is about.
    """
    s = stores("e1")
    s.assert_node("A", "N", {}, 0, OPEN_END)
    s.assert_node("B", "N", {}, 0, OPEN_END)
    s.assert_edge("A", "B", "R", {}, 10, 11, disc="d1")
    eid = edge_eid("A", "B", "R", "d1")

    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["R"]}
    before = fx.call(s, "aggregate_events", q)
    assert before["rows"] == [{"count": 1}]
    eid_narrow = fx.scope_from_terms(s, ScopeTerm(
        kinds=K_EDGE, targets=Targets(edges=(eid,)), rel_types=("R",),
        vt=((0, 100),), vt_mode="event", props=TOP))

    s.ingest_events([{"src": "A", "dst": "B", "rel_type": "R", "disc": "d1",
                      "vt_s": 20}])

    after = fx.call(s, "aggregate_events", q)
    assert after["rows"] == [{"count": 2}], "E-1: ¬FRESH* — a second version"
    versions = fx.call(s, "version_history",
                       {"kind": "edge", "window": {"t_a": 0, "t_b": 100},
                        "belief": "all"})["rows"]
    assert {r["eid"] for r in versions} == {eid}, (
        "E-1's premise: the explicit `disc` wrote **the same logical edge**")

    verdict = fx.check_scope(s, eid_narrow)
    fx.assert_stale(verdict, "E-1")
    fx.assert_matched(verdict, "targets.edges", "E-1")


def test_e4a_a_superseded_belief_scan_depends_on_its_carve_props(stores):
    """**E-4 (a)** (§15; amends **D13.15**). *A scan's belief-mode soundness is
    a named dependency, not an accident.*

    `EdgeScan(belief="superseded") @ Σ = (instant [10,11))` returns nothing
    while the edge's only version is current. `assert_edge(…, 50, 60)` carves
    it: the original `[0,100)` is superseded and now **is** returned at instant
    10. The op's own footprint is `[50,61)`, which misses the instant entirely,
    so the value arm cannot see it.

    D9.0c's fifth sufficient condition — *"exposes a version's `tt_s`/`tt_e`, or
    classifies versions as current/superseded"* — makes such a scan
    carve-reachable **by rule**, and the carve arm is what catches it. The
    erratum's standing obligation is asserted here as the property a future
    narrowing must not break:

    > **A scan's belief-mode soundness depends on its `P` naming
    > `@recut`/`@version`.** Any future narrowing of a scan's `props` must
    > re-derive the belief axis first, under D9.0c, before it may drop
    > `@recut`.
    """
    s = stores("e4a")
    s.assert_node("A", "N", {}, 0, OPEN_END)
    s.assert_node("B", "N", {}, 0, OPEN_END)
    s.assert_edge("A", "B", "R", {"w": 1}, 0, 100)

    scan = EdgeScan("e", belief="superseded", sigma_=Sigma.at_instant(10))
    scope = fx.scope_of_node(s, scan)
    assert scope.terms[0].carve_reachable, (
        "E-4 (a): a `belief=superseded` scan must name `@recut`/`@version`")

    from tgms.tgir.execute import run_plan
    before = run_plan(scan, s.adapter, tt_source=s)
    assert before["rows"] == []

    s.assert_edge("A", "B", "R", {"w": 2}, 50, 60)

    after = run_plan(scan, s.adapter, tt_source=s)
    assert len(after["rows"]) == 1, "the original version is now superseded"
    assert after["result_digest"] != before["result_digest"], "E-4 (a): ¬FRESH*"

    verdict = fx.check_scope(s, scope)
    fx.assert_stale(verdict, "E-4 (a)")
    assert fx.arms(verdict) == {"carve"}, (
        "the value arm's `[50,61)` misses the instant `[10,11)` — "
        f"{fx.describe(verdict)}")


@pytest.mark.xfail(reason="E-4 (b) is drafted for the next re-gate, not yet "
                          "normative in §13; recorded so the rule is armed",
                   strict=False)
def test_e4b_an_edgekey_field_the_footprint_omits_is_a_wildcard(stores):
    """**E-4 (b)** (§15; amends **D13.23**). *The `edges` arm's `EdgeKey` form
    has no intersection rule.*

    D13.3 admits `edges: [{"src":?, "dst":?, "rel_type":?, "disc":?}, …]`, but
    `targets_match` tests only the eid-string form. The drafted rule has three
    branches, and the middle one is the one that matters here:

    > **A field the *footprint* does not carry is treated as `"*"`**, i.e. it
    > matches. `ingest_events`' edge arm coarsens away `disc` (D13.22), so an
    > `EdgeKey` naming `disc` would meet a footprint that has none. Returning
    > `false` there would be a **false negative**; treating unknown as `"*"` is
    > the widening direction (D13.1).

    Marked `xfail(strict=False)` because the erratum explicitly drafts this
    *for the next re-gate* rather than ruling it into §13: a pass is the rule
    already honoured, a fail is a spec-completion item and not an M4.3 gate
    failure. The next re-gate that adopts it should flip this to `strict`.
    """
    s = stores("e4b")
    s.assert_node("A", "N", {}, 0, OPEN_END)
    s.assert_node("B", "N", {}, 0, OPEN_END)
    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["R"]}
    before = fx.call(s, "aggregate_events", q)
    key_scope = fx.scope_from_terms(s, ScopeTerm(
        kinds=K_EDGE,
        targets=Targets(edges=(EdgeKey(src="A", dst="B", rel_type="R",
                                       disc="d1"),)),
        rel_types=("R",), vt=((0, 100),), vt_mode="event", props=TOP))

    s.ingest_events([{"src": "A", "dst": "B", "rel_type": "R", "disc": "d1",
                      "vt_s": 20}])

    assert fx.digest_of(s, "aggregate_events", q) != before["result_digest"]
    fx.assert_stale(fx.check_scope(s, key_scope), "E-4 (b)")


# ===========================================================================
# §13.6 — the worked example, in full, and its variant
# ===========================================================================

def _worked_example_plan():
    """§13.6's three-step plan, verbatim."""
    from tgms.agent.ir import Plan

    return Plan.from_json({
        "plan_id": "s13-6",
        "steps": [
            {"id": "s1", "op": "entity_history",
             "args": {"uid": "A", "include_edges": True}, "depends_on": []},
            {"id": "s2", "op": "aggregate_events",
             "args": {"group_by": [{"dim": "endpoint", "role": "src"}],
                      "aggregates": [{"agg": "count"}],
                      "window": {"t_a": 0, "t_b": 100}, "rel_types": ["MSG"],
                      "endpoint_filter": {"role": "src",
                                          "uids": {"$ref": "s1.edges[*].dst"}}},
             "depends_on": ["s1"]},
            {"id": "s3", "op": "compute",
             "args": {"fn": "topk", "input": {"$ref": "s2.rows"},
                      "field": "count", "k": 1},
             "depends_on": ["s2"]},
        ],
        "answer_spec": {"kind": "entity_set", "from": "s3.rows"},
    })


def _executor(store):
    from tgms.agent.executor import Executor
    from tgms.tools.server import ToolRouter

    return Executor(ToolRouter(store.adapter, tt_source=store))


def _run_worked_example(store):
    """Build `S₀`, run §13.6's plan, apply its two-op batch, and return
    `(record, before, after)`."""
    fx.build_s0(store)
    ex = _executor(store)

    before = ex.run(_worked_example_plan())
    assert before.ok and before.answer == [{"src": "B", "count": 1}]
    record = before.to_json()

    # one batch, two ops — the batch structure is load-bearing for the witness
    # list (one `batch_id`, `op_seq` 0 and 1), and no public write method emits
    # two op kinds in one batch
    store._write([
        make_op("assert_node", uid="A", label="Node", props={"tier": "gold"},
                vt_s=0, vt_e=OPEN_END, source="ingest", provenance_ref=None),
        make_op("ingest_events", offset=0, node_label="Node",
                events=[{"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 45},
                        {"src": "C", "dst": "D", "rel_type": "MSG", "vt_s": 46}],
                source="ingest", provenance_ref=None),
    ])

    after = ex.run(_worked_example_plan())
    return record, before, after


#: `check_trace` is M4.4's deliverable (plan §3.6b, §6's phase table), authored
#: in parallel with this file. The worked example's *verdict and witness list*
#: are asserted here against the merged plan scope, which D13.8's `⊎` makes the
#: sanctioned fallback for a record carrying only `plan_basis`; the per-step
#: `step_id` attribution needs the trace surface and arms itself when it lands.
_HAS_CHECK_TRACE = hasattr(__import__("tgms.tgir.check", fromlist=["check"]),
                           "check_trace")
requires_check_trace = pytest.mark.skipif(
    not _HAS_CHECK_TRACE,
    reason="check_trace (M4 plan §3.6b) is not implemented yet")


def test_the_worked_example_of_13_6(stores):
    """**§13.6** — the worked example, as the integration test.

    `S₀`, `tt_q = tt₃`, the three-step plan, and **one batch with two ops** —
    deliberately one of each of the two effect classes a row-touch rule handles
    worst:

    ```
    op0  assert_node("A", "Node", {"tier":"gold"}, 0, OPEN_END)   [Class B]
    op1  ingest_events([C→D @45, C→D @46])                        [Class A]
    ```

    Four footprints (op0 is carve-capable and emits two; `ingest_events`
    supersedes nothing and emits two value arms and no carve arm), an
    eight-row intersection table, and **two witnesses after dedup** — the
    redundant `fp0c → T1a` match is deduplicated per `(step, op)`.

    Ground truth by recomputation: `s1` changed (A's version is now `[0,∞)`
    with different props and a different `vid`), `s1.edges` did **not**, so
    `s2`'s bound args are still `["X","B","C"]`; `s2` changed, and `s3`'s
    answer flips **`B` → `C`**. *True positive at every granularity, and the
    per-step attribution is exact.*

    What the naive row-touch rule would have said (D6.4): `op1` modifies no
    existing version row and `s2`'s returned rows contain no row for `C` at
    all — *"one false-fresh event from a two-op batch on a five-node store."*
    """
    s = stores("w136")
    record, before, after = _run_worked_example(s)
    plan_scope = DependencyScope.from_json(record["dependency"])
    assert len(plan_scope.terms) == 4, "T1a, the 𝒟 term, T1b, T2 (§13.6)"

    assert after.answer == [{"src": "C", "count": 2}], "§13.6: the answer flips"
    old = {st["step_id"]: st["result_digest"] for st in before.steps}
    new = {st["step_id"]: st["result_digest"] for st in after.steps}
    assert new != old, "§13.6: ¬FRESH* at the plan level"
    assert new["s1"] != old["s1"] and new["s2"] != old["s2"]

    verdict = fx.check_scope(s, plan_scope)
    ws = fx.assert_stale(verdict, "§13.6")
    assert len(ws) == 2, (
        "two witnesses after dedup per op — the redundant `fp0c → T1a` match "
        f"is not a third — {fx.describe(verdict)}")
    by_kind = {w["kind"]: w for w in ws}
    assert set(by_kind) == {"assert_node", "ingest_events"}, fx.describe(verdict)

    w1, w2 = by_kind["assert_node"], by_kind["ingest_events"]
    assert (w1["arm"], w1["op_seq"], w1["class"]) == ("value", 0, "A|B")
    assert w1["identity"]["uid"] in ("A", ["A"])
    assert w1["matched_term"] == 0, "T1a"
    # E-3's ruling, on the very match the two frozen sites disagreed about:
    # `T1a` carries `vt: "*"` and `props: "*"`, so neither is attribution and
    # §13.6's two-element spelling — not D13.27's four — is the correct one.
    assert set(w1["matched_on"]) == {"kinds", "targets.nodes"}, w1["matched_on"]

    assert (w2["arm"], w2["op_seq"], w2["class"]) == ("value", 1, "A")
    assert w2["matched_term"] == 3, "T2"
    assert set(w2["matched_on"]) == {"kinds", "targets.incident", "rel_types",
                                     "vt"}, (
        "§13.6's own attribution for `fp1e → T2` — four concrete conjuncts, "
        f"and `props` is vacuous because the footprint's is `\"*\"` — "
        f"{w2['matched_on']}")
    assert w1["batch_id"] == w2["batch_id"], "one batch, two ops"


@requires_check_trace
def test_the_worked_example_per_step_attribution(stores):
    """**§13.6, the per-step half.** *"The plan verdict is one bit, but each
    witness names the step it actually hit."*

    `check_trace` checks each step against its own scope and its own `tt_q` and
    folds (M4 plan §3.6b): sound, strictly more precise than the merged scope,
    and it is what makes `step_id` — hence D5.4's two granularities and §4.6's
    disaggregation — possible at all. The attribution §13.6 states is exact:
    `s1` is hit by the `assert_node`, `s2` by the `ingest_events`, and `s3` is
    `compute`, which is ∅ intrinsically (D5.3) and inherits everything.
    """
    s = stores("w136s")
    record, _before, _after = _run_worked_example(s)

    verdict = fx.check_record(s, record)
    ws = fx.assert_stale(verdict, "§13.6 per-step")
    by_step = {w["step_id"]: w for w in ws}
    assert set(by_step) == {"s1", "s2"}, (
        "`s3` is `compute`: ∅ intrinsically, all dependency inherited (D5.3) — "
        f"{fx.describe(verdict)}")
    assert by_step["s1"]["kind"] == "assert_node"
    assert by_step["s2"]["kind"] == "ingest_events"

    # the merged check is the fallback and must never be *more* fresh than the
    # per-step fold — monotonicity of the widening (M4 plan §3.6b)
    merged = fx.check_scope(s, DependencyScope.from_json(record["dependency"]))
    assert not fx.is_fresh(merged), fx.describe(merged)


def test_the_worked_example_variant_is_a_true_false_invalidation(stores):
    """**§13.6's variant** — *drop `op1`, keep `op0`.*

    The verdict is still `POSSIBLY_STALE`, with one witness against `s1`, but
    the **answer is unchanged**: `s1.edges` is untouched, so `s2` and `s3` are
    untouched. That is a **false invalidation** — permitted by D1.13, counted
    against invalidation precision by D6.2, and *"a fair illustration of the
    cost the contract deliberately accepts."*

    It is also D5.4's third bullet: stale at the step level, fresh at the answer
    level, which is the argument for reporting both granularities rather than
    only the conservative one. It is the cheapest available demonstration that
    the harness's two metrics measure different things.
    """
    s = stores("w136v")
    fx.build_s0(s)
    ex = _executor(s)

    before = ex.run(_worked_example_plan())
    record = before.to_json()

    s.assert_node("A", "Node", {"tier": "gold"}, 0, OPEN_END)   # op0 only

    after = ex.run(_worked_example_plan())
    assert after.answer == before.answer, "the answer did not move"
    step_digests = {st["step_id"]: st["result_digest"] for st in after.steps}
    old_digests = {st["step_id"]: st["result_digest"] for st in before.steps}
    assert step_digests["s1"] != old_digests["s1"], "s1 changed (new vid/props)"
    assert step_digests["s2"] == old_digests["s2"], "s2 did not"

    plan_scope = DependencyScope.from_json(record["dependency"])
    verdict = fx.check_scope(s, plan_scope)
    ws = fx.assert_stale(verdict, "§13.6 variant")
    assert len(ws) == 1 and ws[0]["kind"] == "assert_node", fx.describe(verdict)
    # `s1`'s terms are the only ones the op reaches: `T2` refuses it on the
    # node/edge routing in `targets_match`, and would refuse the carve arm a
    # second time on `Pᵥ` (§13.6's intersection table, rows 3 and 5)
    assert ws[0]["matched_term"] == 0, "T1a"


# ===========================================================================
# the contract-level invariants, as property tests
# ===========================================================================

def test_the_empty_scope_is_always_fresh(stores):
    """**∅ ⇒ FRESH, always** (D13.2, D13.24 step 7, D5.3).

    *"An empty `terms` list is the empty scope ∅ — nothing can ever invalidate
    this result — and is the correct, non-degenerate value for a `compute` node
    over literal inputs."* The `compute` kernel takes an `adapter` and never
    reads it, so its domain is ∅ **intrinsically**; all of a compute step's
    dependency is inherited through §5's union.

    The one caution §5 states is asserted next door in CE-2/§13.6: a `compute`
    step's own emptiness must never be read as "this step contributes no
    dependency to the plan".
    """
    s = stores("empty")
    s.assert_node("A", "N", {}, 0, OPEN_END)
    empty = DependencyScope.empty(s.store_identity, s.frontier_tt())
    assert empty.is_empty

    for i in range(3):
        s.assert_node(f"n{i}", "N", {"p": i}, 0, OPEN_END)
        s.ingest_events([{"src": "A", "dst": f"n{i}", "rel_type": "MSG",
                          "vt_s": 10 + i}])
        s.correct(EntityRef(kind="node", uid="A"), {"p": i}, 0, 10)
        fx.assert_fresh(fx.check_scope(s, empty), "∅ scope")

    # and the live derivation agrees: `compute` is ∅ from day one
    from tgms.tgir.ttq import dependency_of
    assert dependency_of("compute", fx.basis_for(s)).is_empty


@pytest.mark.parametrize("component",
                         ["kinds", "targets", "rel_types", "vt", "props"])
def test_widening_never_turns_possibly_stale_into_fresh(stores, component):
    """**D4.5 / D13.1 — every approximation widens.**

    *"`D` must over-approximate the true dependency: widening any component of
    `D` preserves soundness; narrowing requires proof."* Membership is a
    conjunction (D8.2), so replacing any one component with ⊤ can only admit
    more ops — and *"no operation in this contract is permitted to narrow
    anything at runtime."*

    Run against a firing scenario (CE-5's carve, caught on the value arm) with
    each of the five components widened in turn. This is also the property that
    makes rollback safe: `"*"` admits everything a derivation admitted and more,
    so coarsening an operator is never a correctness event.
    """
    s = stores(f"widen-{component}")
    fx.build_s0(s)
    q = {"group_by": [], "aggregates": [{"agg": "count"}],
         "window": {"t_a": 0, "t_b": 100}, "rel_types": ["ROLE"]}
    scope = fx.scope_of_call(s, "aggregate_events", q)

    s.correct(EntityRef(kind="edge", src="A", dst="X", rel_type="ROLE", disc=""),
              {"level": 2}, 30, 40)

    fx.assert_stale(fx.check_scope(s, scope), "the narrow scope")
    fx.assert_stale(fx.check_scope(s, fx.widened(scope, component)),
                    f"widened on `{component}`")


def test_undecidable_is_never_downgraded_to_fresh(stores):
    """**D13.25 — `UNDECIDABLE` is not a third contract.**

    *"Every consumer treats `UNDECIDABLE` as `POSSIBLY_STALE`; it is separated
    only so a diagnosis is not lost inside a conservative verdict. D1.13 is
    untouched: the mechanism never says `FRESH` when it cannot decide."*

    The three refusals a scope can carry into `check` on its own account:
    an unrecognized `version` (D13.9's gate, which is what lets §7's ladder
    replace Level-0 terms with Level-2 signatures without a stale reader
    silently vouching for a stored result), a `store` mismatch (D13.24 step 3),
    and a rewritten log prefix (D13.18 — *"the only defence against a scope
    being evaluated against a log that is not the log it was cut from"*).
    """
    s = stores("undecidable")
    s.assert_node("A", "N", {"p": 1}, 0, OPEN_END)
    scope = fx.scope_of_call(s, "entity_history", {"uid": "A"})
    fx.assert_fresh(fx.check_scope(s, scope), "the baseline")

    future = replace(scope, version=scope.version + 99)
    verdict = fx.check_scope(s, future)
    assert not fx.is_fresh(verdict), f"D13.9 — {fx.describe(verdict)}"
    assert fx.verdict_name(verdict) == "UNDECIDABLE", fx.describe(verdict)

    elsewhere = replace(scope, store="some-other-store")
    verdict = fx.check_scope(s, elsewhere)
    assert not fx.is_fresh(verdict), f"store mismatch — {fx.describe(verdict)}"
    assert fx.verdict_name(verdict) == "UNDECIDABLE", fx.describe(verdict)

    tampered = replace(scope, checkpoints=(
        Checkpoint(scope.min_offset, "0" * len(scope.checkpoints[0].chain)),))
    verdict = fx.check_scope(s, tampered)
    assert not fx.is_fresh(verdict), f"D13.18 — {fx.describe(verdict)}"
    assert fx.verdict_name(verdict) == "UNDECIDABLE", fx.describe(verdict)


def test_a_pinned_result_still_gets_its_suffix_scanned(stores):
    """**T1 does not exempt a pinned result from the scan** (D13.24's closing
    note; Corollary C1 as amended).

    *"No step is skippable on the strength of `pinned = true`. D1.10 clamps an
    above-frontier `as_of_tt` down to the frontier, so a genuinely pinned scope
    scans a suffix that is empty **because the log says so**, not because a flag
    said to skip the scan. The shortcut Corollary C1 used to license is what
    FF-4 exploited."*

    Asserted structurally: a pinned scope whose `tt_q` predates a later batch
    reports that batch as a witness. The recompute is stable (T1), so this is a
    false invalidation — the permitted direction, and the price of not carrying
    a `pinned` short-circuit at any level.
    """
    s = stores("pinned")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    frontier = s.frontier_tt()
    q = {"uid": "A", "as_of_tt": frontier}

    before = fx.call(s, "entity_history", q)
    scope = DependencyScope.from_json(before["dependency"])
    assert scope.pinned is True and scope.clamped is False
    fx.assert_fresh(fx.check_scope(s, scope), "nothing has happened yet")

    s.assert_node("A", "L", {"tier": "gold"}, 0, OPEN_END)

    assert fx.digest_of(s, "entity_history", q) == before["result_digest"], "T1"
    verdict = fx.check_scope(s, scope)
    assert not fx.is_fresh(verdict), (
        "the pinned scope must still walk the suffix — "
        f"{fx.describe(verdict)}")
    assert fx.witnesses(verdict), "and report what it found"


def test_tt_now_defaults_to_open_end_and_scans_the_whole_suffix(stores):
    """**`tt_now` rounds *up*, and its default is `OPEN_END`** (M4 plan §3.4,
    §9.1).

    The rounding direction is the opposite of `tt_q`'s: a `tt_now` below a batch
    that is already in the log excludes it from the scan while it is visible to
    anyone who recomputes. The log is fsynced **before** apply, so the log always
    leads the frontier and passing the applied frontier as `tt_now` is the
    false-fresh direction. `OPEN_END` is the only default that is sound without
    an argument; a caller who passes a smaller `tt_now` is asking a narrower
    question and owns it.
    """
    s = stores("ttnow")
    s.assert_node("A", "L", {"p": 1}, 0, OPEN_END)
    scope = fx.scope_of_call(s, "entity_history", {"uid": "A"})

    s.assert_node("A", "L", {"p": 2}, 0, OPEN_END)

    fx.assert_stale(fx.check_scope(s, scope), "the default `tt_now`")
    # the narrower question, asked deliberately: nothing had landed by `tt_q`
    fx.assert_fresh(fx.check_scope(s, scope, tt_now=scope.tt_q),
                    "`tt_now = tt_q` is an empty suffix by construction")
