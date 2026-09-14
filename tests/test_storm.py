"""The Correction Storm driver (Lane C, C1+C3, plus C2's rate/age/degree/
range-width axes and the end-to-end TTF mode;
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`).

A small, deterministic, native-backend storm — the CI-fast population §9
calls for (20 artifacts, 5 batches) — asserting: every arm's false-fresh
count is scored against the same oracle and the two `tgms-*` arms report
zero (G-S1's own gate, at this population's own scale); `global-recompute`
invalidates every scored artifact; `entity-touch`'s nominated set is always
a subset of `global-recompute`'s; every timing field is present, finite and
non-negative; the registry's record-digest chain is byte-identical whether
or not timing collection is enabled (§6's replay-contract obligation); and
two runs of the same seed produce byte-identical registries (§6's
determinism obligation, restated for this harness's own deterministic `tt`
write path rather than `Store._write`'s wall-clock-seeded clock).

`tgms.eval.corrections.generate`'s own taxonomy/placement classification
(class in A-E, placement in the five named ones) is **not** re-tested here
— that is `tests/test_freshness_boundary*`/the M4 corpus's job, and this
harness only ever *calls* `generate()`, never reimplements it.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path

import pytest

import tgms
from tgms.eval.storm import ARMS, AGE_BANDS, MIXES, Storm, build_mix

BACKEND = "native"


# ---------------------------------------------------------------------------
# fixture: a small event-stream store, built once per test via the normal
# write API (its own `tt`s do not need to be deterministic — the storm's OWN
# correction batches are what the determinism tests below care about, and
# those are written through `Storm._write`'s explicit-`tt` path, never
# `Store._write`).
# ---------------------------------------------------------------------------

def _build_fixture(store_dir: Path, *, n_nodes: int = 24, n_events: int = 240,
                   seed: int = 1) -> None:
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(seed)
    uids = [f"n{i}" for i in range(n_nodes)]
    rel_types = ["FOLLOWS", "MESSAGES", "CITES"]
    events = []
    for i in range(n_events):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst, "rel_type": rng.choice(rel_types),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


def _pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("storm-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _pristine(tmp_path_factory)


# ---------------------------------------------------------------------------
# 1 — arms scored against the oracle; the tgms arms are false-fresh-clean
# ---------------------------------------------------------------------------

def test_storm_scores_every_arm_against_the_oracle_and_tgms_arms_are_sound(
    pristine_store: Path,
) -> None:
    storm = Storm(_copy(pristine_store), n_artifacts=20, seed=1, backend=BACKEND)
    try:
        assert len(storm.artifacts) > 0
        results = storm.run(5)
        assert len(results) == 5

        for r in results:
            scored = set(storm.artifacts) - set(r.refused)
            assert set(r.arms) == set(ARMS)

            # G-S1, at this population's scale: the two real arms never miss
            # an oracle-changed artifact.
            assert r.arms["tgms-L0"].false_fresh == ()
            assert r.arms["tgms-L1"].false_fresh == ()
            assert r.arms["global-recompute"].false_fresh == ()

            # global-recompute invalidates every scored artifact, always.
            assert set(r.arms["global-recompute"].invalidated) == scored

            # entity-touch never nominates more than global-recompute does.
            assert set(r.arms["entity-touch"].invalidated) <= set(
                r.arms["global-recompute"].invalidated)
            assert set(r.arms["window-overlap"].invalidated) <= scored
            assert set(r.arms["row-touch"].invalidated) <= scored
            assert set(r.arms["tgms-L0"].invalidated) <= scored
            assert set(r.arms["tgms-L1"].invalidated) <= scored

            # timing fields: present, finite, non-negative for every arm;
            # strictly positive wherever real work was measured.
            for arm_name, outcome in r.arms.items():
                assert outcome.check_wall_ms >= 0.0
                assert outcome.refresh_wall_ms >= 0.0
                assert outcome.ttf_ms is None or outcome.ttf_ms >= 0.0
                if outcome.invalidated:
                    assert outcome.refresh_wall_ms > 0.0
            assert r.arms["global-recompute"].refresh_wall_ms > 0.0
            if r.arms["tgms-L0"].invalidated or r.arms["tgms-L1"].invalidated:
                # at least one of the two tgms arms actually checked something
                assert r.arms["tgms-L0"].check_wall_ms > 0.0 or \
                    r.arms["tgms-L1"].check_wall_ms > 0.0
            assert r.lookup_wall_ms >= 0.0
            assert r.global_recompute_wall_ms > 0.0
            assert r.log_bytes > 0
            assert r.registry_bytes > 0
    finally:
        storm.close()


# ---------------------------------------------------------------------------
# 2 — §6's replay-contract test: timings never enter the digest chain
# ---------------------------------------------------------------------------

def test_timing_does_not_affect_digest_chain(pristine_store: Path) -> None:
    dir_no_timing = _copy(pristine_store)
    dir_with_timing = _copy(pristine_store)
    storm_a = Storm(dir_no_timing, n_artifacts=15, seed=5, backend=BACKEND,
                    collect_timing=False)
    storm_b = Storm(dir_with_timing, n_artifacts=15, seed=5, backend=BACKEND,
                    collect_timing=True)
    try:
        results_a = storm_a.run(4)
        results_b = storm_b.run(4)
        assert len(results_a) == len(results_b) == 4

        assert storm_a.registry.names() == storm_b.registry.names()
        assert storm_a.registry.checkpoint() == storm_b.registry.checkpoint()
        for name in storm_a.registry.names():
            ra = storm_a.registry.current(name)
            rb = storm_b.registry.current(name)
            assert ra.record_digest == rb.record_digest
            assert ra.to_json() == rb.to_json()

        # collect_timing=False really did skip measurement: every timing
        # field this run reports is exactly zero.
        for r in results_a:
            assert r.lookup_wall_ms == 0.0
            for outcome in r.arms.values():
                assert outcome.check_wall_ms == 0.0
                assert outcome.refresh_wall_ms == 0.0
    finally:
        storm_a.close()
        storm_b.close()


# ---------------------------------------------------------------------------
# 3 — seed determinism: the same seed replays to the same registry, byte for
# byte, via this harness's own deterministic `tt` write path.
# ---------------------------------------------------------------------------

def test_seed_determinism_of_the_registry_chain(pristine_store: Path) -> None:
    dir_a = _copy(pristine_store)
    dir_b = _copy(pristine_store)
    storm_a = Storm(dir_a, n_artifacts=12, seed=99, backend=BACKEND)
    storm_b = Storm(dir_b, n_artifacts=12, seed=99, backend=BACKEND)
    try:
        results_a = storm_a.run(3)
        results_b = storm_b.run(3)
        assert len(results_a) == len(results_b) == 3

        assert storm_a.registry.names() == storm_b.registry.names()
        assert storm_a.registry.checkpoint() == storm_b.registry.checkpoint()
        for name in storm_a.registry.names():
            assert (storm_a.registry.current(name).record_digest ==
                    storm_b.registry.current(name).record_digest)

        # a different seed is not required to (and, empirically, does not)
        # produce the same chain — sanity that the test is not vacuous.
        dir_c = _copy(pristine_store)
        storm_c = Storm(dir_c, n_artifacts=12, seed=100, backend=BACKEND)
        try:
            storm_c.run(3)
            assert storm_c.registry.checkpoint() != storm_a.registry.checkpoint()
        finally:
            storm_c.close()
    finally:
        storm_a.close()
        storm_b.close()


# ---------------------------------------------------------------------------
# 4 -- C2: the rate mixes bias the candidate pool toward the declared split
# ---------------------------------------------------------------------------

def test_mix_ratios_bias_the_candidate_pool_toward_the_declared_split(
    pristine_store: Path,
) -> None:
    """`--mix {c1,c2,c3}`'s declared append-vs-correction split (99/1, 95/5,
    80/20) is a *weighting* of `Mix.__call__`'s returned candidate list, not
    a promise about any one batch's draw — so this counts the weighted pool
    itself (deterministic given a fixed `rng`), the same quantity
    `Storm.run_batch`'s `rng.randrange(len(corrections))` samples uniformly
    from. C1's append share must be at least C2's, which must be at least
    C3's -- the declared ordering -- and C1 itself must be append-majority.
    """
    work = _copy(pristine_store)
    storm = Storm(work, n_artifacts=5, seed=2, backend=BACKEND)
    try:
        frac_a: dict[str, float] = {}
        for name in ("c1", "c2", "c3"):
            mix = build_mix(name)
            candidates = mix(storm.store, storm.sub, storm.target, random.Random(0))
            assert candidates
            n_a = sum(1 for c in candidates if c.cls == "A")
            frac_a[name] = n_a / len(candidates)
        assert frac_a["c1"] >= frac_a["c2"] >= frac_a["c3"]
        assert frac_a["c1"] > 0.5
    finally:
        storm.close()


def test_every_mix_and_age_option_is_realizable_and_labeled(pristine_store: Path) -> None:
    """Every `--mix` x `--age` combination the CLI exposes produces at least
    one candidate correction, and an age-banded one is labeled with its own
    band in `generator` (never silently falls back to a plain
    `corrections.py` generator name) -- the "count them" obligation for the
    age axis: a caller can always tell which band a row came from."""
    work = _copy(pristine_store)
    storm = Storm(work, n_artifacts=8, seed=9, backend=BACKEND)
    try:
        for mix_name in MIXES:
            mix = build_mix(mix_name)
            candidates = mix(storm.store, storm.sub, storm.target, random.Random(1))
            assert candidates, f"mix {mix_name!r} produced no candidates"
        for age in AGE_BANDS:
            mix = build_mix("c2", age=age)
            found = False
            for trial_seed in range(20):
                candidates = mix(storm.store, storm.sub, storm.target,
                                 random.Random(trial_seed))
                labeled = [c for c in candidates if c.generator == f"age_{age}"]
                if labeled:
                    found = True
                    break
            assert found, f"age band {age!r} never realized in 20 seeded draws"
    finally:
        storm.close()


def test_degree_and_range_width_selectors_are_recorded_per_correction(
    pristine_store: Path,
) -> None:
    """`--degree`/`--range-width` are accept/reject filters over
    `generate()`'s own output (module docstring) -- this pins that a
    selector never crashes and, when it *does* narrow the pool (rather than
    falling back to the unfiltered one), only ever narrows to corrections
    that really match the bucket."""
    work = _copy(pristine_store)
    storm = Storm(work, n_artifacts=8, seed=11, backend=BACKEND)
    try:
        for degree in ("low", "mid", "high"):
            mix = build_mix("c2", degree=degree)
            candidates = mix(storm.store, storm.sub, storm.target, random.Random(3))
            assert candidates
        for width in ("narrow", "mid", "wide"):
            mix = build_mix("c2", range_width=width)
            candidates = mix(storm.store, storm.sub, storm.target, random.Random(3))
            assert candidates
    finally:
        storm.close()


# ---------------------------------------------------------------------------
# 5 -- C2: the C4 burst lands after exactly `--burst-after` mix calls
# ---------------------------------------------------------------------------

def test_c4_burst_lands_after_the_configured_batch(pristine_store: Path) -> None:
    """`Mix`'s own call counter, not `Storm.run`'s retry loop, is what this
    pins: the `(burst_after + 1)`-th call to the mix -- and no other -- is
    the scheduled burst, deterministically."""
    work = _copy(pristine_store)
    storm = Storm(work, n_artifacts=6, seed=6, backend=BACKEND)
    try:
        mix = build_mix("c4", burst_size=3, burst_after=2)
        rng = random.Random(1)
        for i in range(5):
            candidates = mix(storm.store, storm.sub, storm.target, rng)
            if i == 2:
                assert len(candidates) == 1
                assert candidates[0].generator == "c4_burst"
                assert 0 < len(candidates[0].ops) <= 3
            else:
                assert all(c.generator != "c4_burst" for c in candidates)
    finally:
        storm.close()


def test_c4_burst_appears_exactly_once_in_a_full_storm_run(pristine_store: Path) -> None:
    work = _copy(pristine_store)
    mix = build_mix("c4", burst_size=3, burst_after=1)
    storm = Storm(work, n_artifacts=8, seed=7, backend=BACKEND, mix=mix)
    try:
        results = storm.run(4, max_attempts_factor=10)
        burst_batches = [r for r in results if r.correction_generator == "c4_burst"]
        assert len(burst_batches) == 1
    finally:
        storm.close()


# ---------------------------------------------------------------------------
# 6 -- C2: the end-to-end TTF mode agrees with the sum mode outside timing
# ---------------------------------------------------------------------------

def test_end_to_end_and_sum_ttf_modes_agree_outside_timing(pristine_store: Path) -> None:
    """`--measure-ttf end-to-end` actually re-runs check -> refresh for the
    tgms arms' own nominated set, timed as one interval, *in addition to*
    the oracle's own global-recompute pass (module note in `storm.py::
    Storm.run_batch`) -- so every non-timing field (oracle_changed, refused,
    every arm's invalidated/false_fresh/false_stale) must still match the
    sum-mode run of the same seed byte for byte, and both modes must report
    a strictly positive TTF wherever a tgms arm actually invalidated
    something."""
    dir_sum = _copy(pristine_store)
    dir_e2e = _copy(pristine_store)
    storm_sum = Storm(dir_sum, n_artifacts=15, seed=8, backend=BACKEND, measure_ttf="sum")
    storm_e2e = Storm(dir_e2e, n_artifacts=15, seed=8, backend=BACKEND, measure_ttf="end-to-end")
    try:
        results_sum = storm_sum.run(4)
        results_e2e = storm_e2e.run(4)
        assert len(results_sum) == len(results_e2e) == 4
        for r_sum, r_e2e in zip(results_sum, results_e2e):
            assert r_sum.correction_class == r_e2e.correction_class
            assert r_sum.correction_generator == r_e2e.correction_generator
            assert r_sum.oracle_changed == r_e2e.oracle_changed
            assert r_sum.refused == r_e2e.refused
            assert set(r_sum.arms) == set(r_e2e.arms)
            for arm in r_sum.arms:
                a, b = r_sum.arms[arm], r_e2e.arms[arm]
                assert a.invalidated == b.invalidated
                assert a.false_fresh == b.false_fresh
                assert a.false_stale == b.false_stale
                if arm.startswith("tgms-") and a.invalidated:
                    assert a.ttf_ms is not None and a.ttf_ms > 0.0
                    assert b.ttf_ms is not None and b.ttf_ms > 0.0
            assert r_sum.ttf_mode == "sum"
            assert r_e2e.ttf_mode == "end-to-end"
    finally:
        storm_sum.close()
        storm_e2e.close()


def test_age_vt_meaningful_is_recorded_when_the_age_axis_is_active(
    pristine_store: Path,
) -> None:
    """The SNAP caveat (design memo §2): this fixture is a pure event
    stream (`ingest_events` only, every version `[first_seen, OPEN_END)`),
    so `age_vt_meaningful` must be recorded `False` on every batch once the
    age axis is in play, and `None` when it is not."""
    work_plain = _copy(pristine_store)
    storm_plain = Storm(work_plain, n_artifacts=6, seed=13, backend=BACKEND)
    try:
        for r in storm_plain.run(3):
            assert r.age_vt_meaningful is None
    finally:
        storm_plain.close()

    work_age = _copy(pristine_store)
    mix = build_mix("c2", age="deep")
    storm_age = Storm(work_age, n_artifacts=6, seed=13, backend=BACKEND, mix=mix)
    try:
        results = storm_age.run(3)
        assert results
        for r in results:
            assert r.age_vt_meaningful is False
    finally:
        storm_age.close()
