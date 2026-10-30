"""The storm harness's on-demand read mode (`Storm(refresh_on_read=k)`,
`scripts/bench_correction_storm.py --refresh-on-read k`).

Per batch, after the correction lands, the mode draws k registered names
(its own seeded RNG, without replacement) and gives each one
`check_artifact` and, when the check does not say fresh, one `refresh`,
timed as one interval; the registry is rolled back afterwards. This file
pins:

1. the mode changes nothing else: with it on, every row minus the new
   `on_demand_*` fields equals the same seed's row with it off (non-timing
   fields; sum and end-to-end mode, with and without the chain cache), and
   the registry file and event log end byte-identical;
2. with it off, rows and the summary carry none of the new keys;
3. the fields mean what they say: `on_demand_reads == min(k, registered)`,
   refreshes never exceed reads, the cache-miss field appears with the chain
   cache only, and the draw is deterministic per seed;
4. at k = every registered artifact, the mode refreshes exactly the
   artifacts the tgms-L1 arm invalidates (the pre-filter's non-survivors
   all check fresh);
5. k < 1 is refused, and k above the registered count is clamped;
6. the read policy (`refresh_on_read_policy`, CLI
   `--refresh-on-read-policy`): `"check"` (the default) writes rows with
   exactly the fields and bytes the mode had before the policy existed, and
   `"always"` (recompute on read) refreshes every drawn artifact without a
   single `check_artifact` call, changes nothing else, and adds only
   `on_demand_policy`;
7. `correction_apply_ms` (the correction's own write, outside every other
   interval) is on every row, mode on or off, and is the only field a
   mode-off row gained.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.eval.storm import Storm, summarize

BACKEND = "native"
N_ARTIFACTS, SEED, N_BATCHES = 15, 4, 4

ON_DEMAND_KEYS = {"on_demand_k", "on_demand_ms", "on_demand_reads", "on_demand_refreshes",
                  "on_demand_refresh_errors", "on_demand_check_cache_misses"}
POLICY_KEYS = {"on_demand_policy"}
#: the one field every row gained with the policy (a timing, never a digest input)
APPLY_KEY = "correction_apply_ms"


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
    base = tmp_path_factory.mktemp("storm-on-demand-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _non_timing(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()
           if k not in ("lookup_wall_ms", "global_recompute_wall_ms", "check_cache_misses",
                        APPLY_KEY)
           and k not in ON_DEMAND_KEYS and k not in POLICY_KEYS}
    out["arms"] = {
        arm: {k: (v is None if k == "ttf_ms" else v) for k, v in a.items()
              if k not in ("check_wall_ms", "refresh_wall_ms")}
        for arm, a in row["arms"].items()
    }
    return out


def _run(pristine: Path, *, k: int | None, measure_ttf: str = "sum",
         check_cache: str = "none", n_artifacts: int = N_ARTIFACTS,
         policy: str | None = None,
         ) -> tuple[list[dict[str, Any]], bytes, bytes, Storm]:
    extra = {} if policy is None else {"refresh_on_read_policy": policy}
    storm = Storm(_copy(pristine), n_artifacts=n_artifacts, seed=SEED, backend=BACKEND,
                  measure_ttf=measure_ttf, check_cache=check_cache, refresh_on_read=k, **extra)
    try:
        results = storm.run(N_BATCHES)
        rows = [r.to_json() for r in results]
        storm.summary = summarize(results)  # type: ignore[attr-defined]
        return (rows, storm.registry.path.read_bytes(),
                storm.store.eventlog.path.read_bytes(), storm)
    finally:
        storm.close()


@pytest.mark.parametrize("measure_ttf,check_cache", [
    ("sum", "none"), ("sum", "chain"), ("end-to-end", "chain"),
])
def test_the_mode_changes_nothing_but_its_own_fields(
    pristine_store: Path, measure_ttf: str, check_cache: str,
) -> None:
    off, reg_off, log_off, storm_off = _run(pristine_store, k=None, measure_ttf=measure_ttf,
                                            check_cache=check_cache)
    on, reg_on, log_on, _ = _run(pristine_store, k=5, measure_ttf=measure_ttf,
                                 check_cache=check_cache)
    assert len(off) == len(on) == N_BATCHES
    assert [_non_timing(r) for r in on] == [_non_timing(r) for r in off]
    # the arms' own cache-miss counts are untouched (the mode has its own cache)
    assert [r.get("check_cache_misses") for r in on] == [r.get("check_cache_misses") for r in off]
    assert reg_on == reg_off
    assert log_on == log_off
    for r_on, r_off in zip(on, off):
        assert set(r_on) - set(r_off) <= ON_DEMAND_KEYS
        assert APPLY_KEY in r_on and APPLY_KEY in r_off
        assert set(r_off) <= set(r_on)
    assert storm_off.on_demand_cache is None


def test_mode_off_rows_and_summary_carry_no_new_keys(pristine_store: Path) -> None:
    rows, _, _, storm = _run(pristine_store, k=None)
    for row in rows:
        assert not (set(row) & (ON_DEMAND_KEYS | POLICY_KEYS))
    assert "on_demand" not in storm.summary  # type: ignore[attr-defined]


def test_fields_are_counted_and_the_draw_is_seeded(pristine_store: Path) -> None:
    rows, _, _, storm = _run(pristine_store, k=5, check_cache="chain")
    n_reg = len(storm.artifacts)
    for row in rows:
        assert row["on_demand_k"] == 5
        assert row["on_demand_reads"] == min(5, n_reg)
        assert 0 <= row["on_demand_refreshes"] <= row["on_demand_reads"]
        assert row["on_demand_refresh_errors"] <= row["on_demand_refreshes"]
        assert row["on_demand_ms"] >= 0.0
        # its own cache, persisting across batches: one re-walk per log state
        assert row["on_demand_check_cache_misses"] in (0, 1)
    summary = storm.summary["on_demand"]  # type: ignore[attr-defined]
    assert summary["k"] == 5 and summary["batches"] == N_BATCHES
    assert summary["reads"] == sum(r["on_demand_reads"] for r in rows)
    again, _, _, _ = _run(pristine_store, k=5, check_cache="chain")
    assert ([(r["on_demand_reads"], r["on_demand_refreshes"]) for r in again]
            == [(r["on_demand_reads"], r["on_demand_refreshes"]) for r in rows])
    uncached, _, _, _ = _run(pristine_store, k=5)
    assert all("on_demand_check_cache_misses" not in r for r in uncached)


def test_reading_everything_refreshes_exactly_what_tgms_l1_invalidates(
    pristine_store: Path,
) -> None:
    rows, _, _, storm = _run(pristine_store, k=10_000, check_cache="chain")
    n_reg = len(storm.artifacts)
    for row in rows:
        assert row["on_demand_reads"] == n_reg  # clamped to the registered count
        assert row["on_demand_refreshes"] == row["arms"]["tgms-L1"]["invalidated_count"]


def test_k_below_one_is_refused(pristine_store: Path) -> None:
    with pytest.raises(ValueError, match="refresh_on_read"):
        Storm(_copy(pristine_store), n_artifacts=2, seed=0, backend=BACKEND, refresh_on_read=0)


def test_the_reads_of_one_interval_share_one_validation_per_log_state(
    pristine_store: Path,
) -> None:
    """The k reads run as one `ChainCache.burst` on the interval's own cache
    (failure ledger `check-fingerprint-per-check`): the whole-log digest is
    taken about once per batch, not once per read."""
    rows, _, _, storm = _run(pristine_store, k=10_000, check_cache="chain")
    reads = sum(r["on_demand_reads"] for r in rows)
    cache = storm.on_demand_cache
    assert cache is not None
    assert reads >= 4 * N_BATCHES
    assert cache.fingerprints <= 2 * N_BATCHES + 2
    assert cache.hits >= reads - N_BATCHES


# -- the read policy (recompute on read) ---------------------------------------


def _count_checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch `check_artifact` (as the storm module sees it) to count its
    calls into the returned one-element list, which the caller resets."""
    import tgms.eval.storm as storm_mod

    count = [0]
    real = storm_mod.check_artifact

    def counting(*a: Any, **kw: Any) -> Any:
        count[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(storm_mod, "check_artifact", counting)
    return count


@pytest.mark.parametrize("measure_ttf,check_cache", [("sum", "none"), ("end-to-end", "chain")])
def test_the_default_policy_writes_the_rows_the_mode_always_wrote(
    pristine_store: Path, measure_ttf: str, check_cache: str,
) -> None:
    implicit, reg_a, log_a, _ = _run(pristine_store, k=5, measure_ttf=measure_ttf,
                                     check_cache=check_cache)
    explicit, reg_b, log_b, _ = _run(pristine_store, k=5, measure_ttf=measure_ttf,
                                     check_cache=check_cache, policy="check")
    assert reg_a == reg_b and log_a == log_b
    for a, b in zip(implicit, explicit):
        # same keys in the same order, no policy key, every non-timing value equal
        assert list(a) == list(b)
        assert not (set(a) & POLICY_KEYS)
        for key in ("on_demand_k", "on_demand_reads", "on_demand_refreshes",
                    "on_demand_refresh_errors"):
            assert a[key] == b[key]
        assert _non_timing(a) == _non_timing(b)
    # and the keys are exactly the pre-policy ones plus the one apply timing
    off, _, _, _ = _run(pristine_store, k=None, measure_ttf=measure_ttf, check_cache=check_cache)
    assert set(implicit[0]) - set(off[0]) == ON_DEMAND_KEYS - (
        set() if check_cache == "chain" else {"on_demand_check_cache_misses"})


def test_always_refreshes_every_read_without_a_check_and_changes_nothing_else(
    pristine_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = _count_checks(monkeypatch)
    check, reg_c, log_c, _ = _run(pristine_store, k=5, check_cache="chain")
    n_check_policy = count[0]
    count[0] = 0
    always, reg_a, log_a, storm = _run(pristine_store, k=5, check_cache="chain",
                                       policy="always")
    n_always_policy = count[0]
    count[0] = 0
    _run(pristine_store, k=None, check_cache="chain")
    n_mode_off = count[0]
    n_reg = len(storm.artifacts)
    assert len(always) == len(check) == N_BATCHES
    assert reg_a == reg_c and log_a == log_c
    assert [_non_timing(r) for r in always] == [_non_timing(r) for r in check]
    for r in always:
        assert r["on_demand_policy"] == "always"
        assert r["on_demand_reads"] == min(5, n_reg)
        assert r["on_demand_refreshes"] == r["on_demand_reads"]  # every read, unconditionally
        assert r["on_demand_refresh_errors"] == 0
        assert r["on_demand_check_cache_misses"] == 0  # the interval never touches the cache
        assert set(r) - set(check[0]) == POLICY_KEYS
    # the same names are read: the draw does not depend on the policy
    assert [r["on_demand_reads"] for r in always] == [r["on_demand_reads"] for r in check]
    assert storm.summary["on_demand"]["policy"] == "always"  # type: ignore[attr-defined]
    # not one check_artifact call in the interval: only the tgms arms' own
    # (which run before it, and are the mode-off count) are left, while the
    # "check" policy adds one call per read
    assert n_always_policy == n_mode_off
    assert n_check_policy == n_mode_off + sum(r["on_demand_reads"] for r in check)


def test_always_at_every_artifact_is_never_cheaper_in_work_than_check(
    pristine_store: Path,
) -> None:
    check, _, _, storm = _run(pristine_store, k=10_000, check_cache="chain")
    always, _, _, _ = _run(pristine_store, k=10_000, check_cache="chain", policy="always")
    n_reg = len(storm.artifacts)
    for c, a in zip(check, always):
        assert a["on_demand_refreshes"] == n_reg
        assert c["on_demand_refreshes"] <= a["on_demand_refreshes"]


def test_the_policy_is_validated(pristine_store: Path) -> None:
    with pytest.raises(ValueError, match="refresh_on_read_policy"):
        Storm(_copy(pristine_store), n_artifacts=2, seed=0, backend=BACKEND,
              refresh_on_read=3, refresh_on_read_policy="sometimes")


# -- the correction's own timing -----------------------------------------------


def test_every_row_carries_the_correction_apply_timing(pristine_store: Path) -> None:
    for k in (None, 5):
        rows, _, _, _ = _run(pristine_store, k=k)
        for row in rows:
            assert isinstance(row[APPLY_KEY], float) and row[APPLY_KEY] > 0.0
    # a BatchResult built without the field keeps its bytes (no key, not null)
    from tgms.eval.storm import BatchResult

    assert BatchResult.__dataclass_fields__[APPLY_KEY].default is None


def test_the_apply_timing_is_zero_when_timing_is_off(pristine_store: Path) -> None:
    storm = Storm(_copy(pristine_store), n_artifacts=N_ARTIFACTS, seed=SEED, backend=BACKEND,
                  collect_timing=False)
    try:
        rows = [r.to_json() for r in storm.run(2)]
    finally:
        storm.close()
    assert all(r[APPLY_KEY] == 0.0 for r in rows)
