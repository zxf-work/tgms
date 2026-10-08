"""The storm harness's explicit check-path cache setting (failure ledger
`check-walk-parses-every-record`).

`check()` re-walks the whole event log on every call (D13.24 steps 5-6),
parsing every record to read its `tt`. `Storm(check_cache="chain")` (and
`scripts/bench_correction_storm.py --check-cache chain`) hands every
`check_artifact` call one `ChainCache`. This file pins:

1. the cache changes no verdict and no published record: the non-timing
   rows and `artifacts.jsonl` of a `"chain"` run equal a `"none"` run's, in
   sum and end-to-end mode;
2. the setting is recorded — on every row that uses it (with the batch's
   miss count) and in the CLI manifest's `config` — and a `"none"` row keeps
   exactly its pre-setting keys;
3. the cache is actually used (one re-walk per log state, hits otherwise);
4. the library default stays opt-in: `Storm` defaults to `"none"`.
"""

from __future__ import annotations

import importlib.util
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.eval.storm import CHECK_CACHES, Storm

BACKEND = "native"
ROOT = Path(__file__).resolve().parent.parent
N_ARTIFACTS, SEED, N_BATCHES = 15, 4, 4

#: `BatchResult.to_json()`'s top-level keys before the setting existed.
PRE_SETTING_ROW_KEYS = {
    "batch_index", "correction_class", "correction_generator", "correction_placement",
    "n_registered", "intersects_calls", "candidate_survivors", "lookup_wall_ms",
    "global_recompute_wall_ms", "changed_count", "changed", "refused_count", "arms",
    "ttf_mode", "age_vt_meaningful", "log_bytes", "log_records", "registry_bytes",
    "correction_identities", "correction_vt", "correction_disc", "correction_eid",
}


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
    base = tmp_path_factory.mktemp("storm-check-cache-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _non_timing(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()
           if k not in ("lookup_wall_ms", "global_recompute_wall_ms",
                        "check_cache", "check_cache_misses")}
    out["arms"] = {
        arm: {k: (v is None if k == "ttf_ms" else v) for k, v in a.items()
              if k not in ("check_wall_ms", "refresh_wall_ms")}
        for arm, a in row["arms"].items()
    }
    return out


def _run(pristine: Path, check_cache: str, measure_ttf: str
         ) -> tuple[list[dict[str, Any]], bytes, Storm]:
    storm = Storm(_copy(pristine), n_artifacts=N_ARTIFACTS, seed=SEED, backend=BACKEND,
                  measure_ttf=measure_ttf, check_cache=check_cache)
    try:
        rows = [r.to_json() for r in storm.run(N_BATCHES)]
        return rows, storm.registry.path.read_bytes(), storm
    finally:
        storm.close()


@pytest.mark.parametrize("measure_ttf", ["sum", "end-to-end"])
def test_chain_cache_changes_no_verdict_and_no_published_record(
    pristine_store: Path, measure_ttf: str,
) -> None:
    rows_none, reg_none, _ = _run(pristine_store, "none", measure_ttf)
    rows_chain, reg_chain, storm = _run(pristine_store, "chain", measure_ttf)
    assert len(rows_none) == len(rows_chain) == N_BATCHES
    assert [_non_timing(r) for r in rows_chain] == [_non_timing(r) for r in rows_none]
    assert reg_chain == reg_none
    # used, not merely constructed: the cache served hits, and missed at most
    # once per log state (each batch's own correction append is one state)
    assert storm.chain_cache is not None
    assert storm.chain_cache.hits > 0
    assert storm.chain_cache.misses <= N_BATCHES + 1


def test_the_setting_is_recorded_on_rows_that_use_it(pristine_store: Path) -> None:
    rows_none, _, storm_none = _run(pristine_store, "none", "sum")
    rows_chain, _, _ = _run(pristine_store, "chain", "sum")
    assert storm_none.chain_cache is None
    for row in rows_none:
        assert set(row) == PRE_SETTING_ROW_KEYS
    for row in rows_chain:
        assert set(row) == PRE_SETTING_ROW_KEYS | {"check_cache", "check_cache_misses"}
        assert row["check_cache"] == "chain"
        # the batch's correction append changes the log once: at most one
        # re-walk per batch, every later check that batch a hit
        assert row["check_cache_misses"] in (0, 1)
    assert sum(row["check_cache_misses"] for row in rows_chain) >= 1


def test_library_default_stays_uncached_and_unknown_settings_refuse(
    pristine_store: Path,
) -> None:
    assert CHECK_CACHES == ("none", "chain")
    storm = Storm(_copy(pristine_store), n_artifacts=2, seed=0, backend=BACKEND)
    try:
        assert storm.check_cache == "none"
        assert storm.chain_cache is None
    finally:
        storm.close()
    with pytest.raises(ValueError, match="check_cache"):
        Storm(_copy(pristine_store), n_artifacts=0, seed=0, backend=BACKEND,
              check_cache="sometimes")


def test_cli_records_the_setting_in_the_manifest_config(
    pristine_store: Path, tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "bench_correction_storm", ROOT / "scripts" / "bench_correction_storm.py")
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_correction_storm", bench)
    spec.loader.exec_module(bench)

    out = tmp_path / "out"
    rc = bench.main(["--store", str(pristine_store), "--n-artifacts", "6",
                     "--batches", "2", "--seed", "1", "--backend", BACKEND,
                     "--check-cache", "chain", "--out", str(out)])
    assert rc in (0, None)
    manifest = json.loads((out / f"storm-{pristine_store.name}-1.json").read_text())
    assert manifest["config"]["check_cache"] == "chain"
    rows = [json.loads(line) for line in
            (out / f"storm-{pristine_store.name}-1-rows.jsonl").read_text().splitlines()]
    assert rows and all(r["check_cache"] == "chain" for r in rows)
