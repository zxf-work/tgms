"""The scope-derivation rollout — differential and matrix tests.

`docs/design/SCOPE_DERIVATION_ROLLOUT_DESIGN_2026-09-14.md` (internal, not in
this worktree) specifies ten new `LEAF_SCOPES` derivations in
`tgms/tgir/leaves.py`. This file is the rollout's own test home (one class per
operator), separate from `tests/test_tgir_scopes.py`, which keeps the three
already-shipped derivations (`entity_history`, `neighborhood_evolution`,
`aggregate_events`).

Two independent lines of evidence per operator, exactly as the design's §4
specifies:

- **A hand-written matrix** (§4.1's "per-operator matrices" complement): the
  positive case, the entity-kind exclusion, the `V` exclusion, the carve-arm
  verdict, and the `dense_ids` existence flip where the operator has one.
  Uses the independent D13.20-D13.23 oracle already living in
  `test_tgir_scopes.py` (`hits`, `arms_that_hit`, `footprints`) rather than
  re-deriving it — the point of that independence is separateness from
  `tgms/tgir/check.py`, not from other test modules.
- **A differential test** (§4.1's template): real corrections
  (`tgms.eval.corrections`, the same 8-generator x 5-placement matrix the
  design's own forecast is built on) applied to a real store inside a rolled-
  back transaction, comparing a real recompute's outcome against the derived
  scope's verdict. `changed => in_scope` is asserted — the soundness gate,
  never merely reported — and the observed exclusion rate is printed and
  checked against a floor.

**Scale.** The design's own template samples >= 2000 trials x 20 corrections
per operator for a dedicated measurement run. These are unit tests
("seconds each" — the lane's own instructions), so trial counts here are cut
by two orders of magnitude; the soundness assertion is unaffected by sample
size (it is a per-correction assertion, not a statistical one), and the
printed exclusion rate is reported as an approximation, with a floor set
low enough to absorb the resulting noise rather than the design's own
central estimate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Iterable

import pytest

from tgms.core.errors import CostError, InvalidArgError, LimitError, NotFoundError
from tgms.eval.corrections import Target, generate, probe_substrate
from tgms.storage.base import make_op
from tgms.temporal.algebra import call_operator, ensure_all_registered, validate_args
from tgms.tgir.depscope import TOP, Incident
from tgms.tgir.leaf import sigma_for
from tgms.tgir.leaves import P_VALUE, terms_for

from tests.test_tgir_scopes import (
    arms_that_hit,
    assert_edge,
    assert_node,
    correct_edge,
    correct_node,
    hits,
    ingest,
    retract_edge,
)

#: The registry is populated on import so every `validate_args`/`terms_for`
#: call below — including the ones at class-body evaluation time via the
#: matrix tables — sees every operator.
ensure_all_registered()

# ---------------------------------------------------------------------------
# the shared store
# ---------------------------------------------------------------------------

N_NODES = 12
NODE_UIDS = tuple(f"n{i}" for i in range(N_NODES))


def _mk_store():
    from tests.conftest import fresh_adapter

    a = fresh_adapter()
    nodes = [make_op("assert_node", uid=u, label="A" if i % 2 == 0 else "B",
                     vt_s=0, vt_e=1000, source="setup", provenance_ref=None)
             for i, u in enumerate(NODE_UIDS)]
    a.begin()
    a.apply_ops(nodes, 1)
    a.commit()
    rng = random.Random(7)
    rels = ("R", "S")
    edges = []
    k = 0
    for i in range(N_NODES):
        for j in (1, 2, 3):
            dst = (i + j) % N_NODES
            vt_s = rng.randrange(0, 700)
            vt_e = vt_s + rng.randrange(20, 250)
            edges.append(make_op(
                "assert_edge", src=f"n{i}", dst=f"n{dst}", rel_type=rels[k % 2],
                disc=f"e{k}", vt_s=vt_s, vt_e=vt_e, source="setup", provenance_ref=None))
            k += 1
    a.begin()
    a.apply_ops(edges, 2)
    a.commit()
    return a


@pytest.fixture()
def store():
    a = _mk_store()
    yield a
    a.close()


# ---------------------------------------------------------------------------
# the differential harness (design §4.1, at unit-test scale)
# ---------------------------------------------------------------------------

class _StoreLike:
    """The one attribute `tgms.eval.corrections` needs from a `Store`."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter


def _outcome(adapter: Any, op: str, args: dict[str, Any]) -> tuple[str, Any]:
    """The operator's outcome, digested — a real result or an error class.
    Catching only the error class (never the message) is what makes the
    `dense_ids` hazard of §1.8 visible: a `NotFoundError` that becomes a
    result is a `changed` outcome exactly like a moved digest."""
    try:
        env = call_operator(adapter, op, dict(args))
        return ("ok", env["result_digest"])
    except (NotFoundError, CostError, LimitError, InvalidArgError) as e:
        return ("err", type(e).__name__)


@dataclass
class DiffResult:
    n: int
    n_changed: int
    n_in_scope: int

    @property
    def exclusion_rate(self) -> float:
        return 1.0 - (self.n_in_scope / self.n if self.n else 0.0)

    @property
    def precision(self) -> float:
        return (self.n_changed / self.n_in_scope) if self.n_in_scope else 0.0


def run_differential(adapter: Any, op: str, args: dict[str, Any], *,
                     read_uids: Iterable[str] = (),
                     window: tuple[int, int] | None = None,
                     trials: int = 8, seed: int = 0) -> DiffResult:
    """One `(op, args)` cell against `generate`'s realized correction matrix.

    Each correction is applied inside `begin()`/`rollback()`, so every trial
    sees the same pristine base store (D13.20's `probe_substrate` reads it
    once, up front) — a real fork without DuckDB's cost of rebuilding one.
    `changed ⇒ in_scope` is asserted per correction, never batched away.
    """
    ensure_all_registered()
    filled = validate_args(op, dict(args))
    terms = terms_for(op, filled, sigma_for(op, filled))
    before = _outcome(adapter, op, filled)
    store_like = _StoreLike(adapter)
    sub = probe_substrate(store_like, sample=50, rng=random.Random(seed))
    target = Target(read_uids=tuple(read_uids), window=window)
    rng = random.Random(seed + 1)
    n = n_changed = n_in_scope = 0
    for _ in range(trials):
        for corr in generate(store_like, sub, target, rng=rng):
            adapter.begin()
            try:
                adapter.apply_ops(list(corr.ops), 5_000_000)
            except Exception:
                adapter.rollback()
                continue
            after = _outcome(adapter, op, filled)
            adapter.rollback()
            changed = after != before
            in_scope = any(hits(terms, one_op) for one_op in corr.ops)
            n += 1
            n_changed += int(changed)
            n_in_scope += int(in_scope)
            assert not (changed and not in_scope), (
                f"{op}{filled}: {corr.generator}/{corr.placement} changed the "
                f"result ({before} -> {after}) but the derived scope missed "
                f"it: {corr.to_json()}")
    return DiffResult(n, n_changed, n_in_scope)


# ---------------------------------------------------------------------------
# §2.7 / §2.8 — count_temporal_motifs, find_temporal_motif_instances
# ---------------------------------------------------------------------------

class TestTemporalMotifs:
    """One derivation, two `LEAF_SCOPES` entries."""

    UNFILTERED = {"motif": "M_2node_pingpong", "delta": 50,
                  "window": {"t_a": 0, "t_b": 900}}
    FILTERED = {**UNFILTERED, "node_filter": ["n0", "n1", "n2"]}

    MATRIX = [
        # the positive case
        ("an edge event inside the window", UNFILTERED,
         assert_edge("n0", "n1", vt_s=10, vt_e=11), True),
        ("an edge correction inside the window", UNFILTERED,
         correct_edge("n0", "n1", vt_s=10, vt_e=20), True),
        ("an edge retraction inside the window", UNFILTERED,
         retract_edge("n0", "n1", 5), True),
        ("events ingested inside the window", UNFILTERED,
         ingest("n0", "n1", 10), True),
        # entity-kind exclusion: the operator reads only edges
        ("a node write, no node_filter", UNFILTERED, assert_node("n0"), False),
        ("a node correction, no node_filter", UNFILTERED, correct_node("n0"), False),
        # V exclusion
        ("an edge event after the window", UNFILTERED,
         assert_edge("n0", "n1", vt_s=1000, vt_e=1001), False),
        # carve-arm verdict: P = Pv, not carve-reachable (gate Appendix A.3)
        ("a carve of an edge, entirely outside the window", UNFILTERED,
         correct_edge("n0", "n1", vt_s=2000, vt_e=2010), False),
        # node_filter branch: role="both" on the read term
        ("an edge with both endpoints in the filter", FILTERED,
         assert_edge("n0", "n1", vt_s=10, vt_e=11), True),
        ("an edge naming neither filtered uid", FILTERED,
         assert_edge("n9", "n10", vt_s=10, vt_e=11), False),
        # the derivation's one subtlety: `both` on the read term excludes a
        # single-endpoint edge from the *motif count*, but the existence
        # pair's `either` still catches it, because dense_ids(node_filter)
        # raises if ANY one of the filter's uids is unknown
        ("an edge with only ONE endpoint in the filter — excluded by the read "
         "term's `both`, still caught by the existence pair's `either`",
         FILTERED, assert_edge("n0", "n9", vt_s=10, vt_e=11), True),
        ("an assert_node registering a filtered uid (the existence pair)",
         FILTERED, assert_node("n1"), True),
        ("an assert_node registering an unfiltered uid", FILTERED,
         assert_node("n9"), False),
    ]

    @pytest.mark.parametrize("label,args,op,must", MATRIX, ids=[m[0] for m in MATRIX])
    def test_matrix(self, label, args, op, must):
        filled = validate_args("count_temporal_motifs", dict(args))
        terms = terms_for("count_temporal_motifs", filled, sigma_for("count_temporal_motifs", filled))
        assert hits(terms, op) is must, (
            f"{label}: should {'intersect' if must else 'NOT intersect'}; "
            f"arms hit: {arms_that_hit(terms, op)}")

    def test_find_temporal_motif_instances_shares_the_derivation(self):
        filled = validate_args("find_temporal_motif_instances", dict(self.UNFILTERED))
        terms = terms_for("find_temporal_motif_instances", filled,
                          sigma_for("find_temporal_motif_instances", filled))
        assert hits(terms, assert_edge("n0", "n1", vt_s=10, vt_e=11))
        assert not hits(terms, assert_edge("n0", "n1", vt_s=1000, vt_e=1001))

    def test_rel_type_is_never_narrowed(self):
        """T = ⊤ always and explicitly: the matcher ignores rel_type."""
        filled = validate_args("count_temporal_motifs", dict(self.UNFILTERED))
        (term,) = terms_for("count_temporal_motifs", filled, sigma_for("count_temporal_motifs", filled))
        assert term.rel_types is TOP
        assert hits((term,), assert_edge("n0", "n1", rel_type="ZZZ", vt_s=10, vt_e=11))

    def test_role_both_vs_either_asymmetry(self):
        filled = validate_args("count_temporal_motifs", dict(self.FILTERED))
        read_term, existence_node, existence_dense = terms_for(
            "count_temporal_motifs", filled, sigma_for("count_temporal_motifs", filled))
        assert read_term.targets.incident == Incident("both", ("n0", "n1", "n2"))
        assert existence_node.targets.nodes == ("n0", "n1", "n2")
        assert existence_dense.targets.incident == Incident("either", ("n0", "n1", "n2"))

    def test_i_cannot_narrow_to_the_declared_filter_when_unfiltered(self):
        """§9.11: unfiltered, `I = ⊤` — a motif instance is a combination, not
        a neighbourhood a static uid set can bound."""
        filled = validate_args("count_temporal_motifs", dict(self.UNFILTERED))
        (term,) = terms_for("count_temporal_motifs", filled, sigma_for("count_temporal_motifs", filled))
        assert term.targets.edges is TOP

    def test_differential_unfiltered(self, store):
        result = run_differential(store, "count_temporal_motifs", dict(self.UNFILTERED),
                                  window=(0, 900), trials=10, seed=1)
        print(f"count_temporal_motifs[unfiltered]: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3

    def test_differential_filtered(self, store):
        result = run_differential(store, "count_temporal_motifs", dict(self.FILTERED),
                                  read_uids=("n0", "n1", "n2"), window=(0, 900),
                                  trials=10, seed=2)
        print(f"count_temporal_motifs[filtered]: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3

    def test_differential_find_instances(self, store):
        result = run_differential(store, "find_temporal_motif_instances", dict(self.UNFILTERED),
                                  window=(0, 900), trials=10, seed=3)
        print(f"find_temporal_motif_instances[unfiltered]: exclusion="
              f"{result.exclusion_rate:.2f} precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3


# ---------------------------------------------------------------------------
# §2.9 — temporal_reachability
# ---------------------------------------------------------------------------

class TestTemporalReachability:
    ARGS = {"src": "n0", "window": {"t_a": 0, "t_b": 900}}

    MATRIX = [
        # the positive case: I = TOP (as E), so ANY edge inside the window
        # intersects, not only ones incident to src (CE-1's chain argument)
        ("an edge event inside the window, unrelated to src", ARGS,
         assert_edge("n5", "n6", vt_s=10, vt_e=11), True),
        ("a correction to an edge overlapping the window", ARGS,
         correct_edge("n5", "n6", vt_s=10, vt_e=20), True),
        ("a retraction of an edge overlapping the window", ARGS,
         retract_edge("n5", "n6", 5), True),
        ("events ingested inside the window", ARGS, ingest("n5", "n6", 10), True),
        # entity-kind exclusion: a node write unrelated to src
        ("a node write, unrelated to src", ARGS, assert_node("n9"), False),
        # V exclusion
        ("an edge event after the window", ARGS,
         assert_edge("n5", "n6", vt_s=1000, vt_e=1001), False),
        # carve-arm verdict: P = Pv, not carve-reachable
        ("a carve of an edge, entirely outside the window", ARGS,
         correct_edge("n5", "n6", vt_s=2000, vt_e=2010), False),
        # the existence pair, scoped to src alone
        ("an assert_node registering src itself", ARGS, assert_node("n0"), True),
        ("an assert_edge naming src as an endpoint, far outside the window",
         ARGS, assert_edge("n0", "n50", vt_s=2000, vt_e=2001), True),
        ("an assert_node registering an unrelated uid", ARGS, assert_node("n1"), False),
    ]

    @pytest.mark.parametrize("label,args,op,must", MATRIX, ids=[m[0] for m in MATRIX])
    def test_matrix(self, label, args, op, must):
        filled = validate_args("temporal_reachability", dict(args))
        terms = terms_for("temporal_reachability", filled, sigma_for("temporal_reachability", filled))
        assert hits(terms, op) is must, (
            f"{label}: should {'intersect' if must else 'NOT intersect'}; "
            f"arms hit: {arms_that_hit(terms, op)}")

    def test_i_and_t_are_forced_to_top(self):
        """§9.13: both unavoidable — CE-1 (a chain of new edges can connect
        src to nodes the fixpoint never labelled) and, under delta_max_wait,
        non-monotonicity."""
        filled = validate_args("temporal_reachability", dict(self.ARGS))
        read_term = terms_for("temporal_reachability", filled, sigma_for("temporal_reachability", filled))[0]
        assert read_term.targets.edges is TOP
        assert read_term.rel_types is TOP

    def test_num_entities_is_not_in_r(self):
        """§2.9: `adapter.num_entities()` only sizes the label array — an
        added entity gets INF and is filtered, so the entity count is not in
        `R` and `K = edge` stands (the read-tracing property test's
        allowlist entry, checked structurally here as "no node term exists
        beyond the existence pair")."""
        filled = validate_args("temporal_reachability", dict(self.ARGS))
        terms = terms_for("temporal_reachability", filled, sigma_for("temporal_reachability", filled))
        # only the existence pair's node term names `nodes`, and it is scoped
        # to `src` alone, never to `"*"` the way a num_entities dependency
        # would require
        node_terms = [t for t in terms if getattr(t.targets, "nodes", None) is not None]
        assert len(node_terms) == 1
        assert node_terms[0].targets.nodes == ("n0",)

    def test_differential(self, store):
        result = run_differential(store, "temporal_reachability", dict(self.ARGS),
                                  read_uids=("n0",), window=(0, 900), trials=10, seed=4)
        print(f"temporal_reachability: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.1


# ---------------------------------------------------------------------------
# §2.10 — temporal_paths
# ---------------------------------------------------------------------------

class TestTemporalPaths:
    """Same shape as `temporal_reachability`, with `P = ⊤` — the one
    difference, contrasted directly below."""

    ARGS = {"src": "n0", "dst": "n3", "window": {"t_a": 0, "t_b": 900}}

    MATRIX = [
        ("an edge event inside the window, unrelated to src/dst", ARGS,
         assert_edge("n5", "n6", vt_s=10, vt_e=11), True),
        ("events ingested inside the window", ARGS, ingest("n5", "n6", 10), True),
        ("a node write, unrelated to src/dst", ARGS, assert_node("n9"), False),
        # V exclusion only bites a non-carving op (ingest_events emits no
        # carve arm at all): an assert/correct/retract outside the window
        # still intersects through its unconditional carve arm, below
        ("events ingested after the window (no carve arm to rescue it)", ARGS,
         ingest("n5", "n6", 1000), False),
        # the contrast with temporal_reachability: P = TOP, so the carve arm
        # DOES reach this operator (paths are ranked by a key that includes
        # each edge's vt_s, and it is a top-k, so a carve can reorder or
        # displace a result even from entirely outside the window) — and,
        # unlike ingest_events, assert/correct/retract always carry one
        ("an assert_edge, entirely outside the window (P = TOP: the carve "
         "arm reaches it)", ARGS,
         assert_edge("n5", "n6", vt_s=2000, vt_e=2001), True),
        ("a carve of an edge, entirely outside the window (P = TOP: reaches)",
         ARGS, correct_edge("n5", "n6", vt_s=2000, vt_e=2010), True),
        ("an assert_node registering src", ARGS, assert_node("n0"), True),
        ("an assert_node registering dst", ARGS, assert_node("n3"), True),
        ("an assert_node registering an unrelated uid", ARGS, assert_node("n1"), False),
    ]

    @pytest.mark.parametrize("label,args,op,must", MATRIX, ids=[m[0] for m in MATRIX])
    def test_matrix(self, label, args, op, must):
        filled = validate_args("temporal_paths", dict(args))
        terms = terms_for("temporal_paths", filled, sigma_for("temporal_paths", filled))
        assert hits(terms, op) is must, (
            f"{label}: should {'intersect' if must else 'NOT intersect'}; "
            f"arms hit: {arms_that_hit(terms, op)}")

    def test_p_is_top_unlike_reachability(self):
        """§2.10's one difference from §2.9: the carve arm reaches this
        operator (a top-k ranked by a key naming each edge's vt_s), so V is
        worth nothing against a Class B/C/D op on an edge identity — only
        the entity-kind exclusion in E survives, and it is the whole of the
        narrowing."""
        reach_filled = validate_args("temporal_reachability",
                                     {"src": "n0", "window": {"t_a": 0, "t_b": 900}})
        reach_term = terms_for("temporal_reachability", reach_filled,
                               sigma_for("temporal_reachability", reach_filled))[0]
        paths_filled = validate_args("temporal_paths", dict(self.ARGS))
        paths_term = terms_for("temporal_paths", paths_filled, sigma_for("temporal_paths", paths_filled))[0]
        assert reach_term.props == P_VALUE
        assert paths_term.props is TOP
        far_carve = correct_edge("n5", "n6", vt_s=5000, vt_e=5010)
        assert not hits((reach_term,), far_carve)
        assert hits((paths_term,), far_carve)

    def test_existence_pair_covers_both_src_and_dst(self):
        filled = validate_args("temporal_paths", dict(self.ARGS))
        terms = terms_for("temporal_paths", filled, sigma_for("temporal_paths", filled))
        node_terms = [t for t in terms if getattr(t.targets, "nodes", None) is not None]
        assert len(node_terms) == 1
        assert set(node_terms[0].targets.nodes) == {"n0", "n3"}

    def test_differential(self, store):
        result = run_differential(store, "temporal_paths", dict(self.ARGS),
                                  read_uids=("n0", "n3"), window=(0, 900),
                                  trials=10, seed=5)
        print(f"temporal_paths: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.1


# ---------------------------------------------------------------------------
# §2.5 — burst_detection
# ---------------------------------------------------------------------------

class TestBurstDetection:
    WINDOW = {"t_a": 0, "t_b": 900}
    EDGE_RATE = {"target": {"kind": "edge_event_rate"}, "window": WINDOW, "stride": 10}
    NODE_ACTIVITY = {"target": {"kind": "node_activity", "uid": "n0"},
                     "window": WINDOW, "stride": 10}
    NODE_ACTIVITY_REL = {"target": {"kind": "node_activity", "uid": "n0",
                                    "rel_type": "R"}, "window": WINDOW, "stride": 10}

    EDGE_RATE_MATRIX = [
        ("an edge event inside the window", EDGE_RATE,
         assert_edge("n5", "n6", vt_s=10, vt_e=11), True),
        ("a correction inside the window", EDGE_RATE,
         correct_edge("n5", "n6", vt_s=10, vt_e=20), True),
        ("events ingested inside the window", EDGE_RATE, ingest("n5", "n6", 10), True),
        ("a node write", EDGE_RATE, assert_node("n5"), False),
        ("an edge event after the window", EDGE_RATE,
         assert_edge("n5", "n6", vt_s=1000, vt_e=1001), False),
        ("a carve of an edge, entirely outside the window (P = Pv: excluded)",
         EDGE_RATE, correct_edge("n5", "n6", vt_s=2000, vt_e=2010), False),
    ]

    NODE_ACTIVITY_MATRIX = [
        ("an edge incident to the target uid, inside the window",
         NODE_ACTIVITY, assert_edge("n0", "n6", vt_s=10, vt_e=11), True),
        ("an edge NOT incident to the target uid", NODE_ACTIVITY,
         assert_edge("n5", "n6", vt_s=10, vt_e=11), False),
        # this ALSO names n0 as an endpoint, so — even outside the window —
        # the existence pair (vt = TOP, no rel_type restriction) catches it;
        # only an op naming neither endpoint as n0 is a genuine precision win
        ("an edge incident to the target uid, outside the window (the "
         "existence pair, not the read term, catches it)",
         NODE_ACTIVITY, assert_edge("n0", "n6", vt_s=1000, vt_e=1001), True),
        # the existence pair
        ("an assert_node registering the target uid", NODE_ACTIVITY,
         assert_node("n0"), True),
        ("an assert_edge naming the target uid, far outside the window",
         NODE_ACTIVITY, assert_edge("n0", "n50", vt_s=2000, vt_e=2001), True),
        ("an assert_node registering an unrelated uid", NODE_ACTIVITY,
         assert_node("n1"), False),
        # the rel_type filter narrows T on the READ term, but the existence
        # pair (rel_types = TOP always) still catches an edge naming n0 —
        # so this is a `both` arms hit, not a pure T-exclusion case
        ("an incident edge of a DIFFERENT rel_type — the read term's T "
         "excludes it, but the existence pair still catches it",
         NODE_ACTIVITY_REL, assert_edge("n0", "n6", rel_type="S", vt_s=10, vt_e=11),
         True),
        ("an incident edge of the named rel_type", NODE_ACTIVITY_REL,
         assert_edge("n0", "n6", rel_type="R", vt_s=10, vt_e=11), True),
        # a genuine T-exclusion, isolated from the existence pair: `correct`
        # is not in K_DENSE_ID (it carries no entity-kind discriminator that
        # would let it register a NEW dense id), so a correction incident to
        # n0 of the wrong rel_type is excluded outright
        ("a correction incident to the target uid, wrong rel_type "
         "(isolates T — correct never registers a dense id)",
         NODE_ACTIVITY_REL, correct_edge("n0", "n6", rel_type="S", vt_s=10, vt_e=11),
         False),
    ]

    @pytest.mark.parametrize("label,args,op,must", EDGE_RATE_MATRIX + NODE_ACTIVITY_MATRIX,
                             ids=[m[0] for m in EDGE_RATE_MATRIX + NODE_ACTIVITY_MATRIX])
    def test_matrix(self, label, args, op, must):
        filled = validate_args("burst_detection", dict(args))
        terms = terms_for("burst_detection", filled, sigma_for("burst_detection", filled))
        assert hits(terms, op) is must, (
            f"{label}: should {'intersect' if must else 'NOT intersect'}; "
            f"arms hit: {arms_that_hit(terms, op)}")

    def test_edge_event_rate_has_no_entity_filter(self):
        filled = validate_args("burst_detection", dict(self.EDGE_RATE))
        (term,) = terms_for("burst_detection", filled, sigma_for("burst_detection", filled))
        assert term.targets.edges is TOP

    def test_node_activity_incident_arm_matches_the_kernel_mask(self):
        filled = validate_args("burst_detection", dict(self.NODE_ACTIVITY))
        read_term = terms_for("burst_detection", filled, sigma_for("burst_detection", filled))[0]
        assert read_term.targets.incident == Incident("either", ("n0",))

    def test_differential_edge_event_rate(self, store):
        result = run_differential(store, "burst_detection", dict(self.EDGE_RATE),
                                  window=(0, 900), trials=10, seed=6)
        print(f"burst_detection[edge_event_rate]: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3

    def test_differential_node_activity(self, store):
        result = run_differential(store, "burst_detection", dict(self.NODE_ACTIVITY),
                                  read_uids=("n0",), window=(0, 900), trials=10, seed=7)
        print(f"burst_detection[node_activity]: exclusion={result.exclusion_rate:.2f} "
              f"precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3


# ---------------------------------------------------------------------------
# §2.4 — graph_metric_timeseries
# ---------------------------------------------------------------------------

class TestGraphMetricTimeseries:
    def _args(self, metric, t_a=0, t_b=900):
        return {"metric": metric, "window": {"t_a": t_a, "t_b": t_b}, "stride": 10}

    EDGE_METRIC_MATRIX = [
        ("an edge event inside the window", "edge_event_count",
         assert_edge("n5", "n6", vt_s=10, vt_e=11), True),
        ("a node write", "edge_event_count", assert_node("n5"), False),
        ("an edge event after the window", "edge_event_count",
         assert_edge("n5", "n6", vt_s=1000, vt_e=1001), False),
        ("a carve of an edge, entirely outside the window (P = Pv: excluded)",
         "edge_event_count", correct_edge("n5", "n6", vt_s=2000, vt_e=2010), False),
        ("reciprocity reads only edges too", "reciprocity",
         assert_edge("n5", "n6", vt_s=10, vt_e=11), True),
        ("reciprocity: a node write does not intersect", "reciprocity",
         assert_node("n5"), False),
    ]

    NODE_METRIC_MATRIX = [
        ("a node write inside the window", "node_count",
         assert_node("n5", vt_s=10, vt_e=11), True),
        ("an edge write", "node_count", assert_edge("n5", "n6"), False),
        ("a node write after the window", "node_count",
         assert_node("n5", vt_s=1000, vt_e=1001), False),
    ]

    @pytest.mark.parametrize("label,metric,op,must", EDGE_METRIC_MATRIX + NODE_METRIC_MATRIX,
                             ids=[m[0] for m in EDGE_METRIC_MATRIX + NODE_METRIC_MATRIX])
    def test_matrix(self, label, metric, op, must):
        filled = validate_args("graph_metric_timeseries", self._args(metric))
        terms = terms_for("graph_metric_timeseries", filled,
                          sigma_for("graph_metric_timeseries", filled))
        assert hits(terms, op) is must, (
            f"{label} ({metric}): should {'intersect' if must else 'NOT intersect'}; "
            f"arms hit: {arms_that_hit(terms, op)}")

    def test_mean_out_degree_emits_both_arms(self):
        filled = validate_args("graph_metric_timeseries", self._args("mean_out_degree"))
        terms = terms_for("graph_metric_timeseries", filled,
                          sigma_for("graph_metric_timeseries", filled))
        assert len(terms) == 2
        assert hits(terms, assert_edge("n5", "n6", vt_s=10, vt_e=11))
        assert hits(terms, assert_node("n5", vt_s=10, vt_e=11))

    def test_new_node_rate_widens_to_the_whole_history(self):
        """CE-4: a node version created entirely before t_a can still move a
        birth out of the window, since the answer is a minimum over an
        identity's whole believed history. `node_count`, by contrast, keeps
        the plain window — it is an instant sample, not a history minimum."""
        window_only = validate_args("graph_metric_timeseries",
                                    self._args("node_count", t_a=30, t_b=50))
        widened = validate_args("graph_metric_timeseries",
                                self._args("new_node_rate", t_a=30, t_b=50))
        window_terms = terms_for("graph_metric_timeseries", window_only,
                                 sigma_for("graph_metric_timeseries", window_only))
        widened_terms = terms_for("graph_metric_timeseries", widened,
                                  sigma_for("graph_metric_timeseries", widened))
        early_birth = assert_node("n5", vt_s=5, vt_e=10)
        assert not hits(window_terms, early_birth)
        assert hits(widened_terms, early_birth)
        assert widened_terms[0].vt == ((0, 50),)

    def test_differential_edge_event_count(self, store):
        result = run_differential(store, "graph_metric_timeseries",
                                  self._args("edge_event_count"), window=(0, 900),
                                  trials=10, seed=8)
        print(f"graph_metric_timeseries[edge_event_count]: exclusion="
              f"{result.exclusion_rate:.2f} precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.3

    def test_differential_node_count(self, store):
        result = run_differential(store, "graph_metric_timeseries",
                                  self._args("node_count"), window=(0, 900),
                                  trials=10, seed=9)
        print(f"graph_metric_timeseries[node_count]: exclusion="
              f"{result.exclusion_rate:.2f} precision={result.precision:.2f} n={result.n}")
        assert result.n > 0
        assert result.exclusion_rate >= 0.1
