"""`scripts/bench_correction_storm.py --leaf-scopes {current,storm-v1}`.

`storm-v1` is the lineage the first storm campaign ran on: of the harness's 14
`TEMPLATES`, only `entity_history`, `neighborhood_evolution` and
`aggregate_events` had a derived scope (`tgms.tgir.leaves.LEAF_SCOPES` at
commit `8962b78`); the other ten operators were on the coarse `"*"` scope and
`compute` on the empty one. The flag removes the ten later entries in-process
before the population is registered. This file pins:

1. `storm-v1` removes exactly ten entries and keeps exactly the three, and the
   population then reports the ten templates' artifacts as all-top
   (`narrowing_coverage`), with the three kept and `compute` unchanged;
2. `current` removes nothing and a run under it is byte-identical to a run
   without the flag (rows other than timings, and the registry file);
3. the setting is recorded in the manifest config only off the default, with
   the removed count, so a default manifest keeps its exact config keys.

Each test restores `LEAF_SCOPES` (the flag mutates the module's table).
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
from tgms.eval.storm import Storm, narrowing_coverage
from tgms.tgir import leaves

BACKEND = "native"
ROOT = Path(__file__).resolve().parent.parent
N_ARTIFACTS, SEED, N_BATCHES = 60, 3, 3

KEPT = {"entity_history", "neighborhood_evolution", "aggregate_events"}
REMOVED = {
    "count_temporal_motifs", "find_temporal_motif_instances", "temporal_reachability",
    "temporal_paths", "burst_detection", "graph_metric_timeseries", "co_active",
    "diff_snapshots", "version_history", "snapshot_subgraph",
}


def _load_bench() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bench_correction_storm", ROOT / "scripts" / "bench_correction_storm.py")
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_correction_storm", bench)
    spec.loader.exec_module(bench)
    return bench


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
    base = tmp_path_factory.mktemp("storm-leaf-scopes-fixture")
    _build_fixture(base)
    return base


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A private copy of `LEAF_SCOPES` installed for the test, so a removal
    never leaks into another test; the original is back afterwards."""
    private = dict(leaves.LEAF_SCOPES)
    monkeypatch.setattr(leaves, "LEAF_SCOPES", private)
    return private


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _non_timing(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()
           if k not in ("lookup_wall_ms", "global_recompute_wall_ms", "correction_apply_ms")}
    out["arms"] = {
        arm: {k: (v is None if k == "ttf_ms" else v) for k, v in a.items()
              if k not in ("check_wall_ms", "refresh_wall_ms")}
        for arm, a in row["arms"].items()
    }
    return out


def _run(pristine: Path) -> tuple[list[dict[str, Any]], bytes, dict[str, Any]]:
    storm = Storm(_copy(pristine), n_artifacts=N_ARTIFACTS, seed=SEED, backend=BACKEND)
    try:
        coverage = narrowing_coverage(storm.registry, storm.artifacts)
        rows = [r.to_json() for r in storm.run(N_BATCHES)]
        return rows, storm.registry.path.read_bytes(), coverage
    finally:
        storm.close()


def test_the_table_is_what_the_flag_assumes(table: dict[str, Any]) -> None:
    """The ten later entries and the three kept ones are the table's whole
    content: if a derivation is added, this fails and the flag is revisited."""
    assert set(table) == KEPT | REMOVED
    assert len(REMOVED) == 10


def test_storm_v1_removes_ten_entries_and_marks_their_artifacts_all_top(
    pristine_store: Path, table: dict[str, Any],
) -> None:
    bench = _load_bench()
    _, _, cov_current = _run(pristine_store)
    removed = bench.apply_leaf_scopes("storm-v1")
    assert len(removed) == 10 and set(removed) == REMOVED
    assert set(table) == KEPT
    _, _, cov_v1 = _run(pristine_store)

    counts = cov_v1["per_template_counts"]
    assert counts == cov_current["per_template_counts"]  # same population, drawn the same
    assert set(counts) >= KEPT | REMOVED | {"compute"}
    n_removed_ops = sum(counts.get(op, 0) for op in REMOVED)
    assert n_removed_ops > 0
    # with today's table no template is on the coarse scope; with storm-v1 the
    # ten removed templates' artifacts are, and nothing else is
    assert cov_current["n_all_top_term"] == 0
    assert cov_v1["n_all_top_term"] == n_removed_ops
    assert cov_v1["n_empty_scope"] == cov_current["n_empty_scope"] == counts["compute"]
    narrow = cov_v1["n_artifacts"] - cov_v1["n_all_top_term"] - cov_v1["n_empty_scope"]
    assert narrow == sum(counts.get(op, 0) for op in KEPT)


def test_current_removes_nothing_and_changes_no_row(
    pristine_store: Path, table: dict[str, Any],
) -> None:
    bench = _load_bench()
    before = dict(table)
    rows_a, reg_a, cov_a = _run(pristine_store)
    assert bench.apply_leaf_scopes("current") == []
    assert table == before
    rows_b, reg_b, cov_b = _run(pristine_store)
    assert [_non_timing(r) for r in rows_b] == [_non_timing(r) for r in rows_a]
    assert reg_b == reg_a
    assert cov_b == cov_a


def test_unknown_mode_refuses(table: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="leaf_scopes"):
        _load_bench().apply_leaf_scopes("coarse")
    assert set(table) == KEPT | REMOVED


def test_cli_records_the_setting_only_off_the_default(
    pristine_store: Path, tmp_path: Path, table: dict[str, Any],
) -> None:
    bench = _load_bench()
    out_cur = tmp_path / "cur"
    assert bench.main(["--store", str(pristine_store), "--n-artifacts", "12", "--batches", "2",
                       "--seed", "1", "--backend", BACKEND, "--out", str(out_cur)]) in (0, None)
    cfg = json.loads((out_cur / f"storm-{pristine_store.name}-1.json").read_text())["config"]
    assert not any(k.startswith("leaf_scopes") for k in cfg)

    out_v1 = tmp_path / "v1"
    assert bench.main(["--store", str(pristine_store), "--n-artifacts", "12", "--batches", "2",
                       "--seed", "1", "--backend", BACKEND, "--leaf-scopes", "storm-v1",
                       "--out", str(out_v1)]) in (0, None)
    manifest = json.loads((out_v1 / f"storm-{pristine_store.name}-1.json").read_text())
    cfg = manifest["config"]
    assert cfg["leaf_scopes"] == "storm-v1"
    assert cfg["leaf_scopes_removed"] == 10
    assert sorted(cfg["leaf_scopes_removed_ops"]) == sorted(REMOVED)
    assert sorted(cfg["leaf_scopes_kept_ops"]) == sorted(KEPT)
    cov = manifest["summary"]["narrowing_coverage"]
    assert cov["n_all_top_term"] > 0
