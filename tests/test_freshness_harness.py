"""M4.5 — the correction generators and the harness's own invariants.

The gate this file discharges (M4_IMPLEMENTATION_PLAN §6, M4.5 row):

(a) **all five effect classes and every placement generate**, and each
    generated batch really applies through the write path;
(b) the D6.2 table populates with the refused/errored column **separated**;
(c) trials are **isolated** (§4.1) and replayable from the five receipt fields;
(d) **both controls** are implemented — the naive row-touch rule (D6.4,
    required) and the all-`"*"` scope.

§8.6 is why (c) is a test rather than a convention: *"isolation bugs in the
harness are false-fresh factories"*, in **both** directions. If trial *n*'s
correction survives into trial *n+1*'s substrate, the recorded `tt_q` and the
log no longer correspond and every classification after it is suspect.
"""

from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path

import pytest

import tgms
from tgms.core.errors import TgmsError
from tgms.eval.corrections import (
    CLASSES, GENERATORS, PLACEMENTS, Substrate, Target, generate, probe_substrate,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_freshness as bf  # noqa: E402


@pytest.fixture
def substrate_store(tmp_path):
    """A store with enough shape to place every generator: closed-ended node
    versions, open-ended ones, edges, and more than one rel_type."""
    s = tgms.open(tmp_path / "store", backend="duckdb")
    for i in range(8):
        s.assert_node(f"n{i}", "Node", {"tier": "bronze", "i": i}, 0, 500)
    for i in range(8, 12):
        s.assert_node(f"n{i}", "Node", {"tier": "bronze"}, 0)   # open-ended
    for i in range(7):
        s.assert_edge(f"n{i}", f"n{i + 1}", "MSG", {"w": i}, 10 * i, 10 * i + 40)
        s.assert_edge(f"n{i}", f"n{i + 2 if i < 6 else 0}", "LIKE", {}, 5 * i,
                      5 * i + 20)
    s.ingest_events([{"src": f"n{i}", "dst": f"n{(i + 3) % 12}",
                      "rel_type": "MSG", "vt_s": 100 + i} for i in range(10)])
    yield s
    s.close()


# ---------------------------------------------------------------------------
# (a) the five classes, and every generated batch applies
# ---------------------------------------------------------------------------

def test_every_effect_class_and_placement_is_generated(substrate_store):
    """D6.3 is a **gate**, not an aspiration: *"a harness that injects only
    `correct()` would score a mechanism sound that is unsound for appends"* —
    the exact flaw §2.8 found already shipped in the tree."""
    rng = random.Random(11)
    sub = probe_substrate(substrate_store, rng=rng)
    target = Target(read_uids=("n0", "n1"), window=(0, 200))
    corrections = generate(substrate_store, sub, target, rng=rng)

    assert {c.cls for c in corrections} == set(CLASSES)
    assert {c.generator for c in corrections} == set(GENERATORS)
    assert {c.placement for c in corrections} == set(PLACEMENTS)
    assert len(corrections) >= 20, "the plan's §4.3 count is ~20 realized cells"


def test_the_carve_cell_is_populated_for_every_class_that_can_reach_it(
        substrate_store):
    """`outside-window` is the placement CE-4, FF-1 and RG-1 all live in, and
    the one where a value-arm-only mechanism returns `FRESH` and is wrong."""
    rng = random.Random(12)
    sub = probe_substrate(substrate_store, rng=rng)
    outside = [c for c in generate(substrate_store, sub,
                                   Target(read_uids=("n0",), window=(0, 200)),
                                   rng=rng)
               if c.placement.startswith("outside-window")]
    assert {c.cls for c in outside} >= {"A", "B", "C", "D"}


def test_the_new_identity_cell_names_identities_that_do_not_exist(
        substrate_store):
    """CE-1/CE-2/CE-3 — the class the row-touch baseline cannot see, because
    there are no rows to touch."""
    rng = random.Random(13)
    sub = probe_substrate(substrate_store, rng=rng)
    fresh = [c for c in generate(substrate_store, sub, Target(("n0",), (0, 200)),
                                 rng=rng)
             if c.placement == "new-identity"]
    assert fresh
    known = set(sub.uids)
    assert all(any(u not in known for u in c.identities) for c in fresh)


def test_every_generated_batch_applies_through_the_real_write_path(
        substrate_store, tmp_path):
    """A generator that produces an op the store refuses is producing a *cell
    that was never injected*, and the harness records it as such. Most of them
    must actually apply, or the matrix is a list of intentions."""
    rng = random.Random(14)
    sub = probe_substrate(substrate_store, rng=rng)
    corrections = generate(substrate_store, sub, Target(("n0", "n1"), (0, 200)),
                           rng=rng)
    applied, refused = 0, []
    for c in corrections:
        work = tmp_path / f"w{applied}{len(refused)}"
        shutil.copytree(substrate_store.path, work)
        s = tgms.open(work, backend="duckdb")
        try:
            s._write(list(c.ops))
            applied += 1
        except TgmsError as e:
            refused.append((c.generator, c.placement, type(e).__name__, str(e)))
        finally:
            s.close()
            shutil.rmtree(work, ignore_errors=True)
    assert applied >= len(corrections) - 2, f"too many refused: {refused}"


def test_class_e_really_is_two_ops_on_one_identity_in_one_batch(substrate_store):
    """L2.1. It emits **no footprint of its own** and the builder has no
    Class-E branch; injecting it anyway is the only way to show that the
    absence is a proof rather than a hole."""
    rng = random.Random(15)
    sub = probe_substrate(substrate_store, rng=rng)
    e = next(c for c in generate(substrate_store, sub, Target(("n0",), (0, 200)),
                                 rng=rng, generators=["e_within_batch"]))
    assert len(e.ops) == 2
    assert {o["uid"] for o in e.ops} == {e.identities[0]}
    assert e.ops[0]["vt_s"] == e.ops[1]["vt_s"]


def test_a_whole_store_scan_has_no_unread_identity(substrate_store):
    """An operator naming no identity scans everything, so `-read` draws from
    anywhere and `-unread` yields nothing — rather than a fabricated
    distinction that would put true positives in the true-negative cell."""
    rng = random.Random(16)
    sub = probe_substrate(substrate_store, rng=rng)
    scan = generate(substrate_store, sub, Target(read_uids=(), window=(0, 200)),
                    rng=rng)
    assert not [c for c in scan if c.placement.endswith("-unread")]
    assert [c for c in scan if c.placement == "in-window-read"]


def test_a_correction_is_fully_described_by_its_record(substrate_store):
    rng = random.Random(17)
    sub = probe_substrate(substrate_store, rng=rng)
    for c in generate(substrate_store, sub, Target(("n0",), (0, 200)), rng=rng):
        obj = c.to_json()
        assert obj["class"] in CLASSES
        assert obj["ops"] and all("op" in o for o in obj["ops"])
        assert obj["note"]


# ---------------------------------------------------------------------------
# (b)/(c)/(d) the harness
# ---------------------------------------------------------------------------

def _pristine(tmp_path, store) -> Path:
    """A copy of `store` for the sweep to read, so the fixture's own store is
    provably never the thing under injection."""
    dst = tmp_path / "pristine"
    if not dst.exists():
        shutil.copytree(store.path, dst)
    return dst


def _sweep(tmp_path, store) -> list:
    """One small sweep against a copy of `store`, through the real entry
    points."""
    pristine = _pristine(tmp_path, store)
    profile = bf.Profile("t", ((str(pristine), str(pristine)),), 4, 8, True, 3)
    return bf.sweep(profile)["trials"]


def test_the_sweep_produces_trials_and_the_d62_table_populates(substrate_store,
                                                               tmp_path):
    trials = _sweep(tmp_path, substrate_store)
    assert trials
    summary = bf.summarize(trials)
    assert summary["injected"] > 0
    # the refused/errored column is reported, never folded in
    assert "refused_or_errored" in summary
    assert summary["changed"] + summary["unchanged"] == summary["injected"]


def test_the_refused_or_errored_column_is_separated_from_both_metrics(
        substrate_store, tmp_path):
    """§1.6's rule and §8.8's hazard: admission is a function of *current*
    statistics, so a recompute can refuse where the original did not. Folding
    that into `unchanged` would inflate precision; folding it into `changed`
    would inflate the soundness denominator."""
    trials = _sweep(tmp_path, substrate_store)
    live = bf.scored(trials)
    assert all(t.outcome == bf.OUTCOME_OK for t in live)
    assert all(t.outcome != "NOT_INJECTED" for t in live)


def test_every_trial_carries_the_five_replay_fields(substrate_store, tmp_path):
    """M4 process rule 6: a freshness trial is `(store_digest_before,
    scope_digest, injected_batch_id, verdict, changed)` and is replayable from
    those five fields alone."""
    for t in bf.scored(_sweep(tmp_path, substrate_store)):
        assert t.store_digest_before and t.scope_digest and t.injected_batch_id
        assert t.verdict in ("fresh", "possibly-stale", "undecidable")
        assert isinstance(t.changed, bool)


def test_trials_are_isolated_so_corrections_never_compound(substrate_store,
                                                           tmp_path):
    """§8.6. Every trial must start from the pristine substrate: if trial *n*'s
    correction survived into *n+1*, the recorded `tt_q` and the log would no
    longer correspond and every classification after it would be suspect."""
    pristine = _pristine(tmp_path, substrate_store)
    ref = tgms.open(pristine, backend="duckdb")
    expected = ref.digest()
    ref.close()

    trials = bf.scored(_sweep(tmp_path, substrate_store))
    assert len({t.store_digest_before for t in trials}) == 1, \
        "trials saw more than one starting state"
    assert trials[0].store_digest_before == expected

    after = tgms.open(pristine, backend="duckdb")
    assert after.digest() == expected, "the sweep mutated the substrate"
    after.close()


def test_the_substrate_store_is_never_written_to(substrate_store, tmp_path):
    before = substrate_store.digest()
    _sweep(tmp_path, substrate_store)
    assert substrate_store.digest() == before


def test_both_controls_are_computed(substrate_store, tmp_path):
    """D6.4 makes the row-touch baseline a **required** control — it is the
    number that turns memo §15 from an assertion into a measurement. The
    all-`"*"` control is the precision floor."""
    trials = bf.scored(_sweep(tmp_path, substrate_store))
    assert all(t.rowtouch_verdict in ("fresh", "possibly-stale") for t in trials)
    assert all(t.top_verdict in ("fresh", "possibly-stale", "undecidable")
               for t in trials)


def test_the_all_star_control_is_never_fresher_than_the_real_derivation(
        substrate_store, tmp_path):
    """Sound by construction, and its precision is the floor: `"*"` everywhere
    matches every op the real term matches, and more."""
    for t in bf.scored(_sweep(tmp_path, substrate_store)):
        if t.verdict == "possibly-stale":
            assert t.top_verdict != "fresh", (
                f"{t.cell}/{t.generator}: the all-'*' control went FRESH where "
                f"the real derivation did not — the widening is not monotone")


def test_the_row_touch_baseline_is_blind_to_a_new_identity(substrate_store,
                                                           tmp_path):
    """§3's counterexamples predict exactly this: a correction on an identity
    the stored result has no row for is invisible to a row-touch rule, and
    §13.6 predicts it fails on a two-op batch over a five-node store."""
    trials = [t for t in bf.scored(_sweep(tmp_path, substrate_store))
              if t.placement == "new-identity"]
    assert trials
    assert all(t.rowtouch_verdict == "fresh" for t in trials)


# ---------------------------------------------------------------------------
# the population wiring
# ---------------------------------------------------------------------------

def test_resolve_entities_is_excluded_from_the_population_by_name():
    """§13.8.1's ruling, **wired** rather than assumed. Its only M4 appearance
    is the soundness suite's CE-6, which is a constructed counterexample and
    not a sample from a workload — that use must not leak into this
    population."""
    assert "resolve_entities" in bf.EXCLUDED_OPS
    assert "§13.8.1" in bf.EXCLUDED_OPS["resolve_entities"]
    assert "resolve_entities" not in bf.MEASURED_OPS
    sub = Substrate(("a", "b"), ("R",), 0, 100)
    assert not [c for c in bf.cells_for(sub, "R") if c.op == "resolve_entities"]
    assert bf.summarize([])["excluded_ops"] == bf.EXCLUDED_OPS


def test_compute_is_carried_as_the_empty_scope_control_only():
    """D5.3: `compute` has ∅ scope intrinsically. It is a control that ∅ ⇒
    `FRESH` forever, not a measured operator."""
    sub = Substrate(("a", "b"), ("R",), 0, 100)
    control = [c for c in bf.cells_for(sub, "R") if c.op == "compute"]
    assert len(control) == 1 and control[0].tier == "control"
    assert "compute" not in bf.MEASURED_OPS


def test_the_empty_scope_control_is_fresh_against_every_correction(
        substrate_store, tmp_path):
    trials = [t for t in bf.scored(_sweep(tmp_path, substrate_store))
              if t.op == "compute"]
    if trials:                     # only when the sweep reached the control cell
        assert all(t.verdict == "fresh" for t in trials)
        assert all(not t.changed for t in trials)


def test_only_the_two_real_stores_carry_the_precision_headline():
    """§4.2: a 22-entity fixture cannot support an honest precision number, so
    the fixture is soundness-only and says so in every table."""
    assert bf.PRECISION_STORES == {"bitcoinotc", "collegemsg"}
    assert "ldbc-fixture" not in bf.PRECISION_STORES


def test_the_denominator_floor_is_a_constant_not_a_computed_value():
    """§4.5's exit criterion is committed **before** the run. Adjusting a floor
    after seeing the population is how a meaningless zero gets published."""
    assert bf.FLOOR == {"changed": 300, "operators": 10, "classes": 4,
                        "outside_window": 50, "new_identity": 50,
                        "value_changed": 100}


def test_an_empty_population_reports_the_floor_as_unmet():
    """The honest answer when the matrix produces nothing is "not adequately
    measured", never a lowered floor."""
    summary = bf.summarize([])
    assert summary["floor"]["all_met"] is False
    assert summary["false_fresh"] == 0     # vacuously, and the floor says so


def test_value_changed_strips_version_identity_but_not_values():
    """D-M4g: a `vid`-only change counts as changed under D1.8, and D8.5
    freezes `vid` into the legacy operators' output. Both denominators are
    reported; this is the second one."""
    a = {"rows": [{"uid": "x", "vid": "v1", "props": {"p": 1}}]}
    b = {"rows": [{"uid": "x", "vid": "v2", "props": {"p": 1}}]}
    c = {"rows": [{"uid": "x", "vid": "v2", "props": {"p": 2}}]}
    assert bf._value_of(a) == bf._value_of(b)
    assert bf._value_of(a) != bf._value_of(c)


def test_the_receipt_carries_what_makes_a_run_replayable():
    r = bf.receipt(bf.PROFILES["ci"], {"trial_count": 0})
    assert r["seed"] == bf.PROFILES["ci"].seed
    assert r["profile"] == "ci"
    assert r["machine"]["python"]
    assert "generated" in r
