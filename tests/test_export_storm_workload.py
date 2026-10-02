"""Unit tests for `scripts/export_storm_workload.py` (Lane X1, workload
export; `docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md` §1).

Everything here runs against a tiny synthetic store built in `tmp_path` --
per the standing rule that the laptop runs code and unit tests only, never a
real campaign store or a real-scale harness run (see memory
`experiments-remote-only`). No committed grid file is mutated; one test
reads the real committed grid files to check `load_grid_cells()`'s parsing,
but never runs a cell from it.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

import tgms

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "export_storm_workload.py"
_spec = importlib.util.spec_from_file_location("export_storm_workload", MODULE_PATH)
export_storm_workload = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("export_storm_workload", export_storm_workload)
assert _spec.loader is not None
_spec.loader.exec_module(export_storm_workload)

BACKEND = "native"


def _build_fixture_store(store_dir: Path, *, n_nodes: int = 24, n_events: int = 240,
                         seed: int = 1) -> None:
    """The same tiny deterministic fixture `tests/test_storm.py` builds --
    restated here rather than imported, matching that module's own stated
    preference for a self-contained fixture."""
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


@pytest.fixture()
def tiny_cell_spec() -> "export_storm_workload.CellSpec":
    return export_storm_workload.CellSpec(
        cell_id="tiny-c1-none-n5-s1", store="tiny", mix="c1", age=None,
        n_artifacts=5, seed=1, batches=2,
        committed_dataset_digest=None, committed_log_bytes=None,
        committed_n_registered=None, committed_per_template_counts=None,
        committed_changed=None, committed_correction_meta=None,
        committed_refused_count=None, source="extra",
    )


# ---------------------------------------------------------------------------
# cell_id / CellSpec plumbing
# ---------------------------------------------------------------------------

def test_cell_id_format() -> None:
    assert export_storm_workload.cell_id("collegemsg", "c3", None, 1000, 0) == (
        "collegemsg-c3-none-n1000-s0")
    assert export_storm_workload.cell_id("synth-iv-60k", "c4", "deep", 1000, 2) == (
        "synth-iv-60k-c4-deep-n1000-s2")


def test_load_grid_cells_has_the_committed_36_plus_probe() -> None:
    cells = export_storm_workload.load_grid_cells()
    assert len(cells) == 37
    main_grid = [c for c in cells.values() if c.source == "main-grid"]
    probes = [c for c in cells.values() if c.source == "probe"]
    assert len(main_grid) == 36
    assert len(probes) == 1
    stores = {c.store for c in main_grid}
    mixes = {c.mix for c in main_grid}
    ages = {c.age for c in main_grid}
    seeds = {c.seed for c in main_grid}
    assert stores == {"collegemsg", "synth-iv-60k"}
    assert mixes == {"c1", "c3", "c4"}
    assert ages == {None, "deep"}
    assert seeds == {0, 1, 2}
    probe = probes[0]
    assert probe.n_artifacts == 10_000
    assert probe.committed_changed is not None  # the probe's own rows.jsonl is committed in full


def test_extra_cells_have_no_reference() -> None:
    cells = export_storm_workload.load_grid_cells()
    assert "tiny-c1-none-n5-s1" not in cells  # A2-style cells are not in the default set


# ---------------------------------------------------------------------------
# equality scoring, without running a cell (fast, pure-function checks)
# ---------------------------------------------------------------------------

def test_score_equality_l1_when_digest_matches() -> None:
    spec = export_storm_workload.CellSpec(
        cell_id="x", store="s", mix="c1", age=None, n_artifacts=5, seed=0, batches=1,
        committed_dataset_digest="abc", committed_log_bytes=(100,), committed_n_registered=5,
        committed_per_template_counts={"compute": 5}, committed_changed=None,
        committed_correction_meta=None, committed_refused_count=None, source="main-grid",
    )
    out = export_storm_workload.score_equality(spec, "abc", [100], 5, {"compute": 5}, [])
    assert out["equality_level"] == "L1"


def test_score_equality_fails_on_n_registered_mismatch() -> None:
    spec = export_storm_workload.CellSpec(
        cell_id="x", store="s", mix="c1", age=None, n_artifacts=5, seed=0, batches=1,
        committed_dataset_digest="abc", committed_log_bytes=(100,), committed_n_registered=5,
        committed_per_template_counts={"compute": 5}, committed_changed=None,
        committed_correction_meta=None, committed_refused_count=None, source="main-grid",
    )
    out = export_storm_workload.score_equality(spec, "zzz", [100], 4, {"compute": 5}, [])
    assert out["equality_level"] == "FAIL"


def test_score_equality_l2_partial_without_per_batch_detail() -> None:
    spec = export_storm_workload.CellSpec(
        cell_id="x", store="s", mix="c1", age=None, n_artifacts=5, seed=0, batches=1,
        committed_dataset_digest="abc", committed_log_bytes=(100,), committed_n_registered=5,
        committed_per_template_counts={"compute": 5}, committed_changed=None,
        committed_correction_meta=None, committed_refused_count=None, source="main-grid",
    )
    out = export_storm_workload.score_equality(spec, "zzz", [100], 5, {"compute": 5}, [])
    assert out["equality_level"] == "L2-partial"
    assert out["checks"]["l2_full_per_batch_available"] is False


def test_score_equality_l2_full_when_per_batch_detail_available() -> None:
    spec = export_storm_workload.CellSpec(
        cell_id="x", store="s", mix="c1", age=None, n_artifacts=5, seed=0, batches=1,
        committed_dataset_digest="abc", committed_log_bytes=(100,), committed_n_registered=5,
        committed_per_template_counts=None,
        committed_changed=(("a", "b"),), committed_correction_meta=(("A", "gen", "place"),),
        committed_refused_count=(0,), source="probe",
    )
    batch_results = [{"changed": ["b", "a"], "correction_class": "A", "correction_generator": "gen",
                      "correction_placement": "place", "refused_count": 0}]
    out = export_storm_workload.score_equality(spec, "zzz", [100], 5, {}, batch_results)
    assert out["equality_level"] == "L2"


def test_score_equality_extra_cell_has_no_reference() -> None:
    spec = export_storm_workload.CellSpec(
        cell_id="x", store="s", mix="c1", age=None, n_artifacts=5, seed=0, batches=1,
        committed_dataset_digest=None, committed_log_bytes=None, committed_n_registered=None,
        committed_per_template_counts=None, committed_changed=None,
        committed_correction_meta=None, committed_refused_count=None, source="extra",
    )
    out = export_storm_workload.score_equality(spec, "zzz", [100], 5, {}, [])
    assert out["equality_level"] == "new cell (no committed digest)"


# ---------------------------------------------------------------------------
# a real (tiny) export, end to end
# ---------------------------------------------------------------------------

def test_export_cell_writes_the_full_bundle_and_is_internally_consistent(
    tmp_path: Path, tiny_cell_spec: "export_storm_workload.CellSpec",
) -> None:
    stores_dir = tmp_path / "stores"
    _build_fixture_store(stores_dir / "tiny")
    export_root = tmp_path / "export"

    result = export_storm_workload.export_cell(
        tiny_cell_spec, stores_dir, export_root, build_missing=False)

    assert result["equality_level"] == "new cell (no committed digest)"
    out_dir = export_root / "tiny-c1-none-n5-s1"
    for name in export_storm_workload.BUNDLE_FILES:
        assert (out_dir / name).exists(), name
    assert (out_dir / "export-manifest.json").exists()
    assert (out_dir / "digests.json").exists()

    artifacts = (out_dir / "artifacts.jsonl").read_text().splitlines()
    assert len(artifacts) == 5  # tiny_cell_spec.n_artifacts, none refused at this scale

    oracle_lines = (out_dir / "oracle.jsonl").read_text().splitlines()
    assert len(oracle_lines) == tiny_cell_spec.batches + 1  # epoch 0..batches
    for i, line in enumerate(oracle_lines):
        row = json.loads(line)
        assert row["epoch"] == i

    deltas_lines = (out_dir / "deltas.jsonl").read_text().splitlines()
    assert len(deltas_lines) == tiny_cell_spec.batches

    manifest = json.loads((out_dir / "export-manifest.json").read_text())
    assert len(manifest["version_table_sha256"]) == tiny_cell_spec.batches + 1
    assert set(manifest["files"]) == set(export_storm_workload.BUNDLE_FILES)
    for name, digest in manifest["files"].items():
        assert digest == export_storm_workload._sha256_file(out_dir / name)


def test_export_cell_is_deterministic_by_seed(
    tmp_path: Path, tiny_cell_spec: "export_storm_workload.CellSpec",
) -> None:
    stores_dir = tmp_path / "stores"
    _build_fixture_store(stores_dir / "tiny")

    export_root_a = tmp_path / "export-a"
    export_root_b = tmp_path / "export-b"
    export_storm_workload.export_cell(tiny_cell_spec, stores_dir, export_root_a,
                                      build_missing=False)
    export_storm_workload.export_cell(tiny_cell_spec, stores_dir, export_root_b,
                                      build_missing=False)

    out_a = export_root_a / tiny_cell_spec.cell_id
    out_b = export_root_b / tiny_cell_spec.cell_id
    for name in ("versions-epoch0.jsonl", "artifacts.jsonl", "eventlog-tail.jsonl",
                "deltas.jsonl", "oracle.jsonl"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes(), name


def test_export_cell_raises_when_batches_cannot_be_realized(
    tmp_path: Path, tiny_cell_spec: "export_storm_workload.CellSpec",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force every draw to be unrealizable (mirrors `Storm.run_batch`
    # returning `None`, the "no correction was realizable" case) so the
    # bounded-attempt loop gives up — the cell must not be silently
    # "exported" partial.
    monkeypatch.setattr(export_storm_workload.ExportStorm, "run_batch_export",
                        lambda self, batch_index: None)
    stores_dir = tmp_path / "stores"
    _build_fixture_store(stores_dir / "tiny")
    with pytest.raises(RuntimeError, match="not exported"):
        export_storm_workload.export_cell(tiny_cell_spec, stores_dir, tmp_path / "export",
                                          build_missing=False, max_attempts_factor=1)


# ---------------------------------------------------------------------------
# ensure_store
# ---------------------------------------------------------------------------

def test_ensure_store_present(tmp_path: Path) -> None:
    stores_dir = tmp_path / "stores"
    _build_fixture_store(stores_dir / "tiny")
    path, status = export_storm_workload.ensure_store("tiny", stores_dir, build_missing=False)
    assert path == stores_dir / "tiny"
    assert status == "present"


def test_ensure_store_missing_without_build_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_storm_workload.ensure_store("tiny", tmp_path / "stores", build_missing=False)
