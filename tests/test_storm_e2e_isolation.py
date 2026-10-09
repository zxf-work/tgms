"""The end-to-end TTF intervals are isolated per arm (failure ledger
`storm-e2e-l1-interval-after-l0-refresh`).

`--measure-ttf end-to-end` times each tgms arm's own check -> refresh over
the names that arm nominated. Before the fix the tgms-L0 interval ran first
and republished every name it nominated — a superset of tgms-L1's — so the
tgms-L1 interval then found every one of its names fresh, refreshed nothing,
and recorded its check time as its time-to-fresh. `Storm.run_batch` now
rolls the registry back to the pre-interval state after each interval, and
records how many `refresh()` calls each interval made (`e2e_refresh_calls`).

The storm population is operator-kind artifacts with no recorded scan
regions, so on real data tgms-L1 nominates exactly tgms-L0's set (the
campaign's case; `test_equal_sets_...` below). The general case — L1 a
strict, non-empty subset of L0 — is forced with a test double for Level-1
narrowing: an *oracle-informed* tgms-L1 that re-executes a stale-verdict
artifact's operator and reports it fresh when its result did not change
(the narrowing an ideal L1 would achieve, never a false fresh). `refresh` is
wrapped with a fixed sleep so a refresh share is unmistakable in a timing.
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tgms
import tgms.eval.storm as storm_mod
from tgms.eval.storm import Storm

BACKEND = "native"
SLEEP_MS = 100.0
N_ARTIFACTS, SEED, N_BATCHES = 20, 0, 5


def _build_fixture(store_dir: Path) -> None:
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(1)
    uids = [f"n{i}" for i in range(24)]
    events = []
    for i in range(240):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst,
                       "rel_type": rng.choice(["FOLLOWS", "MESSAGES", "CITES"]),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("storm-e2e-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _slow_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    real = storm_mod.refresh

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(SLEEP_MS / 1000)
        return real(*args, **kwargs)

    monkeypatch.setattr(storm_mod, "refresh", slow)


def _oracle_informed_l1(storm: Storm, monkeypatch: pytest.MonkeyPatch) -> None:
    """Level-1 test double: tgms-L0's verdict, except that a stale artifact
    whose re-executed result equals the one the harness last observed is
    reported fresh — so L1 drops exactly L0's false stales."""
    from tgms.tools.server import ToolRouter

    real = storm_mod.check_artifact

    def check(record: Any, log: Any, *args: Any, level1: bool = True, **kw: Any) -> Any:
        verdict = real(record, log, *args, level1=level1, **kw)
        if not level1 or verdict.actionable_fresh:
            return verdict
        doc = json.loads((storm.store_dir / record.refresh["ref"]).read_text())
        env = ToolRouter(storm.store.adapter, tt_source=storm.store).call(
            doc["op"], doc.get("args") or {})
        if env.get("result_digest") == storm.artifacts[record.name].last_env["result_digest"]:
            return SimpleNamespace(actionable_fresh=True, refresh=None)
        return verdict

    monkeypatch.setattr(storm_mod, "check_artifact", check)


def test_l1_interval_refreshes_its_own_strict_subset(
    pristine_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _slow_refresh(monkeypatch)
    storm = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED,
                  backend=BACKEND, measure_ttf="end-to-end")
    _oracle_informed_l1(storm, monkeypatch)
    try:
        results = storm.run(N_BATCHES)
    finally:
        storm.close()

    strict = 0
    for r in results:
        l0, l1 = r.arms["tgms-L0"], r.arms["tgms-L1"]
        # every interval refreshes exactly what its own arm nominated — the
        # rolled-back registry hands each arm the same stale records
        # (the row's `invalidated` drops oracle-refused names; none expected)
        assert r.refused == ()
        assert l0.e2e_refresh_calls == len(l0.invalidated)
        assert l1.e2e_refresh_calls == len(l1.invalidated)
        if not (l1.invalidated and set(l1.invalidated) < set(l0.invalidated)):
            continue
        strict += 1
        assert l1.false_fresh == ()
        assert l1.e2e_refresh_calls > 0
        assert l1.ttf_ms is not None
        # the refresh share is in the L1 interval: at least the sleeps it made
        assert l1.ttf_ms >= l1.e2e_refresh_calls * SLEEP_MS
        # ... and the recorded TTF exceeds the check time by about that share
        # (pre-fix: ttf_ms ~= check_wall_ms, the collapsed ratio). Compared
        # against the injected sleep rather than refresh_wall_ms, which is
        # the oracle pass's own separate timing of the same refresh and
        # flakes when compared against the interval's independent timing.
        assert l1.ttf_ms - l1.check_wall_ms >= 0.9 * l1.e2e_refresh_calls * SLEEP_MS
        # the JSON row carries the count, so the error is detectable from rows
        assert r.to_json()["arms"]["tgms-L1"]["e2e_refresh_calls"] == l1.e2e_refresh_calls
    assert strict > 0, "fixture drift: no batch where tgms-L1 is a strict subset of tgms-L0"


def test_equal_sets_l1_interval_is_not_starved_by_l0(
    pristine_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The campaign's own shape: no Level-1 narrowing, L1's set == L0's.
    Pre-fix every such L1 interval made zero refresh calls."""
    _slow_refresh(monkeypatch)
    storm = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED,
                  backend=BACKEND, measure_ttf="end-to-end")
    try:
        results = storm.run(N_BATCHES)
    finally:
        storm.close()
    nonempty = 0
    for r in results:
        l0, l1 = r.arms["tgms-L0"], r.arms["tgms-L1"]
        assert l1.e2e_refresh_calls == l0.e2e_refresh_calls == len(l1.invalidated)
        if l1.invalidated:
            nonempty += 1
            assert l1.ttf_ms is not None
            assert l1.ttf_ms >= l1.e2e_refresh_calls * SLEEP_MS
    assert nonempty > 0


def test_end_to_end_mode_leaves_the_registry_as_sum_mode_does(pristine_store: Path) -> None:
    """The per-arm rollback means the intervals leave no trace in the
    registry: the oracle pass publishes onto the same state a sum-mode run
    does, so both modes end with byte-identical `artifacts.jsonl`."""
    storm_sum = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED,
                      backend=BACKEND, measure_ttf="sum")
    storm_e2e = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED,
                      backend=BACKEND, measure_ttf="end-to-end")
    try:
        rs, re_ = storm_sum.run(N_BATCHES), storm_e2e.run(N_BATCHES)
        assert len(rs) == len(re_) == N_BATCHES
        assert storm_sum.registry.path.read_bytes() == storm_e2e.registry.path.read_bytes()
        assert storm_sum.registry.checkpoint() == storm_e2e.registry.checkpoint()
        for a, b in zip(rs, re_):
            assert a.registry_bytes == b.registry_bytes
    finally:
        storm_sum.close()
        storm_e2e.close()


def test_sum_mode_rows_are_unchanged(pristine_store: Path) -> None:
    """Sum mode never runs the intervals: its rows carry exactly the
    pre-fix keys (no `e2e_refresh_calls`), and two runs of one seed agree
    byte for byte outside the timing fields."""
    pre_fix_arm_keys = {"arm", "invalidated_count", "check_wall_ms", "refresh_wall_ms",
                        "ttf_ms", "false_fresh_count", "false_fresh", "false_stale_count"}

    def run() -> list[dict[str, Any]]:
        storm = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED,
                      backend=BACKEND, measure_ttf="sum", collect_timing=False)
        try:
            return [r.to_json() for r in storm.run(N_BATCHES)]
        finally:
            storm.close()

    rows_a, rows_b = run(), run()
    for row in rows_a:
        for arm in row["arms"].values():
            assert set(arm) == pre_fix_arm_keys
    assert json.dumps(rows_a, sort_keys=True) == json.dumps(rows_b, sort_keys=True)
