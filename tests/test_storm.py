"""The Correction Storm driver (Lane C, C1+C3;
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
from tgms.eval.storm import ARMS, Storm

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
