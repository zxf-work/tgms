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
5. k < 1 is refused, and k above the registered count is clamped.
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
           if k not in ("lookup_wall_ms", "global_recompute_wall_ms", "check_cache_misses")
           and k not in ON_DEMAND_KEYS}
    out["arms"] = {
        arm: {k: (v is None if k == "ttf_ms" else v) for k, v in a.items()
              if k not in ("check_wall_ms", "refresh_wall_ms")}
        for arm, a in row["arms"].items()
    }
    return out


def _run(pristine: Path, *, k: int | None, measure_ttf: str = "sum",
         check_cache: str = "none", n_artifacts: int = N_ARTIFACTS,
         ) -> tuple[list[dict[str, Any]], bytes, bytes, Storm]:
    storm = Storm(_copy(pristine), n_artifacts=n_artifacts, seed=SEED, backend=BACKEND,
                  measure_ttf=measure_ttf, check_cache=check_cache, refresh_on_read=k)
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
        assert set(r_off) <= set(r_on)
    assert storm_off.on_demand_cache is None


def test_mode_off_rows_and_summary_carry_no_new_keys(pristine_store: Path) -> None:
    rows, _, _, storm = _run(pristine_store, k=None)
    for row in rows:
        assert not (set(row) & ON_DEMAND_KEYS)
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
